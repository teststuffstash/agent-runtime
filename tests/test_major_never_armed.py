"""Finalize never arms a `major` PR (homelab S9 #1987).

The evidence: sleep-iac#90, a Renovate `major` bump, was ARMED by a fix-round worker's finalize
(`bookkeeping: auto-merge armed on …` in its log) and merged with ZERO review, because `bookkeeping`
armed every PR it held unless `AGENT_ARM_PR=0` or the round died. The `major` label is the
human-merge lane marker (the reviewer's migration evidence + `major/awaiting-human` + a human merge
— homelab `agents/major-handoff.sh`), so the label is what finalize now reads before the arm.

The rule (the contract source is `pr_major_status`'s docstring + homelab#1987):
  - `major` present            → not armed, `armed_by_pod` is FALSE (not unset — the launcher's
                                  fallback arms whatever the pod left UNKNOWN), one loud line.
  - labels probe unreadable    → not armed, FALSE, its own loud line. Fail-CLOSED: arming is the
                                  write; platform rule #6 is never to fail INTO a write.
  - no `major`, probe readable → armed exactly as before.

Hermetic like the sibling suites: `subprocess.run` is stubbed to `test_label_flip.FakeGH`, which
serves the `--json labels` probe from a constructor list and can refuse that ONE probe while the
body read keeps working (so the unreadable case pins the LABELS read, not any read).
"""
from test_label_flip import FakeGH, FakeGit


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class TestPrMajorStatus:
    """`pr_major_status(gh, pr_url)` — the pure lane read: "major" | "clear" | "unreadable"."""

    def test_major_label_is_major(self, af):
        gh = FakeGH(labels=["dependencies", "major"])
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/90") == "major"

    def test_no_labels_is_clear(self, af):
        gh = FakeGH(labels=[])
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/1") == "clear"

    def test_other_labels_are_clear(self, af):
        """`major/awaiting-human` is the HANDOFF's label, set after the evidence — it is not the
        lane marker Renovate puts on the PR, and a prefix match would read every handed-off PR
        as `major` twice over. Exact name only."""
        gh = FakeGH(labels=["dependencies", "minor", "major/awaiting-human"])
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/1") == "clear"

    def test_a_refused_probe_is_unreadable(self, af):
        gh = FakeGH(labels=["major"], fail_labels_read=True)
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/90") == "unreadable"

    def test_garbage_stdout_is_unreadable(self, af):
        """rc 0 with a non-JSON body (a proxy error page, a truncated stream) is NOT "clear" —
        "clear" is a positive reading of an empty list, never the absence of a reading."""
        def gh(*args, stdin=None, timeout=30):
            return _Done(0, "<html>502</html>")
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/90") == "unreadable"

    def test_empty_or_keyless_stdout_is_unreadable(self, af):
        """rc 0 with EMPTY stdout is the stub's `empty` mode, not a reading (the upstream fakes
        answer unknown reads with an empty _Done()) — and an object without `labels` is no
        reading either. Both are "unreadable", never "clear" — clear is never the default."""
        def empty(*args, stdin=None, timeout=30):
            return _Done(0, "")
        def keyless(*args, stdin=None, timeout=30):
            return _Done(0, '{"number": 90}')
        assert af.pr_major_status(empty, "https://github.com/o/r/pull/90") == "unreadable"
        assert af.pr_major_status(keyless, "https://github.com/o/r/pull/90") == "unreadable"

    def test_the_probe_reads_labels_not_the_body(self, af):
        """A PR BODY may say the word (`Upstream: v3 is a major …`); the lane marker is the label
        list alone, so the probe must ask for `labels` and nothing else."""
        gh = FakeGH(labels=[], pr_body="This is a major refactor.\n")
        assert af.pr_major_status(gh, "https://github.com/o/r/pull/1") == "clear"
        probes = [c for c, _ in gh.calls if c[:2] == ("pr", "view")]
        assert probes == [("pr", "view", "https://github.com/o/r/pull/1", "--json", "labels")]


class TestBookkeepingNeverArmsMajor:
    """The seam: `bookkeeping()`'s ARM leg consults the lane read before `gh pr merge --auto`."""

    def _run(self, af, monkeypatch, logfile, gh, capsys):
        git = FakeGit()

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
        monkeypatch.setenv("AGENT_ROUND", "2")
        # A CLEAN round: the route is "arm", so the ONLY thing standing between this PR and
        # `gh pr merge --auto` is the lane read under test (AGENT_ARM_PR is unset → "1").
        stats = {"pr_url": "https://github.com/o/r/pull/90", "exit_status": "clean",
                 "error_class": "", "pod": "p"}
        af.bookkeeping(stats, logfile("all fine\n"))
        return stats, capsys.readouterr().out

    @staticmethod
    def _merges(gh):
        return [c for c, _ in gh.calls if c[:2] == ("pr", "merge")]

    def test_a_major_pr_is_not_armed(self, af, monkeypatch, logfile, capsys):
        """sleep-iac#90's shape, replayed: clean round, PR carries `major` → zero `pr merge`
        calls, the flag FALSE (the launcher fallback must not undo the decision), the loud line."""
        gh = FakeGH(pr_body="Fixes #73\n", labels=["dependencies", "major"])
        stats, out = self._run(af, monkeypatch, logfile, gh, capsys)
        assert self._merges(gh) == []
        assert stats.get("armed_by_pod") is False
        assert ("bookkeeping: arming SKIPPED (PR carries `major` — human-merge lane, "
                "homelab S9 #1987)") in out.splitlines()

    def test_unreadable_labels_do_not_arm(self, af, monkeypatch, logfile, capsys):
        """Fail-closed: the labels probe refuses (the body read still works — the #32 issue link
        and the review flip proceed as usual) → zero `pr merge`, FALSE, a line that says WHY."""
        gh = FakeGH(pr_body="Fixes #73\n", labels=[], fail_labels_read=True)
        stats, out = self._run(af, monkeypatch, logfile, gh, capsys)
        assert self._merges(gh) == []
        assert stats.get("armed_by_pod") is False
        loud = [ln for ln in out.splitlines()
                if ln.startswith("bookkeeping: arming SKIPPED (labels probe UNREADABLE")]
        assert len(loud) == 1
        assert "fail-closed" in loud[0]

    def test_a_pr_without_major_is_armed_as_before(self, af, monkeypatch, logfile, capsys):
        """The unchanged path: labels readable, no `major` → exactly one `pr merge --auto --squash`
        on the PR, `armed_by_pod` TRUE, the pre-existing armed line byte-stable."""
        gh = FakeGH(pr_body="Fixes #73\n", labels=["dependencies", "minor"])
        stats, out = self._run(af, monkeypatch, logfile, gh, capsys)
        assert self._merges(gh) == [
            ("pr", "merge", "https://github.com/o/r/pull/90", "--auto", "--squash")]
        assert stats.get("armed_by_pod") is True
        assert "bookkeeping: auto-merge armed on https://github.com/o/r/pull/90" in out.splitlines()

    def test_the_lane_read_happens_before_the_merge(self, af, monkeypatch, logfile, capsys):
        """Order pins the mechanism: the labels probe precedes the arm on the ordinary path — a
        probe AFTER the write would be a log line, not a gate."""
        gh = FakeGH(pr_body="Fixes #73\n", labels=[])
        self._run(af, monkeypatch, logfile, gh, capsys)
        kinds = [c[:2] for c, _ in gh.calls]
        probe = [i for i, (c, _) in enumerate(gh.calls)
                 if c[:2] == ("pr", "view") and "labels" in c]
        assert probe, "no labels probe was made"
        assert probe[0] < kinds.index(("pr", "merge"))

    def test_no_arm_env_still_wins_without_a_probe(self, af, monkeypatch, logfile, capsys):
        """`AGENT_ARM_PR=0` is the earlier branch and stays byte-identical: it skips WITHOUT
        reading labels (no probe call), with its own pre-existing line."""
        monkeypatch.setenv("AGENT_ARM_PR", "0")
        gh = FakeGH(pr_body="Fixes #73\n", labels=["major"])
        stats, out = self._run(af, monkeypatch, logfile, gh, capsys)
        assert self._merges(gh) == []
        assert stats.get("armed_by_pod") is False
        assert not [c for c, _ in gh.calls if c[:2] == ("pr", "view") and "labels" in c]
        assert "bookkeeping: arming SKIPPED (AGENT_ARM_PR=0 — human-gated PR)" in out.splitlines()
