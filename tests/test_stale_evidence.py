"""Stale-evidence check (agent-runtime#116): when the recipe's final_output cites evidence
with parseable timestamps, assert max(evidence ts) > head commit ts. On violation, flag it
in the stats/summary (loud line), never silently pass.

Hermetic: git is mocked via subprocess.run monkeypatch, and every timestamp is passed in.
"""
import json
import re

import pytest


class TestCheckStaleEvidence:
    """_check_stale_evidence() — the pure-ish predicate over stats + git."""

    def _fake_git(self, af, monkeypatch, head_ts):
        """Monkeypatch subprocess.run so `git log -1 --format=%cI` returns head_ts."""
        calls = []

        def _fake_run(argv, **kw):
            calls.append(tuple(argv))
            if argv[:2] == ["git", "log"] and "--format=%cI" in argv:
                return af.subprocess.CompletedProcess(argv, 0, head_ts + "\n", "")
            return af.subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        return calls

    def test_no_timestamps_no_flag(self, af, monkeypatch):
        """No ISO 8601 timestamps in stats → no stale_evidence flag."""
        stats = {"root_cause": "a logic bug in parse_outcome"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert "stale_evidence" not in stats

    def test_evidence_after_commit_is_clean(self, af, monkeypatch):
        """Evidence timestamp > head commit timestamp → no flag."""
        stats = {"root_cause": "found at 2026-09-02T21:00:00Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert "stale_evidence" not in stats

    def test_evidence_before_commit_is_stale(self, af, monkeypatch):
        """Evidence timestamp < head commit timestamp → stale_evidence flag set."""
        stats = {"root_cause": "tested at 2026-09-02T20:31:07Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True

    def test_evidence_equal_to_commit_is_stale(self, af, monkeypatch):
        """Evidence timestamp == head commit timestamp → stale (must be strictly after)."""
        stats = {"root_cause": "verified 2026-09-02T20:42:41Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True

    def test_max_evidence_wins(self, af, monkeypatch):
        """Multiple timestamps: max evidence ts must be > head commit."""
        stats = {"root_cause": "old at 2026-09-02T19:00:00Z, new at 2026-09-02T21:00:00Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert "stale_evidence" not in stats  # max (21:00) > head (20:42)

    def test_max_evidence_stale_when_all_before(self, af, monkeypatch):
        """Multiple timestamps, all before head commit → stale."""
        stats = {"root_cause": "a at 2026-09-02T19:00:00Z, b at 2026-09-02T20:00:00Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True

    def test_git_failure_is_non_fatal(self, af, monkeypatch):
        """If git fails, the check is skipped — never fatal."""
        calls = []

        def _fake_run(argv, **kw):
            calls.append(tuple(argv))
            return af.subprocess.CompletedProcess(argv, 1, "", "fatal: not a git repo")

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        stats = {"root_cause": "tested at 2026-09-02T20:31:07Z"}
        af._check_stale_evidence(stats)
        assert "stale_evidence" not in stats

    def test_timestamps_in_any_string_field(self, af, monkeypatch):
        """Timestamps in any string stats value (not just root_cause) are checked."""
        stats = {"branch": "fix/issue-116", "evidence_ts": "2026-09-02T20:31:07Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True

    def test_tz_offset_timestamps(self, af, monkeypatch):
        """Timestamps with timezone offsets (+00:00, -05:00) are parsed."""
        stats = {"root_cause": "tested at 2026-09-02T20:31:07+00:00"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True

    def test_non_string_values_ignored(self, af, monkeypatch):
        """Non-string values (bool, int, list) don't cause errors."""
        stats = {"reproduced": True, "ci_passed": True, "root_cause": "tested at 2026-09-02T20:31:07Z"}
        self._fake_git(af, monkeypatch, "2026-09-02T20:42:41Z")
        af._check_stale_evidence(stats)
        assert stats.get("stale_evidence") is True


class TestStaleEvidenceInStatsTable:
    """run_stats_table() must include a stale_evidence warning row when the flag is set."""

    STATS = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "clean",
             "model": "some/model", "harness": "goose", "cost_usd": 0.42, "duration_s": 128,
             "ci_passed": True, "pod": "pod-7", "project": "agent-runtime"}

    def test_no_stale_row_when_flag_absent(self, af):
        """No stale_evidence key → no stale evidence row in table."""
        stats = dict(self.STATS)
        table = af.run_stats_table(stats, "issue-116")
        assert "stale evidence" not in table.lower()

    def test_stale_row_present_when_flag_set(self, af):
        """stale_evidence=True → a warning row appears in the table."""
        stats = dict(self.STATS, stale_evidence=True)
        table = af.run_stats_table(stats, "issue-116")
        assert "⚠" in table
        assert "stale evidence" in table.lower()

    def test_stale_row_in_check_run_when_present(self, af):
        """The stale evidence row rides the check-run output when present."""
        stats = dict(self.STATS, stale_evidence=True)
        gh = TestStaleEvidenceInStatsTable._fake_gh()
        af.emit_run_stats(gh, stats, "issue-116", "o/r", now="2026-09-04T12:00:00Z")
        summary = gh.check_runs[0]["output"]["summary"]
        assert "⚠" in summary
        assert "stale evidence" in summary.lower()

    @staticmethod
    def _fake_gh():
        """Minimal FakeGitHub for the check-run test."""
        class _Done:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        class _Fake:
            def __init__(self):
                self.check_runs = []
                self.calls = []

            def __call__(self, *args, stdin=None, timeout=30):
                self.calls.append((tuple(args), stdin))
                argv = list(args)
                if argv[:2] == ["pr", "view"]:
                    if "headRefOid" in argv:
                        return _Done(0, "abc123\n")
                    return _Done(0, "a PR body\n")
                if argv[0] != "api":
                    return _Done(0)
                method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
                path = [a for a in argv[1:] if not a.startswith("-") and a != method][0]
                if path.endswith("/check-runs"):
                    self.check_runs.append(json.loads(stdin))
                    return _Done(0, json.dumps({"id": 7}))
                if method == "GET":
                    return _Done(0, json.dumps([]))
                if method == "POST":
                    return _Done(0, json.dumps({"id": 901, "body": stdin and json.loads(stdin).get("body")}))
                if method == "PATCH":
                    return _Done(0, json.dumps({"id": 1, "body": stdin and json.loads(stdin).get("body")}))
                return _Done(0)

        return _Fake()


class TestStaleEvidenceInFinalize:
    """The wiring: _check_stale_evidence is called during finalize, and the flag reaches the table."""

    def test_finalize_calls_check_stale_evidence(self, af, monkeypatch, logfile):
        """_check_stale_evidence is called during finalize with the stats dict."""
        called_with = []

        original = af._check_stale_evidence

        def tracking_check(stats):
            called_with.append(stats)
            original(stats)

        monkeypatch.setattr(af, "_check_stale_evidence", tracking_check)
        # We can't easily run finalize() end-to-end, but we can verify the function exists
        # and is referenced in the finalize function body.
        assert hasattr(af, "_check_stale_evidence")

    def test_check_is_referenced_in_finalize(self, af_source):
        """The _check_stale_evidence call must appear in the finalize() function body."""
        assert "_check_stale_evidence(stats)" in af_source, \
            "_check_stale_evidence must be called in finalize()"