"""The two sides of the repetition marker must resolve the SAME path (#46).

`agent-storm-watchdog` writes `repetition-loop` to a marker file just before it SIGTERMs the
harness; `agent-finalize` reads that file to classify the death as `repetition-loop` instead of the
bare `nonzero-exit-143` the signal alone would give (#13's whole deliverable). Nothing checks that
the writer and the reader agree on WHERE — the defect is silent by construction: both defaults are
`/tmp/agent-watchdog-class`, so a suite that only exercises the default passes either way, and an
override of one side alone just makes the classification quietly disappear.

So this pins the resolution itself, per env combination, on BOTH sides:

⚠ It shells out for the writer's answer. The watchdog is bash, and `--marker` is the seam — it
  prints the path the live poll loop would write to and exits. It must NEVER be tested by letting
  the loop reach its kill path: `terminate_harness()` runs `pkill -x goose|opencode|claude|
  .claude-wrapped`, which would SIGTERM the harness running the tests.

⚠ It never writes `/tmp/agent-watchdog-class`. That is a live path on any pod running the watchdog;
  planting `repetition-loop` there would misclassify the ride running this suite. The round-trip
  case therefore only writes under `tmp_path`, and the default case compares paths only.
"""
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHDOG = ROOT / "agent-base" / "agent-storm-watchdog"

DEFAULT_MARKER = "/tmp/agent-watchdog-class"


def _watchdog_marker(overrides):
    """Where the watchdog would write, under exactly these overrides. Both names are stripped from
    the inherited environment first, so an exported one cannot decide the answer."""
    env = {k: v for k, v in os.environ.items() if k not in ("STORM_MARKER", "AGENT_WATCHDOG_MARKER")}
    env.update(overrides)
    out = subprocess.run(
        ["bash", str(WATCHDOG), "--marker"], env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"--marker failed: {out.returncode} {out.stderr}"
    return out.stdout.strip()


def _finalize_marker(af, monkeypatch, overrides):
    """Where agent-finalize would read from, under the same overrides."""
    monkeypatch.delenv("STORM_MARKER", raising=False)
    monkeypatch.delenv("AGENT_WATCHDOG_MARKER", raising=False)  # the autouse fixture sets this one
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    return af._marker_path()


def _cases(tmp_path):
    """(id, overrides, expected) — expected is the path BOTH sides must land on."""
    canonical = str(tmp_path / "canonical-marker")
    legacy = str(tmp_path / "legacy-marker")
    return {
        "default": ({}, DEFAULT_MARKER),
        "canonical-only": ({"AGENT_WATCHDOG_MARKER": canonical}, canonical),
        "legacy-only": ({"STORM_MARKER": legacy}, legacy),
        "both": ({"AGENT_WATCHDOG_MARKER": canonical, "STORM_MARKER": legacy}, canonical),
    }


@pytest.mark.parametrize("case", ["default", "canonical-only", "legacy-only", "both"])
def test_both_sides_resolve_the_same_marker_path(case, af, monkeypatch, tmp_path):
    """An override of either name must move BOTH sides, or neither. A side that moves alone drops
    the `repetition-loop` classification with no error surfaced — the run lands as
    nonzero-exit-143, which is the under-description #13 was filed to remove."""
    overrides, expected = _cases(tmp_path)[case]
    writes = _watchdog_marker(overrides)
    reads = _finalize_marker(af, monkeypatch, overrides)
    assert writes == reads, (
        f"{case}: agent-storm-watchdog writes {writes!r} but agent-finalize reads {reads!r} — "
        "the watchdog's verdict never reaches classify()"
    )
    assert writes == expected


def test_default_path_is_unchanged(af, monkeypatch):
    """The shipped path is what every deployed ride uses with no override at all; unifying the env
    var name must not move it."""
    assert _watchdog_marker({}) == DEFAULT_MARKER
    assert _finalize_marker(af, monkeypatch, {}) == DEFAULT_MARKER


@pytest.mark.parametrize("var", ["AGENT_WATCHDOG_MARKER", "STORM_MARKER"])
def test_override_survives_the_round_trip_into_classify(var, af, monkeypatch, tmp_path):
    """End to end over the seam that matters: with either name overridden, a marker written where
    the watchdog says it writes is the marker classify() acts on."""
    marker = str(tmp_path / f"marker-from-{var}")
    overrides = {var: marker}
    writes = _watchdog_marker(overrides)
    assert writes == marker, f"watchdog would write {writes!r}, not the overridden {marker!r}"
    pathlib.Path(writes).write_text("repetition-loop\n", encoding="utf-8")

    _finalize_marker(af, monkeypatch, overrides)
    assert af._watchdog_class() == "repetition-loop"
    assert af.failure_signature("", ci_passed=None, watchdog=af._watchdog_class()) == (
        "harness-death",
        "repetition-loop",
    )
