"""Cost honesty — agent-runtime#12, the probe-fails-into-a-value class.

Worker `agent-oracle-fleet-181942` (oracle-fleet#1, 2026-07-09) reported `cost_usd: -0.0001` while
really burning $0.0902: its OpenRouter key expired mid-run, the end-of-run usage read 401'd, the
probe returned `0.0` instead of "unknown", and `0.0 - usage_start` went negative. The runs this
zeroes are exactly the ones the model-health pivot most needs priced — auth-storm, token-expiry,
key-death — and since homelab `a9d89c9` (2026-08-08) the coordinator's budget gate charges settled
children their harvested `agent_run_cost_usd`, so a fabricated ~$0 UNDERCHARGES the gate and
over-admits the next children against a budget already spent.

The invariant, on every path that publishes a cost: **a failed probe must never become a number.**
Unknown rides as unknown (null + a `cost_unknown` marker) and the ledger-reflex backfills it later
from the OpenRouter activity API (FU-057). `.agents/review.md` pins this as a prod-serving
invariant, and there was no test on it until this file.

Hermetic: `urlopen` is monkeypatched in every test here — no socket is ever opened.
"""
import io
import json

import pytest


@pytest.fixture
def fake_urlopen(af, monkeypatch):
    """Install a stand-in for `urlopen` and hand back the box it records calls into."""
    box = {}

    def _install(handler):
        def _fake(req, timeout=None):
            box["url"] = req.full_url
            box["data"] = req.data
            return handler(req)

        monkeypatch.setattr(af.urllib.request, "urlopen", _fake)
        return box

    return _install


class _Resp:
    """Minimal urlopen return: a context manager with a status, as router_report reads it."""

    status = 200
    _body = b'{"stored": true, "strike": false}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestOrUsage:
    """The probe itself. It reads a key's cumulative spend; it must report UNKNOWN as unknown."""

    def test_probe_failure_is_unknown_not_zero(self, af, monkeypatch, fake_urlopen):
        """The #12 run verbatim: the key died, so /api/v1/key 401s.

        Returning 0.0 here is what made the delta negative downstream.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-dead")

        def _boom(req):
            raise OSError("HTTP Error 401: Unauthorized")

        fake_urlopen(_boom)
        assert af.or_usage() is None

    def test_absent_key_is_unknown_not_zero(self, af, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert af.or_usage() is None

    def test_malformed_body_is_unknown_not_zero(self, af, monkeypatch, fake_urlopen):
        """A proxy error page parses as neither JSON nor a usage figure — still unknown."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO("<html>502 bad gateway</html>"))
        assert af.or_usage() is None

    def test_reads_the_usage_figure(self, af, monkeypatch, fake_urlopen):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO(json.dumps({"data": {"usage": 12.5}})))
        assert af.or_usage() == 12.5

    def test_missing_usage_field_is_unknown_not_zero(self, af, monkeypatch, fake_urlopen):
        """agent-runtime#41: a 200 whose `data` carries no `usage` key at all.

        The probe did not fail — but it did not learn a figure either, so this is the same
        fabrication class as the dead key: `or 0.0` coalesced the absent field into an
        authoritative zero that then subtracts against the banked baseline.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO(json.dumps({"data": {"limit": 5.0}})))
        assert af.or_usage() is None

    def test_explicit_null_usage_is_unknown_not_zero(self, af, monkeypatch, fake_urlopen):
        """`"usage": null` is the same statement as the absent key: no figure was reported."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO(json.dumps({"data": {"usage": None}})))
        assert af.or_usage() is None

    def test_a_reported_zero_is_a_figure_not_unknown(self, af, monkeypatch, fake_urlopen):
        """The other half of the distinction: a key that genuinely spent nothing reported it.

        `0.0` here is a FACT, so it must stay a number — unknown-ising it would send a run that
        really cost nothing to the activity-API backfill queue forever.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO(json.dumps({"data": {"usage": 0}})))
        assert af.or_usage() == 0.0

    def test_missing_usage_end_never_becomes_a_subtraction(self, af, monkeypatch, fake_urlopen):
        """The composition the issue is actually about — the #12 harm, entered through a 200.

        Baseline banked at $1.09, end probe answers 200-without-usage. Folding that into 0.0 makes
        the delta negative exactly as the dead-key run did; unknown must ride as unknown.
        """
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
        fake_urlopen(lambda req: io.StringIO(json.dumps({"data": {}})))
        assert af.cost_fields(1.0902, af.or_usage()) == {"cost_unknown": True}


class TestCostFields:
    """The usage-delta decision, extracted so it can be tested without a live finalize run."""

    def test_both_ends_known_is_the_delta(self, af):
        assert af.cost_fields(1.0, 1.0902) == {"cost_usd": 0.0902}

    def test_failed_end_read_is_unknown_never_a_subtraction(self, af):
        """#12 verbatim: usage_start banked, end probe dead. `0.0 - 0.0001` must never happen."""
        out = af.cost_fields(0.0001, None)
        assert out == {"cost_unknown": True}
        assert "cost_usd" not in out

    def test_failed_start_read_is_unknown(self, af):
        """A key already dead at --snapshot writes no baseline; the delta is just as unknowable."""
        assert af.cost_fields(None, 0.5) == {"cost_unknown": True}

    def test_residual_negative_is_clamped_to_zero(self, af):
        """Usage accounting can settle a hair below the baseline — that is 0, never a discount."""
        out = af.cost_fields(1.0, 0.9999)
        assert out["cost_usd"] == 0.0
        assert out["cost_usd"] >= 0.0

    def test_a_genuine_zero_is_known_not_unknown(self, af):
        """A run that truly spent nothing is a FACT; it must not be labelled backfill-me."""
        out = af.cost_fields(2.0, 2.0)
        assert out == {"cost_usd": 0.0}


class TestRouterReportCost:
    """The ADR-096 M5 `/report` ledger POST — the last site that fabricated a cost.

    It ran `float(stats.get("cost_usd") or 0.0)`, so a dead-key run whose stats carry NO cost at all
    posted an authoritative-looking `0.0` into the ledger: the same undercount `push_metrics`
    already refuses to emit (it gates on `if "cost_usd" in stats`).
    """

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        """router_report reads six env vars; pin them so a pod's ambient values can't steer a test."""
        for var in ("AGENT_ROUND", "AGENT_STACK", "AGENT_TASK", "MODEL", "GOOSE_MODEL", "HOSTNAME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGENT_REPORT_URL", "http://router.invalid/report")

    def _post(self, af, fake_urlopen, stats, exit_status="clean", error_class=""):
        box = fake_urlopen(lambda req: _Resp())
        af.router_report(stats, exit_status, error_class)
        return json.loads(box["data"].decode())

    def test_unknown_cost_is_never_reported_as_zero(self, af, fake_urlopen):
        """The #12 shape: cost_unknown stats must post null + the marker, never a number."""
        payload = self._post(af, fake_urlopen,
                             {"cost_unknown": True, "pr_url": ""}, "budget-403", "budget-exhausted")
        assert payload["cost_usd"] is None
        assert payload["cost_unknown"] is True

    def test_known_cost_is_reported_verbatim(self, af, fake_urlopen):
        payload = self._post(af, fake_urlopen, {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        assert payload["cost_usd"] == 0.0902
        assert "cost_unknown" not in payload

    def test_a_genuine_zero_is_not_flattened_into_unknown(self, af, fake_urlopen):
        """`or 0.0` erased the difference between "spent nothing" and "we have no idea"."""
        payload = self._post(af, fake_urlopen, {"cost_usd": 0.0})
        assert payload["cost_usd"] == 0.0
        assert payload.get("cost_unknown") is not True

    def test_subscription_run_has_no_cost_and_is_not_backfillable(self, af, fake_urlopen):
        """A claude-tier run carries tokens/turns, not dollars, and no key to backfill from.

        So: no fabricated number, and NOT `cost_unknown` — that flag means "price me from the
        OpenRouter activity API", which can never apply here (finalize's own note, no key_hash).
        """
        payload = self._post(af, fake_urlopen, {"subscription": True, "turns": 42})
        assert payload["cost_usd"] is None
        assert "cost_unknown" not in payload

    def test_payload_degrades_safely_against_todays_receiver(self, af, fake_urlopen):
        """Cross-repo constraint: the receiver still does `float(d.get("cost_usd") or 0.0)` on a
        nullable REAL column (homelab openrouter-proxy/router.py). Fixing its half is out of scope
        here, so the honest payload must not break the POST it rides in today.
        """
        payload = self._post(af, fake_urlopen, {"cost_unknown": True})
        assert float(payload.get("cost_usd") or 0.0) == 0.0  # receiver's exact expression
        json.dumps(payload)  # and it stays a JSON-serializable body


class TestRouterReportRail:
    """Which rail served the ride — agent-runtime#55, the coordinator-path half of homelab#158.

    #158's acceptance is that every degraded ride's run-stats record `rail=subscription-fallback`,
    so the cost of a fleet-wide degrade lands VISIBLY rather than dying with the pod label. The
    launcher twin has fed it since homelab#158 (`agent-session.sh`: `--arg rail "${AGENT_RAIL:-}"`)
    and `run_reports` grew the column in homelab#164 — but a coordinator-path ride has no attending
    launcher (that is exactly why `router_report()` exists in-pod, FU-095 P1), so for precisely the
    rides #158 was written about the store recorded `rail=""`.

    `AGENT_RAIL` is already injected into the worker pod's env by the launcher, so this side is
    self-sufficient: the value is read, never derived here.
    """

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        """Same pinning as the cost class, plus AGENT_RAIL — an ambient rail must not steer these."""
        for var in ("AGENT_ROUND", "AGENT_STACK", "AGENT_TASK", "MODEL", "GOOSE_MODEL", "HOSTNAME",
                    "AGENT_RAIL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGENT_REPORT_URL", "http://router.invalid/report")

    def _post(self, af, fake_urlopen, stats, exit_status="clean", error_class=""):
        box = fake_urlopen(lambda req: _Resp())
        af.router_report(stats, exit_status, error_class)
        return json.loads(box["data"].decode())

    @pytest.mark.parametrize("rail", ["subscription-fallback", "subscription", "openrouter"])
    def test_rail_is_reported_verbatim(self, af, fake_urlopen, monkeypatch, rail):
        """The three values the launcher sets. The pod reports what it was told, unedited."""
        monkeypatch.setenv("AGENT_RAIL", rail)
        payload = self._post(af, fake_urlopen, {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        assert payload["rail"] == rail

    def test_degraded_ride_records_the_fallback_rail(self, af, fake_urlopen, monkeypatch):
        """#158's acceptance clause on the shape it was written about: a fleet-wide degrade, on the
        coordinator path, whose cost must be attributable to the fallback rail after the fact."""
        monkeypatch.setenv("AGENT_RAIL", "subscription-fallback")
        payload = self._post(af, fake_urlopen, {"subscription": True, "turns": 42}, "no-pr", "")
        assert payload["rail"] == "subscription-fallback"
        assert payload["cost_usd"] is None  # a degraded ride still fabricates no dollars (#12)

    def test_absent_rail_is_an_empty_string_key_not_a_missing_one(self, af, fake_urlopen):
        """No AGENT_RAIL in the env (an older launcher, or a hand-run pod) is "" — the same
        not-stated the column already holds. The key must still be PRESENT and a string: the
        receiver does `d.get("rail")` and the column is keyed, not positional."""
        payload = self._post(af, fake_urlopen, {"cost_usd": 0.0})
        assert payload["rail"] == ""

    def test_payload_stays_a_serializable_body_the_receiver_can_ignore(self, af, fake_urlopen,
                                                                      monkeypatch):
        """Backward-compatible in both directions: adding a key must not break the POST against a
        receiver that has not learned it, so it rides as a plain JSON string like every sibling."""
        monkeypatch.setenv("AGENT_RAIL", "subscription-fallback")
        payload = self._post(af, fake_urlopen, {"cost_unknown": True}, "budget-403", "budget-exhausted")
        assert isinstance(payload["rail"], str)
        json.dumps(payload)


class TestRouterReportSessionRef:
    """Issue #115: router_report() must authenticate with the session key ref so the proxy can
    resolve the provider, and fold the replied provider into AGENT_RUN_STATS.

    The proxy resolves the served provider from an `Authorization: Bearer ref:` header. Without
    it, session_ref is always "" and provider attribution is empty. A subscription ride (claude/*)
    has no OpenRouter key ref and must post exactly as today — no header, no provider key.
    """

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        """Same pinning as the cost class — ambient env must not steer these tests."""
        for var in ("AGENT_ROUND", "AGENT_STACK", "AGENT_TASK", "MODEL", "GOOSE_MODEL", "HOSTNAME",
                    "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGENT_REPORT_URL", "http://router.invalid/report")

    def _post(self, af, fake_urlopen, stats, exit_status="clean", error_class=""):
        """Post and return (payload_dict, request_object) so tests can inspect headers too."""
        box = {}

        def _capture(req):
            box["url"] = req.full_url
            box["data"] = req.data
            box["headers"] = {k: v for k, v in req.headers.items()}
            return _Resp()

        fake_urlopen(_capture)
        af.router_report(stats, exit_status, error_class)
        return json.loads(box["data"].decode()), box

    def test_sends_session_ref_when_key_starts_with_ref(self, af, fake_urlopen, monkeypatch):
        """When OPENROUTER_API_KEY is a ref: value, the Authorization header must carry it."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "ref:my-ns/my-secret")
        payload, box = self._post(af, fake_urlopen, {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        auth = box["headers"].get("Authorization", "")
        assert auth == "Bearer ref:my-ns/my-secret", \
            "Expected Authorization header with ref, got %r" % auth

    def test_omits_auth_header_when_key_is_real_key(self, af, fake_urlopen, monkeypatch):
        """A real OpenRouter key (not a ref) must NOT be sent as Authorization on /report."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc123")
        payload, box = self._post(af, fake_urlopen, {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        auth = box["headers"].get("Authorization", "")
        assert auth == "", \
            "Real key must not leak into /report header, got %r" % auth

    def test_omits_auth_header_when_key_is_unset(self, af, fake_urlopen):
        """A subscription ride has no OPENROUTER_API_KEY — must post exactly as today."""
        payload, box = self._post(af, fake_urlopen, {"subscription": True, "turns": 42})
        auth = box["headers"].get("Authorization", "")
        assert auth == "", \
            "Subscription ride must not send auth header, got %r" % auth

    def test_omits_auth_header_when_key_does_not_start_with_ref(self, af, fake_urlopen, monkeypatch):
        """AGENT_CRED_INJECT=0 means the pod holds the real key, not a ref — no header."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live-real-key")
        payload, box = self._post(af, fake_urlopen, {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        auth = box["headers"].get("Authorization", "")
        assert auth == "", \
            "Non-ref key must not be sent as auth header, got %r" % auth


class TestRouterReportProvider:
    """Issue #115: fold the provider the /report reply returns into AGENT_RUN_STATS.

    The proxy now returns `{"stored": true, "strike": false, "provider": "<slug>"}`.
    router_report() must return the provider so finalize() can fold it into stats.
    """

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        for var in ("AGENT_ROUND", "AGENT_STACK", "AGENT_TASK", "MODEL", "GOOSE_MODEL", "HOSTNAME",
                    "OPENROUTER_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGENT_REPORT_URL", "http://router.invalid/report")

    def _post_with_reply(self, af, monkeypatch, reply_body, stats,
                         exit_status="clean", error_class=""):
        """Post and return the provider that router_report() returns."""
        box = {}

        class _ReplyingResp:
            status = 200
            body = json.dumps(reply_body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def _fake(req, timeout=None):
            box["url"] = req.full_url
            box["data"] = req.data
            return _ReplyingResp()

        monkeypatch.setattr(af.urllib.request, "urlopen", _fake)
        provider = af.router_report(stats, exit_status, error_class)
        return provider, box

    def test_returns_provider_from_reply(self, af, fake_urlopen, monkeypatch):
        """When the reply carries a provider slug, router_report() returns it."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "ref:ns/secret")
        provider, box = self._post_with_reply(
            af, monkeypatch,
            {"stored": True, "strike": False, "provider": "openai/gpt-4"},
            {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        assert provider == "openai/gpt-4"

    def test_returns_empty_string_when_reply_has_no_provider(self, af, fake_urlopen, monkeypatch):
        """When the reply omits provider (subscription ride, or no provider event yet), return ''."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "ref:ns/secret")
        provider, box = self._post_with_reply(
            af, monkeypatch,
            {"stored": True, "strike": False},
            {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        assert provider == ""

    def test_returns_empty_string_when_reply_provider_is_empty(self, af, fake_urlopen, monkeypatch):
        """When the reply has provider: '', return '' — never fabricate a value."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "ref:ns/secret")
        provider, box = self._post_with_reply(
            af, monkeypatch,
            {"stored": True, "strike": False, "provider": ""},
            {"cost_usd": 0.0902, "pr_url": "http://x/1"})
        assert provider == ""

    def test_returns_empty_string_when_urlopen_fails(self, af, fake_urlopen, monkeypatch):
        """Best-effort: an unreachable proxy returns '' and never fails the run."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "ref:ns/secret")

        def _boom(req, timeout=None):
            raise OSError("Connection refused")

        monkeypatch.setattr(af.urllib.request, "urlopen", _boom)
        provider = af.router_report({"cost_usd": 0.0902, "pr_url": "http://x/1"}, "clean", "")
        assert provider == ""


class TestRunStatsTableProvider:
    """Issue #115: the run-log table renders provider the way it renders rail."""

    def test_provider_row_appears_when_provider_in_stats(self, af):
        table = af.run_stats_table({"provider": "openai/gpt-4", "model": "gpt-4",
                                    "harness": "goose", "cost_usd": 0.09, "duration_s": 42,
                                    "ci_passed": True, "exit_status": "clean",
                                    "error_class": "", "node": "n1", "pod": "p1",
                                    "project": "test"}, "test-task")
        assert "| provider | `openai/gpt-4` |" in table

    def test_provider_row_omitted_when_provider_not_in_stats(self, af):
        """Never render a provider row with an empty value — same rule as rail."""
        table = af.run_stats_table({"model": "gpt-4", "harness": "goose", "cost_usd": 0.09,
                                    "duration_s": 42, "ci_passed": True, "exit_status": "clean",
                                    "error_class": "", "node": "n1", "pod": "p1",
                                    "project": "test"}, "test-task")
        assert "| provider |" not in table


class TestFinalizeProviderOrdering:
    """Issue #115, round 2: router_report() must fold provider BEFORE bookkeeping() renders the
    run-stats table, not merely before the AGENT_RUN_STATS line.

    bookkeeping() internally calls emit_run_stats() -> run_stats_table(), which builds and POSTs
    the run-stats table at that moment. If router_report() runs after bookkeeping(), the provider
    reaches only the JSON line, never the rendered table — the third acceptance clause ("the
    run-log table renders it the way rail does") is unmet.

    This test drives finalize() with stubs and asserts that bookkeeping() observes stats with
    "provider" already present. It is RED on the broken ordering (router_report after bookkeeping)
    and GREEN once the ordering is corrected.
    """

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        """Pin env vars that finalize() reads so ambient values can't steer the test."""
        for var in ("PROJECT", "HOSTNAME", "NODE_NAME", "HARNESS", "GOOSE_MODEL", "MODEL",
                    "OPENROUTER_KEY_HASH", "AGENT_RAIL", "AGENT_DISPATCH_EPOCH",
                    "OPENROUTER_API_KEY", "AGENT_REPORT_URL", "WORK_BRANCH", "AGENT_TASK",
                    "HARNESS_EXIT", "STORM_MARKER", "AGENT_WATCHDOG_MARKER",
                    "AGENT_PHASE_MARKS", "START_EPOCH", "USAGE_START"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOSTNAME", "test-pod")
        monkeypatch.setenv("AGENT_REPORT_URL", "http://router.invalid/report")
        monkeypatch.setenv("AGENT_WATCHDOG_MARKER", "/tmp/no-such-marker")

    def test_provider_folded_before_bookkeeping_observes_stats(self, af, monkeypatch, tmp_path):
        """router_report() must fold provider into stats BEFORE bookkeeping() reads them.

        Stub router_report to return a known provider slug; spy on bookkeeping to capture the
        stats dict it receives. Assert that "provider" is present in the captured dict.
        """
        # Spy on bookkeeping: capture the stats dict it receives
        bookkeeping_captured = {}

        def _bookkeeping_spy(stats, log_path):
            bookkeeping_captured.update(stats)
            # Don't actually do bookkeeping — no gh, no git in tests

        monkeypatch.setattr(af, "bookkeeping", _bookkeeping_spy)

        # Stub router_report to return a known provider
        def _stub_router_report(stats, exit_status, error_class):
            return "openai/gpt-4"

        monkeypatch.setattr(af, "router_report", _stub_router_report)

        # Stub all the other heavy functions that finalize() calls
        monkeypatch.setattr(af, "salvage_push", lambda stats, log_path: None)
        monkeypatch.setattr(af, "derive_pr_url", lambda stats: None)
        monkeypatch.setattr(af, "push_metrics", lambda stats, exit_status, error_class: None)
        monkeypatch.setattr(af, "push_phase_metrics", lambda durations, stats: None)
        monkeypatch.setattr(af, "upload_transcripts", lambda stats, log_path: None)
        monkeypatch.setattr(af, "parse_outcome", lambda log_path: {})
        monkeypatch.setattr(af, "claude_usage", lambda: {})
        monkeypatch.setattr(af, "claude_cost_equiv", lambda log_path: None)
        monkeypatch.setattr(af, "or_usage", lambda: None)
        monkeypatch.setattr(af, "cost_fields", lambda start, end: {"cost_usd": 0.09})
        monkeypatch.setattr(af, "_read_float", lambda path: None)

        # Stub classify to return clean exit
        def _stub_classify(log_path, stats):
            return "clean", ""

        monkeypatch.setattr(af, "classify", _stub_classify)

        # Create a dummy log file
        log_path = str(tmp_path / "run.log")
        with open(log_path, "w") as f:
            f.write("test run log\n")

        # Run finalize — this is the function under test
        af.finalize(log_path)

        # Assert: bookkeeping must have observed "provider" in stats
        assert "provider" in bookkeeping_captured, \
            "bookkeeping() must observe provider in stats; got keys: %r" % list(bookkeeping_captured.keys())
        assert bookkeeping_captured["provider"] == "openai/gpt-4", \
            "provider value must match what router_report returned"
