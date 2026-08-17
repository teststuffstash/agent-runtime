"""The deterministic Touches-escapes block (agent-runtime#70 / homelab#379 worker half).

The worker's JUSTIFICATION of each escape stays a recipe instruction; the LIST is finalize's fact.
`agent-finalize` computes the ride's escape set — changed paths (`git diff --name-only
<base>..HEAD`) vs the issue's declared `Touches:` footprint, prefix-intersection with ADR-097
semantics (the canonical matcher is homelab `agents/touches-check.sh` / `footprint.sh`, mirrored
here) — and appends one machine-readable block to the PR body it opens/edits:

    Touches-escapes: none
    Touches-escapes: <comma-separated paths>
    Touches: undeclared

Deterministic and model-independent: the escape list is computed, never narrated. The append is
edit-in-place and idempotent across rounds — round 2 rewrites round 1's block, it does not stack a
second one — the same discipline as the `<!-- agent-summary -->` comment. An escaped path in the
governance tier (`agents/*`, `.agents/*`, `scripts/*`, `policy/*`, `.github/*`, `tofu/github/*`,
`tofu/cloudflare/*`) rides as `path|governance`, so the homelab reviewer's BLOCKING finding for it
is not silently dropped. And the `Touches:` line comes from the ISSUE body, which is absent on
plenty of issues — absent must land as `Touches: undeclared`, never a crash and never `none`.

Hermetic: every decision is a pure function over (issue body, changed paths) — no gh, no git, no
network. The wiring test stubs `subprocess.run` and records argv, exactly like
`test_summary_channel.py`'s FakeGitHub.
"""
import json

import pytest


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGH:
    """In-memory GitHub for the wiring tests: an issue-body store, a mutable PR body, and the
    ADR-103 endpoints bookkeeping reaches (check-run, summary-comment). Records every argv."""

    def __init__(self, issue_body="Touches: tests/\n", pr_body="Fixes #70\n"):
        self.issue_body = issue_body
        self.pr_body = pr_body
        self.comments = []
        self.calls = []
        self.check_runs = []

    def __call__(self, *args, stdin=None, timeout=30):
        self.calls.append((tuple(args), stdin))
        argv = list(args)
        if argv[:2] == ["issue", "view"]:
            return _Done(0, self.issue_body)
        if argv[:2] == ["pr", "view"]:
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
    `_resolve_branch` needs. `fail=True` makes the DIFF unreadable (never the rev-parse)."""

    def __init__(self, changed=("tests/test_a.py",), wd="/work/repo", fail=False):
        self.changed = list(changed)
        self.wd = wd
        self.fail = fail

    def __call__(self, argv):
        if "rev-parse" in argv:
            return _Done(0, "fix/issue-70-touches-escapes\n")
        if argv[:5] == ["git", "-C", self.wd, "diff", "--name-only"]:
            if self.fail:
                return _Done(1, "", "fatal: bad revision\n")
            return _Done(0, "\n".join(self.changed) + ("\n" if self.changed else ""))
        raise AssertionError("unexpected git argv: %r" % (argv,))


class TestParseTouchesFootprint:
    """`parse_touches_footprint(issue_body)` — the declared footprint, or None when undeclared."""

    def test_absent_line_is_undeclared(self, af):
        assert af.parse_touches_footprint("Fix the bug.\n\n**Deliverable**: a thing\n") is None

    def test_no_body_is_undeclared(self, af):
        assert af.parse_touches_footprint(None) is None
        assert af.parse_touches_footprint("") is None

    def test_empty_value_is_undeclared(self, af):
        assert af.parse_touches_footprint("Touches:\n") is None
        assert af.parse_touches_footprint("Touches:   \n") is None

    def test_bare_star_is_undeclared(self, af):
        """`Touches: *` declares no reviewable footprint — every changed path escapes."""
        assert af.parse_touches_footprint("Touches: *\n") is None

    def test_entries_are_comma_separated_and_whitespace_stripped(self, af):
        assert af.parse_touches_footprint("Touches: agents/*, tests/\n") == ["agents", "tests"]

    def test_a_glob_cuts_at_the_first_star(self, af):
        """ADR-097 normalization: `chassis/**` → `chassis/` → `chassis`."""
        assert af.parse_touches_footprint("Touches: chassis/**\n") == ["chassis"]

    def test_a_leading_star_entry_matches_everything(self, af):
        """`**/x.py` cuts to the EMPTY prefix — conservative by construction."""
        assert af.parse_touches_footprint("Touches: **/x.py\n") == [""]

    def test_empty_entries_between_commas_are_dropped(self, af):
        assert af.parse_touches_footprint("Touches: agents/*, , tests/\n") == ["agents", "tests"]

    def test_the_line_match_is_case_insensitive(self, af):
        assert af.parse_touches_footprint("touches: tests/\n") == ["tests"]

    def test_a_mid_line_mention_is_not_a_declaration(self, af):
        """Only a line that STARTS with `Touches:` declares a footprint."""
        body = "The footprint is declared via a Touches: line in the issue body.\n"
        assert af.parse_touches_footprint(body) is None


class TestFootprintOverlap:
    """`footprint_overlap(a, b)` — path-boundary-aware, not raw string-prefix (ADR-097)."""

    def test_identical_prefixes_overlap(self, af):
        assert af.footprint_overlap("chassis", "chassis") is True

    def test_a_path_under_the_prefix_overlaps(self, af):
        assert af.footprint_overlap("chassis/api.py", "chassis") is True
        assert af.footprint_overlap("chassis", "chassis/api.py") is True

    def test_a_sibling_path_does_not_overlap(self, af):
        """`chassis` ∩ `chassis-x`: raw string-prefix would say overlap, the boundary says no."""
        assert af.footprint_overlap("chassis-x", "chassis") is False

    def test_an_empty_side_matches_everything(self, af):
        """A leading-`*` entry normalizes to the empty prefix — overlap with everything."""
        assert af.footprint_overlap("chassis/api.py", "") is True
        assert af.footprint_overlap("", "chassis/api.py") is True


class TestTouchesEscapes:
    """`touches_escapes(footprint, changed_paths)` → (undeclared, escapes)."""

    def test_undeclared_means_every_path_escapes(self, af):
        undeclared, escapes = af.touches_escapes(None, ["a.py", "b.py"])
        assert undeclared is True
        assert escapes == ["a.py", "b.py"]

    def test_declared_and_all_overlapping_is_no_escape(self, af):
        undeclared, escapes = af.touches_escapes(["tests"], ["tests/test_a.py", "tests/b.py"])
        assert (undeclared, escapes) == (False, [])

    def test_a_path_outside_the_footprint_escapes(self, af):
        undeclared, escapes = af.touches_escapes(
            ["tests"], ["agent-base/agent-finalize", "tests/test_a.py"])
        assert (undeclared, escapes) == (False, ["agent-base/agent-finalize"])

    def test_no_changed_paths_means_no_escapes(self, af):
        assert af.touches_escapes(["tests"], []) == (False, [])


class TestTouchesBlock:
    """`touches_block(issue_body, changed_paths)` — the three emitted shapes, delimited."""

    def test_undeclared(self, af):
        block = af.touches_block("no touches line here\n", ["a.py"])
        assert "Touches: undeclared" in block
        assert "Touches-escapes" not in block

    def test_no_escapes(self, af):
        block = af.touches_block("Touches: tests/\n", ["tests/test_a.py"])
        assert "Touches-escapes: none" in block

    def test_escapes_are_comma_separated_in_git_order(self, af):
        block = af.touches_block("Touches: tests/\n",
                                 ["agent-base/agent-finalize", ".github/workflows/ci.yaml",
                                  "tests/test_a.py"])
        assert ("Touches-escapes: agent-base/agent-finalize,"
                ".github/workflows/ci.yaml|governance" in block)

    def test_a_governance_path_is_marked(self, af):
        """`.github/*` etc. are BLOCKING findings for the homelab reviewer — the block must not
        silently drop the distinction."""
        block = af.touches_block("Touches: tests/\n", [".github/workflows/ci.yaml"])
        assert "Touches-escapes: .github/workflows/ci.yaml|governance" in block

    def test_a_non_governance_path_is_not_marked(self, af):
        block = af.touches_block("Touches: tests/\n", ["agent-base/agent-finalize"])
        assert "Touches-escapes: agent-base/agent-finalize" in block
        assert "agent-base/agent-finalize|governance" not in block

    def test_the_block_is_delimited_by_the_markers(self, af):
        block = af.touches_block("Touches: tests/\n", ["tests/test_a.py"])
        assert block.startswith(af.TOUCHES_BLOCK_BEGIN)
        assert block.endswith(af.TOUCHES_BLOCK_END)


class TestAppendTouchesBlock:
    """`append_touches_block(body, block)` — the edit-in-place, idempotent append."""

    BLOCK = "<!-- agent-touches: begin -->\nTouches-escapes: none\n<!-- agent-touches: end -->"

    def test_first_append_on_an_empty_body(self, af):
        assert af.append_touches_block("", self.BLOCK) == self.BLOCK

    def test_first_append_joins_the_existing_body(self, af):
        assert af.append_touches_block("Fixes #70\n", self.BLOCK) == "Fixes #70\n\n" + self.BLOCK

    def test_a_second_append_replaces_not_stacks(self, af):
        """Round 2 rewrites round 1's block — the marker appears exactly once."""
        first = af.append_touches_block("Fixes #70\n", self.BLOCK)
        second = af.touches_block("Touches: tests/\n", ["agent-base/agent-finalize"])
        out = af.append_touches_block(first, second)
        assert out.count(af.TOUCHES_BLOCK_BEGIN) == 1
        assert "Touches-escapes: agent-base/agent-finalize" in out
        assert "Touches-escapes: none" not in out


class TestPostTouchesBlockWiring:
    """`post_touches_block(gh, slug, pr_url, issue)` — issue body in, PR body block out."""

    def _install(self, af, monkeypatch, gh, git):
        calls = []

        def _fake_run(argv, **kw):
            calls.append(tuple(argv))
            if argv[0] == "git":
                return git(argv)
            assert argv[0] == "gh"
            return gh(*argv[1:], stdin=kw.get("input"))

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        return calls

    def test_it_appends_the_block_to_the_pr_body(self, af, monkeypatch):
        gh = FakeGH(issue_body="Touches: tests/\n", pr_body="Fixes #70\n")
        git = FakeGit(changed=("agent-base/agent-finalize", "tests/test_a.py"))
        self._install(af, monkeypatch, gh, git)
        af.post_touches_block(gh, "o/r", "https://github.com/o/r/pull/70", "70")
        assert "Touches-escapes: agent-base/agent-finalize" in gh.pr_body
        assert gh.pr_body.count(af.TOUCHES_BLOCK_BEGIN) == 1

    def test_undeclared_lands_as_undeclared(self, af, monkeypatch):
        gh = FakeGH(issue_body="no touches line here\n", pr_body="Fixes #70\n")
        self._install(af, monkeypatch, gh, FakeGit())
        af.post_touches_block(gh, "o/r", "https://github.com/o/r/pull/70", "70")
        assert "Touches: undeclared" in gh.pr_body

    def test_it_is_idempotent_across_rounds(self, af, monkeypatch):
        """Round 2 (the change set grew) REWRITES round 1's block — one block, new fact."""
        gh = FakeGH(issue_body="Touches: tests/\n", pr_body="Fixes #70\n")
        git = FakeGit(changed=("tests/test_a.py",))
        self._install(af, monkeypatch, gh, git)
        af.post_touches_block(gh, "o/r", "https://github.com/o/r/pull/70", "70")
        assert "Touches-escapes: none" in gh.pr_body
        git.changed = ("tests/test_a.py", "agent-base/agent-finalize")
        af.post_touches_block(gh, "o/r", "https://github.com/o/r/pull/70", "70")
        assert gh.pr_body.count(af.TOUCHES_BLOCK_BEGIN) == 1
        assert "Touches-escapes: agent-base/agent-finalize" in gh.pr_body
        assert "Touches-escapes: none" not in gh.pr_body

    def test_an_unreadable_diff_skips_not_fabricates(self, af, monkeypatch):
        """Rule #6: a git failure is not "no changes" — that would emit `Touches-escapes: none`
        for a change set finalize cannot see."""
        gh = FakeGH()
        self._install(af, monkeypatch, gh, FakeGit(fail=True))
        af.post_touches_block(gh, "o/r", "https://github.com/o/r/pull/70", "70")
        assert "Touches" not in gh.pr_body


class TestBookkeepingWiresTheBlock:
    """The seam: `bookkeeping()`'s issue leg calls `post_touches_block` on a real PR."""

    def _run(self, af, monkeypatch, logfile, gh, git):
        def _fake_run(argv, **kw):
            if argv[0] == "git":
                return git(argv)
            assert argv[0] == "gh"
            return gh(*argv[1:], stdin=kw.get("input"))

        monkeypatch.setattr(af.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        monkeypatch.setattr(af, "_fresh_gh_env", lambda: {})
        monkeypatch.setenv("AGENT_TASK", "issue-70")
        monkeypatch.setenv("REPO_URL", "https://github.com/o/r")
        monkeypatch.setenv("MODEL", "some/model")
        monkeypatch.setenv("AGENT_ROUND", "1")
        stats = {"pr_url": "https://github.com/o/r/pull/70", "exit_status": "clean", "pod": "p"}
        af.bookkeeping(stats, logfile("all fine\n"))
        return stats

    def test_a_clean_issue_round_ends_with_the_block_on_the_pr(self, af, monkeypatch, logfile):
        gh = FakeGH(issue_body="Touches: tests/\n", pr_body="Fixes #70\n")
        git = FakeGit(changed=("agent-base/agent-finalize",))
        self._run(af, monkeypatch, logfile, gh, git)
        assert gh.pr_body.count(af.TOUCHES_BLOCK_BEGIN) == 1
        assert "Touches-escapes: agent-base/agent-finalize" in gh.pr_body

    def test_an_issue_without_a_touches_line_lands_undeclared(self, af, monkeypatch, logfile):
        gh = FakeGH(issue_body="Fix the bug.\n", pr_body="Fixes #70\n")
        self._run(af, monkeypatch, logfile, gh, FakeGit(changed=("agent-base/agent-finalize",)))
        assert "Touches: undeclared" in gh.pr_body

    def test_two_rounds_leave_exactly_one_block(self, af, monkeypatch, logfile):
        """End to end across the resume: the block is rewritten, never stacked."""
        gh = FakeGH(issue_body="Touches: tests/\n", pr_body="Fixes #70\n")
        git = FakeGit(changed=("tests/test_a.py",))
        self._run(af, monkeypatch, logfile, gh, git)
        git.changed = ("tests/test_a.py", "agent-base/agent-finalize")
        self._run(af, monkeypatch, logfile, gh, git)
        assert gh.pr_body.count(af.TOUCHES_BLOCK_BEGIN) == 1
        assert "Touches-escapes: agent-base/agent-finalize" in gh.pr_body
        assert "Touches-escapes: none" not in gh.pr_body
