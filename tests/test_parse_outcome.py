"""parse_outcome() — pulling the recipe's structured final_output JSON out of the run log.

Everything downstream keys off what this returns: `ci_passed` and `pr_url` drive classify(), and
`pr_url` is what decides whether a round counts as having shipped. A miss here reads as a silent
failure, never a loud one.

Two log shapes reach it: the bare object goose/opencode/claude print in the clear, and the native
`codex exec --json` JSONL stream #146's seam produces (TestCodexEventStream).
"""
import json


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


def codex_stream(report, extra=()):
    """A native `codex exec --json` stream: the four events the #148 review names, in order.

    `report` is the agent_message body (the recipe's final prose), `extra` any further events.
    Serialised with json.dumps, so the contract inside `report` lands on disk JSON-STRING ESCAPED —
    the real shape, and the reason the raw-log regex cannot see it.
    """
    events = [
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "item_0", "type": "agent_message", "text": report}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 42, "cached_input_tokens": 12, "output_tokens": 5}},
    ]
    return "".join(json.dumps(e) + "\n" for e in list(events) + list(extra))


class TestCodexEventStream:
    """The native Codex JSONL stream — requirement 2's "run log usable by agent-finalize".

    The review on PR#148 (RasmusSoot, CHANGES_REQUESTED) is the case these pin: the contract rides
    JSON-string escaped inside the last `item.completed`/`agent_message`, so the raw-log regex sees
    a backslash where it wants a quote and `json.loads` refuses the escaped body. Pre-build, every
    test below that reads a contract fails — `parse_outcome` returns {} and the ride loses
    ci_passed/reproduced/root_cause/branch/pr_url at the runtime boundary the seam establishes.
    """

    CONTRACT = {
        "reproduced": True,
        "ci_passed": True,
        "root_cause": "no bug — built the seam",
        "branch": "fix/issue-146-codex-headless-seam",
        "pr_url": "https://github.com/teststuffstash/agent-runtime/pull/148",
    }

    def _log(self, logfile, report=None, extra=()):
        body = "Final report:\n" + json.dumps(self.CONTRACT if report is None else report) + "\n"
        return logfile(codex_stream(body, extra))

    def test_all_contract_fields_survive_the_event_envelope(self, af, logfile):
        """The end-to-end regression: four native events in, every contract field out."""
        out = af.parse_outcome(self._log(logfile))
        assert out == self.CONTRACT

    def test_the_last_agent_message_is_the_verdict(self, af, logfile):
        """A Codex turn can emit several agent_messages; as with the bare-object path, the LAST
        one is the report — an early optimistic one must not win."""
        early = json.dumps({"reproduced": False, "ci_passed": False, "pr_url": ""})
        late = json.dumps(self.CONTRACT)
        stream = "".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n",
            json.dumps({"type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": early}}) + "\n",
            json.dumps({"type": "item.completed",
                        "item": {"id": "item_1", "type": "agent_message", "text": late}}) + "\n",
            json.dumps({"type": "turn.completed"}) + "\n",
        ])
        assert af.parse_outcome(logfile(stream)) == self.CONTRACT

    def test_plain_diagnostics_between_events_do_not_break_decoding(self, af, logfile):
        """The seam tees stderr into the same log, so non-JSON lines sit between events — and a
        killed ride leaves a truncated final line. Neither may cost the contract."""
        body = "Final report:\n" + json.dumps(self.CONTRACT) + "\n"
        stream = codex_stream(body)
        mixed = "codex: reading config\n" + stream + "truncated {\"type\":\"turn.com"
        assert af.parse_outcome(logfile(mixed)) == self.CONTRACT

    def test_a_bare_object_log_is_untouched_by_the_adapter(self, af, logfile):
        """The addition must not displace goose/opencode/claude: no Codex event in the log means
        the adapter stands down and the existing extraction answers."""
        log = '{"ci_passed": true, "pr_url": "http://x/9"}\n'
        assert af.codex_event_text(log) == ""
        assert af.parse_outcome(logfile(log)) == {"ci_passed": True, "pr_url": "http://x/9"}

    def test_turn_failed_is_failure_evidence_not_a_contract(self, af, logfile):
        """`turn.failed` is a dead turn: it must yield no contract (nothing fabricated) and its
        message must be readable without the JSON envelope."""
        stream = (
            '{"type":"thread.started","thread_id":"t"}\n'
            '{"type":"turn.failed","error":{"message":"stream error: 401 Unauthorized"}}\n'
        )
        assert af.parse_outcome(logfile(stream)) == {}
        assert "401 Unauthorized" in af.codex_failure_evidence(stream)

    def test_top_level_error_event_is_failure_evidence(self, af, logfile):
        """The stream-level fault shape: `{"type":"error","message":…}`."""
        stream = '{"type":"error","message":"unexpected status 403 Forbidden"}\n'
        assert af.parse_outcome(logfile(stream)) == {}
        assert "403 Forbidden" in af.codex_failure_evidence(stream)

    def test_a_failed_codex_stream_is_not_read_as_a_finished_round(self, af, logfile, monkeypatch):
        """Classification-ready: a Codex round that DIED must not read as one that emitted its
        report (`blocked-deliberate`) or shipped — the failure evidence has to reach classify()."""
        monkeypatch.setenv("HARNESS_EXIT", "1")
        monkeypatch.setenv("AGENT_TASK", "issue-146-codex-headless-seam")
        stream = (
            '{"type":"thread.started","thread_id":"t"}\n'
            '{"type":"error","message":"stream error: connection reset"}\n'
        )
        log = logfile(stream)
        stats = af.parse_outcome(log)
        assert stats == {}
        assert af.classify(log, stats) == ("failed", "nonzero-exit-1")

    def test_a_finished_codex_round_is_not_struck_for_its_own_log(self, af, logfile):
        """The other half of #36: the contract the adapter recovers is what tells classify the
        round reached its own finish line, so its log's contents are text, not a death."""
        log = self._log(logfile)
        stats = af.parse_outcome(log)
        assert stats["ci_passed"] is True and stats["pr_url"] == self.CONTRACT["pr_url"]
        assert af.classify(log, stats) == ("clean", "")
