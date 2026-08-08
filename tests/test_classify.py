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

    def test_budget_outranks_everything(self, af, logfile):
        """A dead key dooms the run; the truncation it ends on is a symptom."""
        log = TRUNCATION_LOG + "402 payment required\n"
        s, e = af.classify(logfile(log), {})
        assert (s, e) == ("budget-403", "budget-exhausted")

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


class TestNoArtifactPaths:
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
