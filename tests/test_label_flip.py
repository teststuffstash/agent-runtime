"""The PR-open label flip (agent-runtime#73 / homelab#501 cause half).

At PR-open, `agent-finalize` flips the source issue out of `agent/in-progress` and into
`agent/review` — the deterministic lifecycle edge (homelab#501 direction (b)): today NOTHING writes
`agent/review`; it happens only when an LLM session happens to visit, so footprint release is
arbitrary. The flip rides the ARM leg beside auto-merge arming, reads the issue number from the
STRONG LINK finalize guarantees in the PR body (`Implements #N` / `Fixes #N` — agent-runtime#34),
and obeys homelab IL-T16's ordering discipline: `agent/review` is ADDED first, `agent/in-progress`
is REMOVED second (the write is not atomic, and removing first would leave the issue label-less in
the reconciliation window). Idempotent (gh's edit is a no-op for a label already in the target
state — the homelab scan belt runs the same flip level-triggered and either side may win the race)
and never fatal (a refused write is a stderr line; the belt reconciles within a scan tick,
homelab#501 deliverable 1).

Hermetic: `parse_strong_link` is a pure function; `flip_issue_to_review`'s wiring stubs
`subprocess.run` and records argv, exactly like `test_touches_block.py`'s FakeGH.
"""
import json


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGH:
    """In-memory GitHub for the flip wiring: a mutable PR body, a label-edit recorder, and the
    ADR-103 endpoints bookkeeping reaches (check-run, summary comment). `fail_*` make the
    corresponding write refuse, to pin the ordering discipline."""

    def __init__(self, issue_body="Touches: tests/\n", pr_body="Fixes #73\n",
                 fail_body_read=False, fail_add=False, fail_remove=False):
        self.issue_body = issue_body
        self.pr_body = pr_body
        self.fail_body_read = fail_body_read
        self.fail_add = fail_add
        self.fail_remove = fail_remove
        self.comments = []
        self.calls = []
        self.check_runs = []
        self.label_edits = []  # ("add" | "remove", issue, label) in call order

    def __call__(self, *args, stdin=None, timeout=30):
        self.calls.append((tuple(args), stdin))
        argv = list(args)
        if argv[:2] == ["issue", "view"]:
            return _Done(0, self.issue_body)
        if argv[:2] == ["issue", "edit"]:
            issue = argv[2]
            verb = "add" if "--add-label" in argv else "remove"
            label = (argv[argv.index("--add-label") + 1] if "--add-label" in argv
                     else argv[argv.index("--remove-label") + 1])
            self.label_edits.append((verb, issue, label))
            if verb == "add" and self.fail_add:
                return _Done(1, "", "Label 'agent/review' does not exist\n")
            if verb == "remove" and self.fail_remove:
                return _Done(1, "", "Label 'agent/in-progress' does not exist\n")
            return _Done(0)
        if argv[:2] == ["pr", "view"]:
            if self.fail_body_read:
                return _Done(1, "", "fatal: repository not found\n")
            if "headRefOid" in argv:
                return _Done(0, "deadbee\n")
            return _Done(0, self.pr_body)
        if argv[:2] == ["pr", "edit"]:
            self.pr_body = argv[argv.index("--body") + 1]
            return _Done(0)
        if argv[:2] == ["pr", "merge"]:
            return _Done(0)
        if argv[0] != "api":
            return _Done(0)
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        path = [a for a in argv[1:] if not a.startswith("-") and a != method][0]
        body = (json.loads(stdin) if stdin else {}).get("body")
        if path.endswith("/check-runs"):
            self.check_runs.append(json.loads(stdin))
            return _Done(0, json.dumps({"id": 7}))
        if method == "GET":
            return _Done(0, json.dumps(self.comments))
        if method == "POST":
            self.comments.append(body)
            return _Done(0, json.dumps({"id": 1, "created_at": "t", "body": body}))
        return _Done(0)


class FakeGit:
    """In-memory git for the wiring tests: the changed-paths diff plus the rev-parse seam
    `_resolve_branch` needs."""

    def __init__(self, changed=("tests/test_a.py",), wd="/work/repo"):
        self.changed = list(changed)
        self.wd = wd

    def __call__(self, argv):
        if "rev-parse" in argv:
            return _Done(0, "fix/issue-73-review-flip\n")
        if argv[:5] == ["git", "-C", self.wd, "diff", "--name-only"]:
            return _Done(0, "\n".join(self.changed) + ("\n" if self.changed else ""))
        raise AssertionError("unexpected git argv: %r" % (argv,))


class TestParseStrongLink:
    """`parse_strong_link(body)` — the issue number in the strong link, or None."""

    def test_fixes_is_strong(self, af):
        assert af.parse_strong_link("Fixes #73\n") == "73"

    def test_implements_is_strong(self, af):
        assert af.parse_strong_link("Implements #73\n\nFix the bug.\n") == "73"

    def test_the_first_strong_link_wins(self, af):
        """The #32 guarantee PREPENDS `Implements #N`; a recipe body may also cite a sibling."""
        body = "Implements #73\n\nAlso fixes #12.\n"
        assert af.parse_strong_link(body) == "73"

    def test_a_mid_body_strong_link_is_found(self, af):
        assert af.parse_strong_link("Some prose\n\nImplements #7 here.\n") == "7"

    def test_refs_is_not_strong(self, af):
        """`Refs #N` declares a PARTIAL delivery — a weak link, nothing deterministic to flip."""
        assert af.parse_strong_link("Refs #73 — one item scoped out.\n") is None

    def test_a_bare_mention_is_not_a_link(self, af):
        assert af.parse_strong_link("Close #73, please.\n") is None

    def test_no_body_is_none(self, af):
        assert af.parse_strong_link("") is None
        assert af.parse_strong_link(None) is None

    def test_the_capture_is_the_whole_number(self, af):
        """`#73` must not bleed into `#731`: the capture is the WHOLE run of digits."""
        assert af.parse_strong_link("Fixes #731\n") == "731"
        assert af.parse_strong_link("Fixes #731\n") != "73"


class TestFlipIssueToReview:
    """`flip_issue_to_review(gh, slug, pr_url)` — read the strong link, flip in IL-T16 order."""

    def test_it_adds_review_then_removes_in_progress(self, af, monkeypatch):
        gh = FakeGH(pr_body="Fixes #73\n")
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == [
            ("add", "73", "agent/review"),
            ("remove", "73", "agent/in-progress"),
        ]

    def test_it_flips_an_implements_link(self, af, monkeypatch):
        gh = FakeGH(pr_body="Implements #7\n\nBody.\n")
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == [
            ("add", "7", "agent/review"),
            ("remove", "7", "agent/in-progress"),
        ]

    def test_no_strong_link_does_nothing(self, af, monkeypatch):
        gh = FakeGH(pr_body="Refs #73 — partial delivery.\n")
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == []

    def test_an_unreadable_body_does_nothing(self, af, monkeypatch):
        gh = FakeGH(pr_body="Fixes #73\n", fail_body_read=True)
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == []

    def test_a_failed_add_skips_the_remove(self, af, monkeypatch):
        """IL-T16: the add comes FIRST, so a failed add must NOT be followed by the remove — that
        would leave the issue label-less (neither in-progress nor review) in the reconciliation
        window. The belt reconciles the whole flip instead."""
        gh = FakeGH(pr_body="Fixes #73\n", fail_add=True)
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == [("add", "73", "agent/review")]

    def test_a_failed_remove_still_keeps_the_review_label(self, af, monkeypatch):
        gh = FakeGH(pr_body="Fixes #73\n", fail_remove=True)
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == [
            ("add", "73", "agent/review"),
            ("remove", "73", "agent/in-progress"),
        ]

    def test_idempotent_across_rounds(self, af, monkeypatch):
        """Round 2 flips again: gh's edit is a no-op for a label already in the target state, so
        the already-flipped issue is tolerated — the same level-triggered flip the belt runs."""
        gh = FakeGH(pr_body="Fixes #73\n")
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        af.flip_issue_to_review(gh, "o/r", "https://github.com/o/r/pull/1")
        assert gh.label_edits == [
            ("add", "73", "agent/review"), ("remove", "73", "agent/in-progress"),
            ("add", "73", "agent/review"), ("remove", "73", "agent/in-progress"),
        ]


class TestBookkeepingWiresTheFlip:
    """The seam: `bookkeeping()`'s ARM leg flips on a real PR; a died round holding an earlier
    round's PR does NOT flip (#49 — the strike leg owns that issue)."""

    def _run(self, af, monkeypatch, logfile, gh, git, exit_status="clean", error_class=""):
        def _fake_run(argv, **kw):
            if argv[0] == "git":
                return git(argv)
            assert argv[0] == "gh"
            return gh(*argv[1:], stdin=kw.get("input"))

        monkeypatch.setattr(af.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        monkeypatch.setattr(af, "_fresh_gh_env", lambda: {})
        monkeypatch.setenv("AGENT_TASK", "issue-73")
        monkeypatch.setenv("REPO_URL", "https://github.com/o/r")
        monkeypatch.setenv("MODEL", "some/model")
        monkeypatch.setenv("AGENT_ROUND", "1")
        stats = {"pr_url": "https://github.com/o/r/pull/73", "exit_status": exit_status,
                 "error_class": error_class, "pod": "p"}
        af.bookkeeping(stats, logfile("all fine\n"))
        return stats

    def test_a_clean_issue_round_flips_the_issue(self, af, monkeypatch, logfile):
        gh = FakeGH(pr_body="Fixes #73\n")
        self._run(af, monkeypatch, logfile, gh, FakeGit())
        assert ("add", "73", "agent/review") in gh.label_edits
        assert gh.label_edits.index(("add", "73", "agent/review")) < \
            gh.label_edits.index(("remove", "73", "agent/in-progress"))

    def test_the_flip_consumes_the_strong_link_the_guarantee_maintains(self, af, monkeypatch,
                                                                      logfile):
        """The flip reads the PR body's strong link — never the task string (agent-runtime#34).
        The #32 step runs first and PREPENDS `Implements #N` when the body never named its issue, so
        even a recipe body that claims a sibling (`Fixes #42`) flips the issue the guarantee made
        strong — the source issue the coordinator scan will actually release."""
        gh = FakeGH(pr_body="Fixes #42\n")
        self._run(af, monkeypatch, logfile, gh, FakeGit())
        assert ("add", "73", "agent/review") in gh.label_edits
        assert not [e for e in gh.label_edits if e[1] == "42"]

    def test_a_body_with_only_refs_gets_the_strong_link_prepended_and_flips(self, af, monkeypatch,
                                                                           logfile):
        """#87: `Refs #N` is a weak mention — the #32 guarantee prepends `Implements #N`, and
        then the flip reads the strong link and flips the label."""
        gh = FakeGH(pr_body="Refs #73 — partial delivery.\n")
        self._run(af, monkeypatch, logfile, gh, FakeGit())
        assert ("add", "73", "agent/review") in gh.label_edits

    def test_a_died_round_holding_an_earlier_pr_does_not_flip(self, af, monkeypatch, logfile):
        """The flip rides the ARM leg like the touches block and stats channel: a died round's
        strike leg owns the issue, and #49 pins its exact label writes (zero)."""
        gh = FakeGH(pr_body="Fixes #73\n")
        self._run(af, monkeypatch, logfile, gh, FakeGit(),
                  exit_status="harness-death", error_class="goose-panic")
        assert gh.label_edits == []
