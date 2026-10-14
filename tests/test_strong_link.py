"""The container-written strong link on a PR the worker did not open (homelab#2063 / #2046).

The class: the un-armed `major` lane dispatches a worker ride ONTO an existing Renovate PR branch
with a carrier issue describing the adaptation. The worker on homelab#2046 committed with only
`Refs #2063` in its COMMIT message — a weak link — and Renovate's PR body never names the carrier.
The homelab scan's issue-label lifecycle (`agents/coordinator-scan.sh` review-flip belt + C6
merged-closeout) and GitHub's own close-on-merge both key on a STRONG link in the PR BODY, so
#2063 would have stayed open past the merge. On #2033 the worker happened to write
`Implements #2071` / `Fixes #2036` into the body — a link left to the worker's prose is the
defect; it must be CONTAINER-WRITTEN (ADR-122).

The guarantee (`ensure_strong_link`, the #32 step in `bookkeeping()`) already existed and failed
LIVE on that ride: it read and wrote through `gh pr view`/`gh pr edit`, which are GraphQL, and the
App installation's GraphQL pool was exhausted by the ride's own `gh pr checks`/`gh pr list` calls
(`GraphQL: API rate limit already exceeded for installation ID …`, pod
agent-homelab-issue-2063-r1, 09:57:25Z). The REST-only ADR-103 summary line on the same ride
landed. So the fix pins two things:

  1. TRANSPORT — the guarantee is REST end to end (`gh api repos/{slug}/pulls/{n}` for body+base,
     `repos/{slug}` for the default branch, `--method PATCH … --input -` for the write), and it
     lands while every GraphQL verb refuses with the live error string.
  2. GRAMMAR — presence is tested with the READER's grammar, the scan's own regex pair
     (`docs/agents/issue-authoring.md` §A child cannot close itself; agents/coordinator-scan.sh
     review-flip belt), copied into `strong_link_for`:
         (^|[^a-z])(implements|closes|close[ds]?|fixe[ds]?|fix|resolve[ds]?)[ \\t]+#N\\b   (i)
         (?m)^[ \\t]*issue:[ \\t]*#N\\b                                                  (i)
     A body that already strongly links #N in ANY of those forms is left byte-identical; a
     `Refs #N` or bare `#N` is not a link and the write happens.

Expected values in the cases below are derived from those two regexes and the #107 keyword rule
(`Fixes` when the PR base IS the default branch, else `Implements`; prepend + blank line), never by
running the code.

Hermetic like the sibling suites: `RestWorld` is an in-memory GitHub reached through the same
`gh(*argv, stdin=…)` seam `bookkeeping()` uses; the GraphQL verbs can be made to refuse as a unit.
"""
import json
import re

import pytest

LIVE_GRAPHQL_ERROR = "GraphQL: API rate limit already exceeded for installation ID 142724430.\n"

# Renovate's body on homelab#2046, abridged to its load-bearing shape: a table, a release-notes
# fold, the debug trailer, then the worker's adaptation section — and NO `#2063` anywhere.
RENOVATE_BODY = (
    "This PR contains the following updates:\n\n"
    "| Package | Type | Update | Change |\n|---|---|---|---|\n"
    "| helm | required_provider | major | `~> 2.17` → `~> 3.0` |\n\n---\n\n"
    "<details>\n<summary>hashicorp/terraform-provider-helm (helm)</summary>\n…\n</details>\n\n"
    "🚦 **Automerge**: Disabled by config. Please merge this manually once you are satisfied.\n\n"
    "<!--renovate-debug:eyJjcmVhdGVkSW5WZXIiOiI0NC4xMTUuMTMifQ==-->\n\n\n---\n\n"
    "### Adaptation for terraform-provider-helm 3.x (blocks → nested objects)\n\n"
    "**File changed:** `tofu/cilium.tf` — one line. `kubernetes {` → `kubernetes = {`.\n\n"
    "A **human** merges this PR (un-armed `major`)."
)


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class RestWorld:
    """In-memory GitHub for the guarantee: ONE PR (`slug`#`number`) with a mutable body and a base
    ref, a repo default branch, a set of numbers that ARE pull requests (for the #107 probe), and
    the ADR-103 endpoints the arm leg reaches. `graphql_down=True` makes every non-`api` gh verb
    (`pr view`, `pr edit`, `pr merge`, `repo view`, …) refuse with the live error — the pool the
    ride drained — while `gh api` (REST, a separate pool) keeps answering.

    `pr_author` is recorded for the reader: the guarantee is deliberately author-blind (a PR the
    worker opened and forgot to link needs the write as much as a Renovate one — #32), so the
    "worker-opened PR is untouched" case is carried by its body, which the open-time contract says
    already holds the trailer."""

    def __init__(self, body, base="master", default_branch="master", slug="o/r", number=2046,
                 pr_numbers=(), pr_author="app/renovate", graphql_down=False,
                 fail_read=False, fail_patch=False):
        self.body = body
        self.base = base
        self.default_branch = default_branch
        self.slug, self.number = slug, number
        self.pr_numbers = {str(n) for n in pr_numbers} | {str(number)}
        self.pr_author = pr_author
        self.graphql_down = graphql_down
        self.fail_read, self.fail_patch = fail_read, fail_patch
        self.calls = []       # (argv tuple, stdin)
        self.patches = []     # bodies written, in order
        self.comments = []
        self.check_runs = []

    # -- the seam agent-finalize calls: gh(*argv, stdin=..., timeout=...) -----------------------
    def __call__(self, *args, stdin=None, timeout=30):
        self.calls.append((tuple(args), stdin))
        argv = list(args)
        if argv[0] != "api":
            if self.graphql_down:
                return _Done(1, "", LIVE_GRAPHQL_ERROR)
            if argv[:2] == ["pr", "view"]:
                if "labels" in argv:
                    return _Done(0, json.dumps({"labels": [{"name": "major"}]}))
                if "headRefOid" in argv:
                    return _Done(0, "deadbee\n")
                return _Done(0, self.body)
            if argv[:2] == ["pr", "edit"]:
                self.body = argv[argv.index("--body") + 1]
                return _Done(0)
            return _Done(0)
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        path = [a for a in argv[1:] if not a.startswith("-") and a != method
                and not (a.startswith(".") and "--jq" in argv)][0]
        payload = json.loads(stdin) if stdin else {}
        m = re.fullmatch(r"repos/([^/]+/[^/]+)/pulls/(\d+)", path)
        if m:
            slug, num = m.groups()
            if slug != self.slug or num not in self.pr_numbers:
                return _Done(1, "", "HTTP 404: Not Found\n")
            if ".number" in argv:
                return _Done(0, num + "\n")
            if num != str(self.number):
                return _Done(1, "", "HTTP 404: Not Found\n")
            if method == "PATCH":
                if self.fail_patch:
                    return _Done(1, "", "HTTP 403: Resource not accessible by integration\n")
                self.body = payload["body"]
                self.patches.append(payload["body"])
                return _Done(0, json.dumps({"number": self.number, "body": self.body}))
            if self.fail_read:
                return _Done(1, "", "HTTP 502: Server Error\n")
            return _Done(0, json.dumps({"number": self.number, "body": self.body,
                                        "base": {"ref": self.base},
                                        "user": {"login": self.pr_author}}))
        if re.fullmatch(r"repos/[^/]+/[^/]+", path) and ".default_branch" in argv:
            return _Done(0, self.default_branch + "\n")
        if path.endswith("/check-runs"):
            self.check_runs.append(payload)
            return _Done(0, json.dumps({"id": 7}))
        if method == "GET":
            return _Done(0, json.dumps(self.comments))
        if method == "POST":
            self.comments.append(payload.get("body"))
            return _Done(0, json.dumps({"id": 1, "created_at": "t", "body": payload.get("body")}))
        return _Done(0)

    # -- assertion helpers ----------------------------------------------------------------------
    def graphql_calls(self):
        return [c for (c, _s) in self.calls if c[0] != "api"]

    def pr_edits(self):
        return [c for (c, _s) in self.calls if c[:2] == ("pr", "edit")]


class TestStrongLinkFor:
    """`strong_link_for(body, n)` — the READER's grammar, one case per production of the scan's
    regex pair. Each expectation is read off the regex, not off the function."""

    @pytest.mark.parametrize("keyword", [
        "implements", "closes", "close", "closed", "fixes", "fix", "fixed",
        "resolves", "resolve", "resolved",
    ])
    def test_every_keyword_the_scan_accepts_is_strong(self, af, keyword):
        # `(implements|closes|close[ds]?|fixe[ds]?|fix|resolve[ds]?)` → exactly these ten spellings.
        assert af.strong_link_for("%s #2063\n" % keyword, "2063") is True

    def test_case_is_folded(self, af):
        # jq "i" ⇔ re.IGNORECASE: `Fixes`, `FIXES`, `fIxEs` all match.
        assert af.strong_link_for("FIXES #2063", 2063) is True
        assert af.strong_link_for("Implements #2063", "2063") is True

    def test_tabs_and_several_blanks_are_allowed_between_keyword_and_number(self, af):
        # `[ \t]+` — one or more of space/tab, nothing else.
        assert af.strong_link_for("Fixes\t#2063", "2063") is True
        assert af.strong_link_for("Fixes   #2063", "2063") is True
        assert af.strong_link_for("Fixes\n#2063", "2063") is False   # a newline is not `[ \t]`
        assert af.strong_link_for("Fixes#2063", "2063") is False     # `+`, not `*`

    def test_mid_body_is_fine_for_keywords(self, af):
        # `(^|[^a-z])` anchors only the KEYWORD's left edge, not the line: prose around it is ok.
        assert af.strong_link_for("Some prose.\n\nThis also fixes #2063 here.\n", "2063") is True

    def test_a_keyword_glued_to_a_letter_is_not_strong(self, af):
        # `(^|[^a-z])` — `prefixes #N` contains `fixes #N` but the char before `fixes` is `e`.
        assert af.strong_link_for("prefixes #2063", "2063") is False
        # …and IGNORECASE folds the guard too: `E` is inside `[^a-z]`'s excluded set under "i".
        assert af.strong_link_for("prEfixes #2063", "2063") is False

    def test_the_issue_trailer_is_strong_only_when_line_anchored(self, af):
        # `(?m)^[ \t]*issue:[ \t]*#N\b` — `Issue:` must start its line (leading blanks allowed),
        # and the blank between `:` and `#` is OPTIONAL (`*`).
        assert af.strong_link_for("Title\n\nIssue: #2063\n", "2063") is True
        assert af.strong_link_for("  issue:#2063", "2063") is True
        assert af.strong_link_for("Tracking issue: #2063", "2063") is False

    def test_refs_and_bare_mentions_are_weak(self, af):
        # THE live shape: `Refs #N` is not in the keyword list; `#N` alone has no keyword.
        assert af.strong_link_for("Refs #2063\n", "2063") is False
        assert af.strong_link_for("See #2063 for the adaptation.\n", "2063") is False
        assert af.strong_link_for(RENOVATE_BODY, "2063") is False

    def test_the_number_is_whole(self, af):
        # `#N\b` — `Fixes #20631` does not link #2063; `Fixes #2063` does not link #206.
        assert af.strong_link_for("Fixes #20631", "2063") is False
        assert af.strong_link_for("Fixes #2063", "206") is False
        assert af.strong_link_for("Fixes #2063.", "2063") is True   # `.` is a boundary

    def test_a_link_to_another_issue_is_not_a_link_to_this_one(self, af):
        # #2033's shape: `Fixes #2036` in the body says nothing about #2071.
        assert af.strong_link_for("Fixes #2036\n", "2071") is False

    def test_nothing_to_match(self, af):
        assert af.strong_link_for("", "2063") is False
        assert af.strong_link_for(None, "2063") is False
        assert af.strong_link_for("Fixes #2063", "") is False
        assert af.strong_link_for("Fixes #2063", "abc") is False


class TestEnsureStrongLink:
    """`ensure_strong_link(gh, slug, pr_url, issue, stats)` on its own — the write and its
    idempotence, REST only."""

    URL = "https://github.com/o/r/pull/2046"

    def test_missing_link_on_a_renovate_pr_is_prepended_with_fixes_on_the_default_branch(self, af):
        """THE live instance: Renovate's body, base master == default master, carrier #2063.
        Expected body = `Fixes #2063` + blank line + the ORIGINAL body byte for byte (#107's
        `Fixes` on the default branch; #32's prepend-never-rewrite)."""
        gh = RestWorld(RENOVATE_BODY)
        stats = {}
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", stats)
        assert gh.patches == ["Fixes #2063\n\n" + RENOVATE_BODY]
        assert af.strong_link_for(gh.body, "2063") is True    # the scan now reads it
        assert stats.get("issue_link_added_by_pod") is True

    def test_off_the_default_branch_the_keyword_is_implements(self, af):
        """A `goal/**` base: a closing keyword is inert there and the goal lane owns closure."""
        gh = RestWorld("goal child body\n", base="goal/17-p0-mvp", default_branch="master")
        af.ensure_strong_link(gh, "o/r", self.URL, "73", {})
        assert gh.patches == ["Implements #73\n\ngoal child body\n"]

    @pytest.mark.parametrize("present", [
        "Fixes #2063\n\n%s", "%s\n\nImplements #2063", "%s\n\nIssue: #2063\n",
        "%s\n\ncloses #2063", "%s\nResolved #2063.",
    ])
    def test_a_body_already_strong_in_any_scan_form_is_untouched(self, af, present):
        """Idempotence by the READER's grammar: whichever of the scan's forms is present, no
        write is issued and the body is byte-identical afterwards."""
        body = present % RENOVATE_BODY
        gh = RestWorld(body)
        stats = {}
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", stats)
        assert gh.patches == []
        assert gh.body == body
        assert "issue_link_added_by_pod" not in stats

    def test_refs_does_not_satisfy_the_guarantee(self, af):
        """`Refs #N` is exactly the weak form the class is about — the write must still happen,
        and the `Refs` line is left where it was."""
        body = RENOVATE_BODY + "\n\nRefs #2063\n"
        gh = RestWorld(body)
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", {})
        assert gh.patches == ["Fixes #2063\n\n" + body]

    def test_a_worker_opened_pr_carrying_its_trailer_is_untouched(self, af):
        """The PR the worker opened itself: its body carries the strong link by the open-time
        contract (`Fixes #N` from the recipe, or #32's own earlier prepend), so round 2 issues no
        write."""
        body = "Fixes #73\n\nWhat changed and why.\n\n<!-- agent-touches: begin -->\n" \
               "Touches-escapes: none\n<!-- agent-touches: end -->\n"
        gh = RestWorld(body, pr_author="app/homelab-agents-1234", number=74)
        stats = {}
        af.ensure_strong_link(gh, "o/r", "https://github.com/o/r/pull/74", "73", stats)
        assert gh.patches == []
        assert gh.body == body
        assert "issue_link_added_by_pod" not in stats

    def test_a_second_round_writes_nothing(self, af):
        """Round 1 writes, round 2 finds its own line by the scan's grammar and stops."""
        gh = RestWorld(RENOVATE_BODY)
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", {})
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", {})
        assert len(gh.patches) == 1

    def test_rest_only_no_graphql_verb_is_issued(self, af):
        """The transport pin: not one `pr view` / `pr edit` / `repo view` — every read and the
        write go through `gh api` (REST)."""
        gh = RestWorld(RENOVATE_BODY)
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", {})
        assert gh.graphql_calls() == []
        assert gh.pr_edits() == []
        assert gh.patches == ["Fixes #2063\n\n" + RENOVATE_BODY]

    def test_the_link_lands_while_the_graphql_pool_is_exhausted(self, af):
        """The live failure, replayed: every GraphQL verb answers the 09:57:25Z error. The
        guarantee must not notice."""
        gh = RestWorld(RENOVATE_BODY, graphql_down=True)
        stats = {}
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", stats)
        assert gh.patches == ["Fixes #2063\n\n" + RENOVATE_BODY]
        assert stats.get("issue_link_added_by_pod") is True

    def test_an_unreadable_pr_writes_nothing(self, af):
        """Rule #6: a failed read is not an empty body — prepending onto a body this code never
        saw would rewrite the rest. No write, no flag."""
        gh = RestWorld(RENOVATE_BODY, fail_read=True)
        stats = {}
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", stats)
        assert gh.patches == []
        assert "issue_link_added_by_pod" not in stats

    def test_a_refused_write_leaves_the_flag_unset(self, af):
        gh = RestWorld(RENOVATE_BODY, fail_patch=True)
        stats = {}
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", stats)
        assert gh.body == RENOVATE_BODY
        assert "issue_link_added_by_pod" not in stats

    def test_a_non_object_payload_is_unreadable_not_empty(self, af):
        """rc 0 with a proxy page or a JSON array is no reading at all."""
        class _Garbage(RestWorld):
            def __call__(self, *args, stdin=None, timeout=30):
                if args[0] == "api" and "--method" not in args and args[1].endswith("/pulls/2046"):
                    return _Done(0, "[]")
                return super().__call__(*args, stdin=stdin, timeout=timeout)
        gh = _Garbage(RENOVATE_BODY)
        af.ensure_strong_link(gh, "o/r", self.URL, "2063", {})
        assert gh.patches == []

    def test_closing_keywords_aimed_at_prs_ride_the_same_single_patch(self, af):
        """#107 lint kept, on REST: `Fixes #2046` (a PR) → `Refs #2046`; the probe is
        `repos/o/r/pulls/2046 --jq .number`, and the rewrite and the link land in ONE write."""
        body = "Adaptation for the bump in #2046.\n\nFixes #2046\n"
        gh = RestWorld(body, number=2070, pr_numbers=(2046,))
        af.ensure_strong_link(gh, "o/r", "https://github.com/o/r/pull/2070", "2063", {})
        assert gh.patches == ["Fixes #2063\n\nAdaptation for the bump in #2046.\n\nRefs #2046\n"]
        probes = [c for (c, _s) in gh.calls if c[:2] == ("api", "repos/o/r/pulls/2046")]
        assert len(probes) == 1 and ".number" in probes[0]

    def test_no_pr_number_or_slug_means_no_call_at_all(self, af):
        gh = RestWorld(RENOVATE_BODY)
        af.ensure_strong_link(gh, "", self.URL, "2063", {})
        af.ensure_strong_link(gh, "o/r", "not-a-pr-url", "2063", {})
        af.ensure_strong_link(gh, "o/r", self.URL, "issue-2063", {})
        assert gh.calls == []


class TestBookkeepingWiresTheGuarantee:
    """The seam: `bookkeeping()` reaches the guarantee on the live shape — an `issue-N` task, a
    PR the worker did not open, `AGENT_ARM_PR=0` (the launcher derived --no-arm for the un-armed
    `major`), `ci-failed` (an ARM-route exit, the round ended) — and the link lands even with the
    GraphQL pool gone. Also: the flag rides the AGENT_RUN_STATS dict the launcher reads."""

    def _run(self, af, monkeypatch, logfile, world, exit_status="ci-failed", task="issue-2063"):
        def _fake_run(argv, **kw):
            if argv[0] == "git":
                # `_changed_paths` (touches block) — an unreadable diff skips that block, which is
                # not what this suite is about.
                return _Done(1, "", "fatal: bad revision\n")
            assert argv[0] == "gh"
            return world(*argv[1:], stdin=kw.get("input"))

        monkeypatch.setattr(af.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        monkeypatch.setattr(af, "_fresh_gh_env", lambda: {})
        monkeypatch.setenv("AGENT_TASK", task)
        monkeypatch.setenv("REPO_URL", "https://github.com/o/r")
        monkeypatch.setenv("AGENT_ARM_PR", "0")
        monkeypatch.setenv("MODEL", "deepseek/deepseek-v4.1-flash:exacto")
        monkeypatch.setenv("AGENT_ROUND", "1")
        stats = {"pr_url": "https://github.com/o/r/pull/2046", "exit_status": exit_status,
                 "error_class": "ci-red" if exit_status == "ci-failed" else "", "pod": "p"}
        af.bookkeeping(stats, logfile("ci red on master's own lint\n"))
        return stats

    def test_the_live_ride_replayed_lands_the_link(self, af, monkeypatch, logfile):
        world = RestWorld(RENOVATE_BODY, graphql_down=True)
        stats = self._run(af, monkeypatch, logfile, world)
        assert world.patches == ["Fixes #2063\n\n" + RENOVATE_BODY]
        assert stats.get("issue_link_added_by_pod") is True
        # --no-arm held: the human-merge lane is not armed by this leg either.
        assert stats.get("armed_by_pod") is False

    def test_a_died_round_holding_the_foreign_pr_still_links_it(self, af, monkeypatch, logfile):
        """#32 on the strike leg (#49's table: issue link = yes on both PR rows)."""
        world = RestWorld(RENOVATE_BODY)
        stats = self._run(af, monkeypatch, logfile, world, exit_status="harness-death")
        assert world.patches == ["Fixes #2063\n\n" + RENOVATE_BODY]
        assert stats.get("strike_by_pod") is True

    def test_an_already_linked_body_is_untouched_end_to_end(self, af, monkeypatch, logfile):
        body = RENOVATE_BODY + "\n\nFixes #2063\n"
        world = RestWorld(body)
        stats = self._run(af, monkeypatch, logfile, world)
        assert world.patches == []
        assert world.body == body
        assert "issue_link_added_by_pod" not in stats

    def test_a_non_issue_task_writes_no_link(self, af, monkeypatch, logfile):
        """A `pr-N` ride (the reviewer's shape) has no carrier issue to link."""
        world = RestWorld(RENOVATE_BODY)
        self._run(af, monkeypatch, logfile, world, task="pr-2046")
        assert world.patches == []
