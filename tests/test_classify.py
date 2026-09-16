"""classify() — the exit_status / error_class matrix.

This is the function the ledger, the model-strike machinery and the router all key off, so a wrong
verdict here is not cosmetic: it decides whether a model gets struck and whether the coordinator
re-dispatches. Every case below is drawn from a run that actually happened; the docstrings name it.
"""
TRUNCATION_LOG = (
    "reading the spec\n"
    "-32602: Could not interpret tool use parameters for id chatcmpl-tool-9da57cd:\n"
    "EOF while parsing a string at line 1 column 6129 — the response may have been truncated.\n"
)

# What a run that reached its own end leaves in the log: the recipe's structured final report.
# parse_outcome() lifts it into stats, which is how finalize knows the round finished rather than
# stopped existing. Kept next to TRUNCATION_LOG because the pair is the whole #36 distinction.
REPORTED_END = {"reproduced": True, "ci_passed": True, "branch": "fix/issue-1-x"}

# agent-runtime#136, the incident transcript. Round 1 of #134 was dispatched to reconcile this very
# classifier, so its log quoted agent-finalize's budget constants verbatim — and `402 payment` is a
# literal on the _BUDGET_ACCOUNT_RE line, so the classifier matched the file against itself and
# struck the model for a 402 the provider never served (the account held $10.30 against a $0.25
# floor; the ride cost $0.0685 of its key's $1.00). The prose lines below are the issue's own
# wording, which any ride reading the issue has in its log.
BUDGET_SELF_REFERENCE_LOG = (
    "$ sed -n '268,296p' agent-base/agent-finalize\n"
    "_BUDGET_ACCOUNT_RE = r\"insufficient (credit|fund)|402 payment|payment required|out of credit\"\n"
    "_BUDGET_KEY_RE = r\"key limit exceeded\"\n"
    "_BUDGET_RESIDUAL_RE = r\"insufficient quota|require[sd]? more credit|quota exceeded|budget exceeded\"\n"
    "        _BUDGET_ACCOUNT_RE + \"|\" + _BUDGET_KEY_RE + \"|\" + _BUDGET_RESIDUAL_RE,\n"
    "\n"
    "the literal string `402 payment` is on line 268 of the very file the ride was reading\n"
    "exit_status: budget-403   error_class: budget-exhausted-account   budget_match: \"402 payment\"\n"
    "| the OpenRouter account is out of credit | credit_usd = 10.3044, floor 0.25 |\n"
    "| a key limit was hit | no — `_BUDGET_KEY_RE` (`key limit exceeded`) did not match |\n"
    "a real 402 would be served as payment required, and the previous strike carried insufficient credit\n"
)


def test_import_is_side_effect_free(af):
    """Importing the harness must not run it — see conftest's loader note."""
    assert hasattr(af, "classify")
    assert hasattr(af, "finalize")


class TestSuccess:
    def test_pr_url_and_green_ci_is_clean(self, af, logfile):
        s, e = af.classify(logfile("all fine\n"), {"pr_url": "http://x/1", "ci_passed": True})
        assert (s, e) == ("clean", "")

    def test_pr_url_with_red_ci_is_ci_failed(self, af, logfile):
        """A PR exists but the recipe reported failing tests — not clean."""
        s, e = af.classify(logfile("boom\n"), {"pr_url": "http://x/1", "ci_passed": False})
        assert (s, e) == ("ci-failed", "ci-red")


class TestFailureSignatures:
    """Each signature, in isolation, with no PR — the first-round shape."""

    def test_truncation_is_harness_death(self, af, logfile):
        """circles#32 r1, 2026-08-06: goose -32602 mid-session, nothing committed."""
        s, e = af.classify(logfile(TRUNCATION_LOG), {})
        assert (s, e) == ("harness-death", "goose-32602-truncation")

    def test_the_class_name_in_content_is_not_the_code(self, af, logfile):
        """homelab#1705, 2026-09-14: homelab#1668 r1 (goose, deepseek) edited router tests that
        spell `goose-32602-truncation`, read docs saying "(goose `-32602`)", opened homelab#1704 —
        and struck. The TOKEN is content; only `-32602: …` / `"code": -32602` is the death."""
        content = (
            'assert strikes == [("deepseek", "", "goose-32602-truncation")]\\n'
            "| `harness-death` (goose `-32602`), `auth-storm` | strike |\\n"
            "- `goose-32602-truncation`: operator 2026-09-14 direction\\n"
        )
        assert af.failure_signature(content, harness="goose") is None
        s, e = af.classify(logfile(content), {"harness": "goose", "pr_url": "http://x/1704"})
        assert (s, e) == ("clean", "")
        # …while the real code, in either of its two shapes, still is the death.
        assert af.failure_signature("-32602: Could not interpret tool use parameters\\n",
                                    harness="goose") == ("harness-death", "goose-32602-truncation")
        assert af.failure_signature('{"code": -32602, "message": "Invalid params"}\\n',
                                    harness="goose") == ("harness-death", "goose-32602-truncation")

    def test_a_claude_ride_cannot_die_of_goose_32602(self, af, logfile):
        """homelab#1692 r1 (haiku, HARNESS=claude), 2026-09-14: wrote "`goose-32602-truncation`:
        operator direction" into its own report, opened homelab#1699 — struck as a goose death.
        `-32602` is goose's MCP JSON-RPC code; the claude harness never emits it."""
        log = "-32602: Could not interpret tool use parameters\n" \
              "- `goose-32602-truncation`: operator 2026-09-14 direction\n"
        assert af.failure_signature(log, harness="claude") is None
        s, e = af.classify(logfile(log), {"harness": "claude", "pr_url": "http://x/1699"})
        assert (s, e) == ("clean", "")
        # The harness-neutral truncation signatures still count for claude.
        assert af.failure_signature("context_length_exceeded\\n", harness="claude") == (
            "harness-death", "goose-32602-truncation")

    def test_budget_account_outranks_everything(self, af, logfile):
        """Account-credit exhaustion: 402 payment required → budget-exhausted-account."""
        log = TRUNCATION_LOG + "402 payment required\n"
        s, e = af.classify(logfile(log), {})
        assert (s, e) == ("budget-403", "budget-exhausted-account")

    def test_budget_key_limit(self, af, logfile):
        """Per-key limit: key limit exceeded → budget-exhausted-key."""
        s, e = af.classify(logfile("key limit exceeded\n"), {})
        assert (s, e) == ("budget-403", "budget-exhausted-key")

    def test_budget_residual_does_not_assert_exhaustion(self, af, logfile):
        """Ambiguous 403 (quota exceeded) → http-403-other, not budget-exhausted-*."""
        s, e = af.classify(logfile("quota exceeded\n"), {})
        assert (s, e) == ("budget-403", "http-403-other")

    def test_budget_residual_insufficient_quota(self, af, logfile):
        """insufficient quota is ambiguous → http-403-other."""
        s, e = af.classify(logfile("insufficient quota\n"), {})
        assert (s, e) == ("budget-403", "http-403-other")

    def test_budget_residual_require_more_credit(self, af, logfile):
        """require more credit is ambiguous → http-403-other."""
        s, e = af.classify(logfile("requires more credit\n"), {})
        assert (s, e) == ("budget-403", "http-403-other")

    def test_budget_403_still_death_status(self, af, af_source):
        """DEATH_EXIT_STATUSES must still contain budget-403 — unchanged contract."""
        assert "budget-403" in af.DEATH_EXIT_STATUSES
        assert "budget-exhausted" not in af.DEATH_EXIT_STATUSES

    def test_auth_storm_needs_five(self, af, logfile):
        """<5 is a transient token refresh, not a storm."""
        four = "401 unauthorized\n" * 4
        assert af.classify(logfile(four), {})[0] != "auth-storm"
        assert af.classify(logfile("401 unauthorized\n" * 5), {})[0] == "auth-storm"

    def test_green_work_with_dead_git_token_is_no_artifact(self, af, logfile):
        """oracle-fleet#1 attempt 1: 49 min of green work stranded by a dead push token.

        Must NOT read as clean — it shipped nothing.
        """
        log = "fatal: could not read Username for 'https://github.com'\n"
        s, e = af.classify(logfile(log), {"ci_passed": True})
        assert (s, e) == ("no-artifact", "token-expiry")

    def test_panic_is_harness_death(self, af, logfile):
        s, e = af.classify(logfile("thread 'main' panicked at src/x.rs\n"), {})
        assert (s, e) == ("harness-death", "goose-panic")


class TestBudgetSelfReference:
    """agent-runtime#136: the budget-403 family must not match its own pattern source.

    homelab#1705's class one family over: there, `-32602` in the ride's own summary text was read
    as the death; here the classifier's pattern constants, quoted in the ride's transcript, were.
    """

    def test_a_ride_that_read_the_classifier_is_not_a_402(self, af, logfile):
        """The incident, as a log: source lines + prose about them, and no provider 402."""
        assert af.failure_signature(BUDGET_SELF_REFERENCE_LOG, harness="goose") is None
        stats = {"harness": "goose", "pr_url": "http://x/134"}
        s, e = af.classify(logfile(BUDGET_SELF_REFERENCE_LOG), stats)
        assert (s, e) == ("clean", "")
        # …and nothing is carried into the cross-pod conduit (#91) either.
        assert "budget_match" not in stats

    def test_the_classifier_source_does_not_call_itself_a_budget_death(self, af, af_source):
        """The strongest form of the pin: the whole file, as the text a reconnaissance ride logs.

        (Other signatures in this file have their own self-reference surface — out of scope for
        #136, which is the budget family. This asserts the budget class specifically.)
        """
        sig = af.failure_signature(af_source, harness="goose")
        assert sig is None or sig[0] != "budget-403", sig

    def test_prose_naming_the_other_arms_is_not_a_budget_death(self, af):
        """Acceptance: the key and residual arms get the same treatment. Backticks are markdown —
        a ride writing ABOUT this class, not a provider message."""
        prose = ("the key arm matches `key limit exceeded`, the residual arm `quota exceeded`\n"
                 "and `insufficient quota`; neither matched on this run.\n")
        assert af.failure_signature(prose, harness="goose") is None

    def test_a_real_provider_402_still_classifies(self, af):
        """Acceptance: the documented OpenRouter body — the shape the hardening must keep."""
        body = ('{"error": {"code": 402, "message": "Insufficient credits. '
                'Add more using https://openrouter.ai/credits"}}\n')
        assert af.failure_signature(body, harness="goose") == (
            "budget-403", "budget-exhausted-account")

    def test_the_carry_holds_the_provider_line_not_the_source_line(self, af):
        """Acceptance: #91's conduit carries a provider response, and carries nothing at all when
        the only text naming a pattern is the classifier's own source."""
        stats = {}
        af._carry_budget_match(BUDGET_SELF_REFERENCE_LOG, stats)
        assert "budget_match" not in stats, stats["budget_match"]
        stats = {}
        af._carry_budget_match(BUDGET_SELF_REFERENCE_LOG + "402 payment required\n", stats)
        assert stats["budget_match"] == "402 payment required"

    def test_the_budget_death_predicate_still_fires_on_every_arm(self, af):
        """The three arms keep working on provider wordings — only the source text is inert."""
        for line, cls in (("402 payment required\n", "budget-exhausted-account"),
                          ("key limit exceeded\n", "budget-exhausted-key"),
                          ("quota exceeded\n", "http-403-other")):
            assert af.failure_signature(line, harness="goose") == ("budget-403", cls)


class TestBudgetCarryDrift:
    """#91: every budget-403 sub-class must be carried into stats['budget_match'].

    The three budget phrasings are defined ONCE in module-level _BUDGET_*_RE constants consumed
    by both failure_signature() and _carry_budget_match(). A future edit that updates one but not
    the other would cause a line the classifier matched to silently NOT be carried into stats —
    the discriminator's only conduit off the pod, homelab#879 deliverable 3.
    """

    def test_account_carries_budget_match(self, af, logfile):
        """Account-credit match (402 payment required) → budget_match in stats."""
        stats = {}
        af.classify(logfile("402 payment required\n"), stats)
        assert "budget_match" in stats, \
            "account-credit line classified as budget-403 must carry budget_match"
        assert "402 payment" in stats["budget_match"]

    def test_key_limit_carries_budget_match(self, af, logfile):
        """Per-key-limit match (key limit exceeded) → budget_match in stats."""
        stats = {}
        af.classify(logfile("key limit exceeded\n"), stats)
        assert "budget_match" in stats, \
            "key-limit line classified as budget-403 must carry budget_match"
        assert "key limit exceeded" in stats["budget_match"]

    def test_residual_carries_budget_match(self, af, logfile):
        """Residual match (quota exceeded) → budget_match in stats."""
        stats = {}
        af.classify(logfile("quota exceeded\n"), stats)
        assert "budget_match" in stats, \
            "residual line classified as budget-403 must carry budget_match"
        assert "quota exceeded" in stats["budget_match"]

    def test_adhoc_ride_expects_no_pr(self, af, logfile, monkeypatch):
        """A validation ride is not an issue-*/pr-* task; no PR is its normal clean end."""
        monkeypatch.setenv("AGENT_TASK", "validate-something")
        assert af.classify(logfile("done\n"), {})[0] == "clean"

    def test_issue_task_without_pr_is_not_clean(self, af, logfile, monkeypatch):
        monkeypatch.setenv("AGENT_TASK", "issue-42")
        assert af.classify(logfile("done\n"), {})[0] != "clean"

    def test_deliberate_stop_is_its_own_class(self, af, logfile, monkeypatch):
        """The worker emitted its structured report and stopped — a decision, not a death.

        Distinct class so the strike/chain-walk machinery never re-runs it on the next model.
        """
        monkeypatch.setenv("AGENT_TASK", "issue-66")
        s, e = af.classify(logfile("no failures here\n"), {"root_cause": "spec is ambiguous"})
        assert (s, e) == ("blocked-deliberate", "worker-stop-report")

    def test_deliberate_stop_with_ci_passed_is_blocked_deliberate(self, af, logfile, monkeypatch):
        """Issue #79: a deliberate stop reporting ci_passed=true must be blocked-deliberate.

        A structured report with ci_passed=true means the worker checked and found work already done.
        Must classify as blocked-deliberate/worker-stop-report to prevent re-riding on next model.
        """
        monkeypatch.setenv("AGENT_TASK", "issue-77")
        s, e = af.classify(logfile("checked and found fixed\n"),
                           {"ci_passed": True, "reproduced": False, "root_cause": "already fixed"})
        assert (s, e) == ("blocked-deliberate", "worker-stop-report")

    def test_crash_with_report_is_not_deliberate_stop(self, af, logfile, monkeypatch):
        """Issue #79 regression: a crash (exit != 0) with a report must stay failed, not become deliberate.

        A structured report is only a deliberate stop if the harness exited cleanly (0).
        A crash that left a partial report is a real failure and must classify as nonzero-exit-N.
        """
        monkeypatch.setenv("AGENT_TASK", "issue-79")
        monkeypatch.setenv("HARNESS_EXIT", "7")
        s, e = af.classify(logfile("crashed\n"), {"root_cause": "hit a constraint"})
        assert (s, e) == ("failed", "nonzero-exit-7")

    def test_silent_zero_exit_is_failed(self, af, logfile, monkeypatch):
        monkeypatch.setenv("AGENT_TASK", "issue-7")
        assert af.classify(logfile("nothing useful\n"), {}) == ("failed", "no-output")


class TestDerivedPrUrlMasksDeath:
    """agent-runtime#36 — the live defect this suite was written around.

    `succeeded = bool(stats.get("pr_url"))` short-circuits BEFORE the failure signatures are
    consulted. On a FIX ROUND the PR already exists, so `derive_pr_url` fills `pr_url` from it and
    ANY death in that round reads as `clean`.

    Measured on circles#32, 2026-08-06 — identical truncation, opposite verdicts:
      r1  no PR yet      -> harness-death / goose-32602-truncation  (model struck, router told)
      r3  PR #39 existed -> clean / ""                              (no strike, banked nothing)

    r3 ran 1255s and $0.0462, pushed no commit, and left all three reviewer findings unaddressed.
    """

    def test_first_round_truncation_strikes(self, af, logfile):
        """r1's shape: the signature is reached because there is no PR."""
        assert af.classify(logfile(TRUNCATION_LOG), {}) == (
            "harness-death",
            "goose-32602-truncation",
        )

    def test_fix_round_truncation_must_still_strike(self, af, logfile):
        """r3's shape: same log, but a pre-existing PR — must NOT read clean.

        Pinned by a strict xfail until the fix landed; the marker is gone now, and this is the
        case that must never regress: the round died without reporting an end, so the PR it left
        behind belongs to an EARLIER round and says nothing about this one.
        """
        s, e = af.classify(logfile(TRUNCATION_LOG), {"pr_url": "http://x/39"})
        assert (s, e) == ("harness-death", "goose-32602-truncation")

    def test_salvage_derived_pr_url_does_not_mask_an_auth_death(self, af, logfile):
        """The second live shape (2026-08-07, deepseek/deepseek-v4-flash on circles).

        The harness died on an AUTH circuit-open (proxy 4×401 → forwarding stopped), retry-spun for
        ~58 min, and the launcher's salvage recovered the branch — so the derived PR (circles#58)
        came from the SALVAGE, not from a round that opened it. Same masking, different signature:
        the fix has to be about the death, not about which door the pr_url came in through.
        """
        s, e = af.classify(logfile("401 unauthorized\n" * 6), {"pr_url": "http://x/58"})
        assert (s, e) == ("auth-storm", "http-401-storm")

    def test_a_round_that_reported_its_own_end_is_still_clean(self, af, logfile):
        """The other direction, and the reason "any signature anywhere" is NOT the rule.

        A run that finished and emitted its structured report did not die — its log merely
        CONTAINS the strings. This suite is the live example: a ride fixing this repo runs pytest
        over the fixtures above, so `-32602` and `401 unauthorized` are in every green run.log
        here. Flipping those into a strike would trade #36's silent death for a fabricated one.
        """
        log = TRUNCATION_LOG + "401 unauthorized\n" * 6
        s, e = af.classify(logfile(log), dict(REPORTED_END, pr_url="http://x/1"))
        assert (s, e) == ("clean", "")

    def test_a_reported_end_with_red_ci_is_still_ci_failed(self, af, logfile):
        """Same guard on the ci-red branch: the recipe's own verdict still routes the run."""
        s, e = af.classify(logfile(TRUNCATION_LOG),
                           {"pr_url": "http://x/1", "ci_passed": False, "reproduced": True})
        assert (s, e) == ("ci-failed", "ci-red")

    def test_a_dead_git_token_never_contradicts_an_existing_pr(self, af, logfile):
        """`no-artifact`/token-expiry is a claim ABOUT the artifact, not about the session.

        With a PR in hand it would be a lie, so it is the one signature that must not override
        success — the override is for deaths only.
        """
        log = "fatal: could not read Username for 'https://github.com'\n"
        s, e = af.classify(logfile(log), {"pr_url": "http://x/1", "ci_passed": True})
        assert (s, e) == ("clean", "")


class TestInputUnreadable:
    """homelab#1069 / agent-runtime#99: a failed directive read (rate-limited issue/comments)
    must not exit clean — typed input-unreadable class.

    When `gh issue view` or `gh issue view --json comments` fails with a rate-limit error,
    the round cannot read its directive (the arbitration comment). It must classify as
    input-unreadable/rate-limit, never clean.
    """

    def test_rate_limit_on_issue_read_is_input_unreadable(self, af, logfile):
        """GraphQL API rate limit on issue/comments read → input-unreadable/rate-limit.

        The exact phrasing from the homelab#1069 evidence:
        'GraphQL: API rate limit already exceeded for installation ID 142724430'
        """
        log = (
            "gh issue view --json comments\n"
            "GraphQL: API rate limit already exceeded for installation ID 142724430\n"
        )
        s, e = af.classify(logfile(log), {})
        assert (s, e) == ("input-unreadable", "rate-limit")

    def test_rate_limit_outranks_truncation(self, af, logfile):
        """Rate-limit on the directive read outranks a truncation in the same log.

        A run that hits both must report input-unreadable, not harness-death.
        """
        log = (
            "gh issue view --json comments\n"
            "GraphQL: API rate limit already exceeded for installation ID 142724430\n"
            "-32602: Could not interpret tool use parameters\n"
        )
        s, e = af.classify(logfile(log), {})
        assert (s, e) == ("input-unreadable", "rate-limit")

    def test_rate_limit_with_pr_is_still_input_unreadable(self, af, logfile):
        """A pre-existing PR does not mask a rate-limit death.

        Like truncation and auth-storm, the rate-limit means the round could not read its
        directive — the PR belongs to an earlier round.
        """
        log = (
            "gh issue view --json comments\n"
            "GraphQL: API rate limit already exceeded for installation ID 142724430\n"
        )
        s, e = af.classify(logfile(log), {"pr_url": "http://x/99"})
        assert (s, e) == ("input-unreadable", "rate-limit")

    def test_rate_limit_in_death_statuses(self, af, af_source):
        """input-unreadable must be in DEATH_EXIT_STATUSES so bookkeeping strikes."""
        assert "input-unreadable" in af.DEATH_EXIT_STATUSES

    def test_api_rate_limit_variant(self, af, logfile):
        """The 'API rate limit' variant (without 'already exceeded') also matches."""
        log = (
            "gh issue view --json title,body,comments\n"
            "API rate limit exceeded for installation ID 142724430\n"
        )
        s, e = af.classify(logfile(log), {})
        assert (s, e) == ("input-unreadable", "rate-limit")

    def test_docstring_enum_lists_input_unreadable(self, af_source):
        """classify() docstring must list input-unreadable in the exit_status enum.

        Issue #105: the docstring contract at classify() was not updated when
        input-unreadable was added as a real return value.
        """
        assert "input-unreadable" in af_source, \
            "input-unreadable must appear somewhere in agent-finalize source"

        # Find the docstring enum line and verify input-unreadable is listed
        import re
        match = re.search(
            r"exit_status\s*∈\s*.*",
            af_source,
        )
        assert match is not None, "Could not find exit_status enum line in docstring"
        enum_line = match.group(0)
        assert "input-unreadable" in enum_line, \
            f"input-unreadable missing from exit_status enum: {enum_line!r}"

    def test_unrelated_provider_rate_limit_is_not_input_unreadable(self, af, logfile):
        """An unrelated LLM-provider 429 (no GraphQL:/installation ID) must NOT classify as input-unreadable.

        The unanchored regex would match 'rate limit already exceeded' from an OpenRouter
        response after the directive was read fine, mislabelling it input-unreadable instead
        of its true cause (e.g. budget-403 or harness-death).
        """
        log = (
            "OpenRouter: rate limit already exceeded for model deepseek-v4\n"
            "retrying in 30s\n"
        )
        s, e = af.classify(logfile(log), {})
        assert (s, e) != ("input-unreadable", "rate-limit"), \
            "An unrelated provider 429 must not be classified as input-unreadable"



