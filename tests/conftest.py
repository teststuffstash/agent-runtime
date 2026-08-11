"""Load `agent-base/agent-finalize` as an importable module.

The harness ships as an extensionless executable (it is `COPY`d onto PATH in the image), so it
cannot be imported by name. Loading it by path is the whole trick; everything else in this suite is
ordinary pytest.

⚠ Import must not execute the script. `agent-finalize` guards its entrypoint with
`if __name__ == "__main__":`, and this loader gives it a different name, so import is side-effect
free. If that guard is ever removed, every test here turns into a live finalize run — which is why
`test_import_is_side_effect_free` exists.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FINALIZE = _ROOT / "agent-base" / "agent-finalize"


def _load():
    spec = importlib.util.spec_from_loader(
        "agent_finalize", importlib.machinery.SourceFileLoader("agent_finalize", str(_FINALIZE))
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_finalize"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def af():
    """The agent-finalize module."""
    return _load()


@pytest.fixture(scope="session")
def af_source():
    """agent-finalize's own SOURCE text, for the tests that must read the code rather than call it.

    A test that derives its cases from the module OBJECT can only ever see what the module already
    agrees on: `DEATH_EXIT_STATUSES` iterated against `bookkeeping_route`, which consults the same
    tuple, is self-referential (#61). Pinning a hand-maintained table to the literals a function can
    actually return needs the source. Same path as `_load()`, so the two cannot drift.
    """
    return _FINALIZE.read_text(encoding="utf-8")


@pytest.fixture
def logfile(tmp_path):
    """Write a run log and hand back its path — classify() reads from disk, not a string."""

    def _write(text):
        p = tmp_path / "run.log"
        p.write_text(text, encoding="utf-8")
        return str(p)

    return _write


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """classify() reads HARNESS_EXIT, AGENT_TASK and the watchdog marker from the environment;
    the resumable-branch answer (#33) reads WORK_BRANCH.

    Left to the ambient environment these leak between tests and, worse, make a green suite depend
    on whatever the developer happened to export. Every test starts from a known-empty state and
    opts in explicitly.
    """
    monkeypatch.delenv("HARNESS_EXIT", raising=False)
    monkeypatch.delenv("AGENT_TASK", raising=False)
    monkeypatch.delenv("WORK_BRANCH", raising=False)
    # Both marker names, because the reader honours STORM_MARKER as a fallback (#46) — an exported
    # one would otherwise point classify() at a real marker file and decide these tests.
    monkeypatch.delenv("STORM_MARKER", raising=False)
    monkeypatch.setenv("AGENT_WATCHDOG_MARKER", str(tmp_path / "no-such-marker"))
    # Same reasoning for the phase marks (#66): this suite runs INSIDE an agent pod, whose
    # entrypoint has already written /tmp/agent-phase-marks for the real ride. A test reading the
    # default path would then assert against another run's clone time.
    monkeypatch.setenv("AGENT_PHASE_MARKS", str(tmp_path / "no-such-phase-marks"))
