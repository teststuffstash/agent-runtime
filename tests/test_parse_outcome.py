"""parse_outcome() — pulling the recipe's structured final_output JSON out of the run log.

Everything downstream keys off what this returns: `ci_passed` and `pr_url` drive classify(), and
`pr_url` is what decides whether a round counts as having shipped. A miss here reads as a silent
failure, never a loud one.
"""


class TestExtraction:
    def test_takes_the_LAST_well_formed_object(self, af, logfile):
        """A round can emit several; the final one is the verdict.

        Rounds that retry print an early optimistic object before the real end-state.
        """
        log = (
            '{"reproduced": false, "ci_passed": false, "pr_url": ""}\n'
            "...more work...\n"
            '{"reproduced": true, "ci_passed": true, "pr_url": "http://x/9"}\n'
        )
        out = af.parse_outcome(logfile(log))
        assert out["ci_passed"] is True
        assert out["pr_url"] == "http://x/9"

    def test_ignores_malformed_json(self, af, logfile):
        """A truncated object must not poison the result — the truncation class exists precisely
        because models get cut off mid-emit."""
        log = '{"ci_passed": true, "pr_url": "http://x/1"}\n{"ci_passed": true, "pr_ur\n'
        out = af.parse_outcome(logfile(log))
        assert out["pr_url"] == "http://x/1"

    def test_missing_file_is_empty_not_an_exception(self, af, tmp_path):
        """finalize runs in the pod's teardown; an unreadable log must never crash bookkeeping."""
        assert af.parse_outcome(str(tmp_path / "absent.log")) == {}

    def test_no_json_at_all(self, af, logfile):
        assert af.parse_outcome(logfile("just prose, no contract object\n")) == {}

    def test_only_declared_keys_survive(self, af, logfile):
        """Stray keys the recipe invented are dropped, so downstream sees a fixed shape."""
        log = '{"ci_passed": true, "pr_url": "http://x/1", "invented": "ignore me"}\n'
        out = af.parse_outcome(logfile(log))
        assert "invented" not in out
        assert set(out) <= {"reproduced", "ci_passed", "branch", "pr_url", "coverage_pct", "root_cause"}
