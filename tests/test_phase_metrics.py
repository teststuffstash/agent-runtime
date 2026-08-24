"""The IN-POD half of the ride's phase breakdown — FU-160 / agent-runtime#66.

The launcher (homelab `agents/agent-session.sh`, homelab#317) already emits four phases —
`dispatch-gates`, `pod-spinup`, `ride`, `bookkeeping` — where `ride` is the ENVELOPE around
everything this repo does. A `ride` bar that grows says *where to look*, not what happened: the
clone, `devbox install`, the LLM loop and the terminal in-pod leg are one opaque block. This file
gates the pod's own contribution to that family.

Two things are worth more than the rest here:

1. **The clobber.** The pushgateway replaces same-named metrics within a GROUP, and the launcher
   re-pushes its whole family on every phase close. Pushing `agent_run_phase_seconds` into the
   launcher's group would make the two sides delete each other — and since the launcher's final
   `bookkeeping` push happens AFTER this pod is gone, the in-pod rows are the ones that lose,
   silently and only in production. `TestPhaseGroupPath` pins the extra `source=in-pod` grouping
   label that makes the two groups distinct.

2. **No fabricated span.** A phase whose two ends are not both known must be ABSENT, never `0` —
   the #12 class applied to durations (a 0s `llm-loop` reads as a ride whose model never ran, and
   would be indistinguishable from a genuinely instant one on the panel).

Hermetic: `urlopen` is monkeypatched, marks are written into `tmp_path`, and the entrypoint test
reads the checked-out file. No socket, no cluster, no pushgateway.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "agent-base" / "entrypoint.sh"


@pytest.fixture
def marks(tmp_path, monkeypatch):
    """Point the marks file at a temp path and hand it back."""
    p = tmp_path / "agent-phase-marks"
    monkeypatch.setenv("AGENT_PHASE_MARKS", str(p))
    return p


@pytest.fixture
def fake_urlopen(af, monkeypatch):
    """Record the request instead of sending it. Same shape as tests/test_cost.py's."""
    box = {}

    def _install(handler=None):
        def _fake(req, timeout=None):
            box["url"] = req.full_url
            box["data"] = req.data
            box["method"] = req.get_method()
            if handler is not None:
                return handler(req)
            raise AssertionError("unreachable")  # pragma: no cover

        monkeypatch.setattr(af.urllib.request, "urlopen", _fake)
        return box

    return _install


class _Ok:
    """Minimal urlopen return — a context manager, as the pushgateway push uses it."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPhaseMarks:
    """`agent-finalize --mark <phase> <seconds>` — the only writer of the marks file.

    The format has ONE owner on purpose: the entrypoint is the only thing that can see where the
    clone and `devbox install` end, and a format spelled once in shell and once in python is a
    drift waiting to happen.
    """

    def test_a_mark_round_trips(self, af, marks):
        af.mark_phase("clone", 12.4)
        assert af.read_phase_marks() == {"clone": 12.4}

    def test_marks_keep_the_order_they_closed_in(self, af, marks):
        af.mark_phase("clone", 3)
        af.mark_phase("devbox-install", 94)
        assert list(af.read_phase_marks()) == ["clone", "devbox-install"]

    def test_no_marks_file_is_no_phases_not_an_error(self, af, marks):
        """The overwhelmingly common path on an older launcher/entrypoint: nothing marked."""
        assert not marks.exists()
        assert af.read_phase_marks() == {}

    def test_a_garbled_line_is_dropped_not_guessed(self, af, marks):
        marks.write_text("clone 12.4\nthis is not a mark\ndevbox-install oops\nllm 7\n")
        assert af.read_phase_marks() == {"clone": 12.4, "llm": 7.0}

    def test_a_non_numeric_duration_writes_nothing(self, af, marks):
        """Rule #6 on the write side: an unusable value must not become a row."""
        assert af.mark_phase("clone", "n/a") is False
        assert af.read_phase_marks() == {}

    def test_a_negative_duration_is_refused(self, af, marks):
        assert af.mark_phase("clone", -5) is False
        assert af.read_phase_marks() == {}

    def test_a_hostile_phase_name_never_reaches_the_file(self, af, marks):
        """The name becomes a Prometheus label value; the mark is called from shell."""
        assert af.mark_phase('clone"} 1\nagent_run_phase_seconds{phase="x', 1) is False
        assert af.read_phase_marks() == {}

    def test_a_re_marked_phase_corrects_itself(self, af, marks):
        af.mark_phase("clone", 3)
        af.mark_phase("clone", 9)
        assert af.read_phase_marks() == {"clone": 9.0}

    def test_an_unwritable_marks_file_is_non_fatal(self, af, monkeypatch, tmp_path):
        """The entrypoint runs under `set -e` — a stats mark may never fail a ride."""
        monkeypatch.setenv("AGENT_PHASE_MARKS", str(tmp_path / "no-such-dir" / "marks"))
        assert af.mark_phase("clone", 1) is False


class TestPhaseDurations:
    """The table finalize assembles: the entrypoint's marks + the two spans it measures itself."""

    def test_the_two_derived_spans_come_from_the_timestamps_finalize_holds(self, af):
        """`--snapshot` (end of prep) → finalize's first statement → now."""
        out = af.phase_durations({}, start_epoch=100.0, finalize_epoch=1000.0, now=1015.0)
        assert out == {"llm-loop": 900.0, "finalize": 15.0}

    def test_marks_ride_alongside_the_derived_spans(self, af):
        out = af.phase_durations({"clone": 3.0, "devbox-install": 94.0},
                                 start_epoch=100.0, finalize_epoch=1000.0, now=1015.0)
        assert out == {"clone": 3.0, "devbox-install": 94.0,
                       "llm-loop": 900.0, "finalize": 15.0}

    def test_the_derived_spans_tile_the_run_duration(self, af):
        """The invariant that makes the breakdown readable against `agent_run_duration_s`:
        everything from `--snapshot` to now is accounted for by exactly two rows."""
        start, fin, now = 100.0, 1000.0, 1015.0
        out = af.phase_durations({}, start, fin, now)
        assert out["llm-loop"] + out["finalize"] == now - start

    def test_no_snapshot_means_no_llm_loop_row_not_a_zero_one(self, af):
        """A run whose `--snapshot` never landed cannot know where the loop began. Absent, not 0:
        a 0s `llm-loop` is a fabricated measurement (the #12 class, on the duration side)."""
        out = af.phase_durations({}, start_epoch=None, finalize_epoch=1000.0, now=1015.0)
        assert "llm-loop" not in out
        assert out == {"finalize": 15.0}

    def test_a_backwards_clock_never_becomes_a_negative_span(self, af):
        out = af.phase_durations({}, start_epoch=1000.0, finalize_epoch=900.0, now=1015.0)
        assert out["llm-loop"] == 0.0


class TestPhaseGroupPath:
    """THE design decision of #66 — and the specific regression to prove.

    The launcher pushes into `/metrics/job/agent_run_phase/project/<p>/issue/<n>/round/<r>/role/
    worker`. Same grouping key ⇒ same group ⇒ whoever pushes last wins, and that is the launcher's
    post-pod `bookkeeping` close. One extra grouping label (`source=in-pod`) makes the group
    distinct, keeps the job name the homelab panels/alert pin, and needs no homelab change.
    """

    LAUNCHER = "/metrics/job/agent_run_phase/project/circles/issue/12/round/2/role/worker"

    def test_the_in_pod_group_is_not_the_launchers_group(self, af):
        """If these two are ever equal, the in-pod phases are deleted after the pod exits."""
        assert af.phase_group_path("circles", "12", "2") != self.LAUNCHER

    def test_it_is_the_launchers_group_plus_one_label(self, af):
        assert af.phase_group_path("circles", "12", "2") == self.LAUNCHER + "/source/in-pod"

    def test_the_job_stays_the_one_the_consumers_pin(self, af):
        """The homelab alert and the three dashboard panels select `job="agent_run_phase"`; a
        separate job would leave them blind to exactly the rows this issue adds."""
        assert af.phase_group_path("circles", "12", "2").startswith(
            "/metrics/job/agent_run_phase/")

    def test_the_join_labels_the_alert_matches_on_are_all_present(self, af):
        """`AgentRunPhaseSlow` joins `and on (project, issue, round, role)`."""
        path = af.phase_group_path("circles", "12", "2")
        for label, value in (("project", "circles"), ("issue", "12"), ("round", "2"),
                             ("role", "worker")):
            assert "/%s/%s" % (label, value) in path

    def test_a_slash_in_a_value_cannot_forge_a_grouping_label(self, af):
        assert "/round/2/" not in af.phase_group_path("circles/round/2", "12", "1")

    def test_an_empty_value_never_becomes_an_empty_path_segment(self, af):
        """`//` is not a grouping label — the gateway rejects the push outright."""
        assert "//" not in af.phase_group_path("", "", "").replace("/metrics/", "")


class TestPhaseMetricsBody:
    """The exposition body. The contract is fixed by consumers that already exist."""

    def test_the_contract_shape(self, af):
        body = af.phase_metrics_body({"clone": 12.4})
        assert body == ('# TYPE agent_run_phase_seconds gauge\n'
                        '# HELP agent_run_phase_seconds Seconds this ride spent in one '
                        'LAUNCHER-owned phase; the in-pod breakdown belongs to '
                        'agent-finalize (FU-160).\n'
                        'agent_run_phase_seconds{phase="clone"} 12.4\n')

    def test_help_line_matches_the_launcher_byte_for_byte(self, af):
        """HELP is per metric NAME: the pushgateway merges every group's copy of the family on
        each scrape, and a help string differing from the launcher's is logged as a ~256KB
        inconsistency line per conflicting group pair per scrape — 48GiB/day of Loki ingest
        before this line existed (homelab#811, prometheus/pushgateway#194)."""
        body = af.phase_metrics_body({"clone": 1.0})
        assert ("# HELP agent_run_phase_seconds Seconds this ride spent in one LAUNCHER-owned "
                "phase; the in-pod breakdown belongs to agent-finalize (FU-160).") in body.splitlines()[1]
        assert body.count("# HELP") == 1

    def test_one_type_line_for_the_whole_family(self, af):
        body = af.phase_metrics_body({"clone": 3.0, "devbox-install": 94.0, "llm-loop": 900.0})
        assert body.count("# TYPE") == 1
        assert body.count("agent_run_phase_seconds{") == 3

    def test_phase_is_the_only_metric_label(self, af):
        """The launcher's rows carry no other label; a split label set grows a second series per
        ride on every `by (phase)` panel."""
        for line in af.phase_metrics_body({"clone": 1.0}).splitlines():
            if line.startswith("agent_run_phase_seconds{"):
                assert line.split("{", 1)[1].split("}", 1)[0] == 'phase="clone"'

    def test_nothing_to_say_is_an_empty_body(self, af):
        assert af.phase_metrics_body({}) == ""


class TestPushPhaseMetrics:
    """The push itself — best-effort in every degenerate direction the issue names."""

    @pytest.fixture(autouse=True)
    def _explicit_env(self, monkeypatch):
        for var in ("AGENT_TASK", "AGENT_ROUND", "PROJECT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGENT_PUSHGATEWAY_URL", "http://pushgw.invalid:9091")

    def test_it_pushes_the_phases_into_the_in_pod_group(self, af, monkeypatch, fake_urlopen):
        monkeypatch.setenv("AGENT_TASK", "issue-66")
        monkeypatch.setenv("AGENT_ROUND", "2")
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({"clone": 12.4}, {"project": "agent-runtime"})
        assert box["url"] == ("http://pushgw.invalid:9091/metrics/job/agent_run_phase/project/"
                              "agent-runtime/issue/66/round/2/role/worker/source/in-pod")
        assert b'agent_run_phase_seconds{phase="clone"} 12.4' in box["data"]

    def test_the_issue_segment_matches_the_launchers_derivation(self, af, monkeypatch,
                                                                fake_urlopen):
        """`AGENT_TASK` with a leading `issue-` stripped — so a `pr-12` ride stays `pr-12`."""
        monkeypatch.setenv("AGENT_TASK", "pr-12")
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({"clone": 1.0}, {"project": "circles"})
        assert "/issue/pr-12/" in box["url"]

    def test_no_task_key_still_produces_a_legal_group(self, af, fake_urlopen):
        """An adhoc ride (no AGENT_TASK, no PROJECT) must not push a `//` path the gateway 400s."""
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({"clone": 1.0}, {})
        assert "//" not in box["url"].split("://", 1)[1]
        assert box["url"].endswith("/source/in-pod")

    def test_an_unreachable_gateway_never_fails_the_run(self, af, fake_urlopen):
        def _boom(req):
            raise OSError("Connection refused")

        fake_urlopen(_boom)
        af.push_phase_metrics({"clone": 1.0}, {"project": "circles"})  # must not raise

    def test_no_gateway_configured_pushes_nothing(self, af, monkeypatch, fake_urlopen):
        monkeypatch.setenv("AGENT_PUSHGATEWAY_URL", "")
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({"clone": 1.0}, {"project": "circles"})
        assert box == {}

    def test_nothing_measured_pushes_nothing(self, af, fake_urlopen):
        """An empty body would still create the group — and an empty group with a fresh
        `push_time_seconds` is worse than no group: it answers the alert's freshness clause."""
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({}, {"project": "circles"})
        assert box == {}

    def test_the_push_replaces_this_rounds_own_previous_attempt(self, af, fake_urlopen):
        """PUT, not POST: the group is exclusively the pod's (nothing else sets `source=in-pod`),
        so a re-run of the same issue/round must overwrite it wholesale rather than leave a stale
        phase from an earlier attempt behind — the same discipline `push_metrics` uses for
        `agent_run`. The clobber the issue is about is BETWEEN groups, and this group is ours."""
        box = fake_urlopen(lambda req: _Ok())
        af.push_phase_metrics({"clone": 1.0}, {"project": "circles"})
        assert box["method"] == "PUT"


class TestEntrypointMarksTheSpansOnlyItCanSee:
    """The two rows the issue names that finalize cannot derive from what it already holds.

    `clone` and `devbox install` both end BEFORE `agent-finalize --snapshot` runs, so the only
    place in the pod that knows their boundaries is the entrypoint. This is a wiring test in the
    shape of `test_devbox_cache_workflow.py`: it reads the checked-out file, because what goes
    wrong here is not a runtime bug but a mark that was never wired — invisible until a ride is
    already over.
    """

    @staticmethod
    def _lines():
        return ENTRYPOINT.read_text(encoding="utf-8").splitlines()

    def _index(self, needle):
        for i, line in enumerate(self._lines()):
            if needle in line:
                return i
        raise AssertionError("entrypoint.sh has no line containing %r" % needle)

    @pytest.mark.parametrize("phase", ["clone", "devbox-install"])
    def test_the_phase_is_marked(self, phase):
        self._index("agent-finalize --mark %s" % phase)

    @pytest.mark.parametrize("phase", ["clone", "devbox-install"])
    def test_the_mark_cannot_fail_the_ride(self, phase):
        """`set -euo pipefail` is on from line 7 — an unguarded stats call would abort the run
        before the harness ever starts."""
        line = self._lines()[self._index("agent-finalize --mark %s" % phase)]
        assert line.rstrip().endswith("|| true")

    def test_the_clone_mark_closes_the_clone(self):
        assert self._index("git clone --depth") < self._index("agent-finalize --mark clone")
        assert self._index("agent-finalize --mark clone") < self._index('cd "$WORKDIR"')

    def test_the_devbox_mark_closes_devbox_install(self):
        """First `devbox install` hit is the `echo` that announces it — either way the mark that
        closes the phase has to come after the command that opens it."""
        assert self._index("devbox install") < self._index("agent-finalize --mark devbox-install")

    def test_the_marks_are_written_before_the_snapshot_reads_them(self):
        """`--snapshot` must not truncate the marks file: both marks precede it."""
        snap = self._index("agent-finalize --snapshot")
        assert self._index("agent-finalize --mark clone") < snap
        assert self._index("agent-finalize --mark devbox-install") < snap
