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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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
