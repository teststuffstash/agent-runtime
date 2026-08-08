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
    monkeypatch.setenv("AGENT_WATCHDOG_MARKER", str(tmp_path / "no-such-marker"))
