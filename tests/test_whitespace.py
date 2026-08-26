"""Repo-wide trailing-whitespace guard.

Subsumes the per-file check that was in test_classify.py::test_no_trailing_whitespace.
Uses `git ls-files` + `git grep -I` so it covers ALL tracked text files, including the
extensionless harness executables (agent-base/agent-finalize, agent-storm-watchdog).
"""

import subprocess
import pathlib


def test_no_trailing_whitespace_in_repo():
    """No tracked text file may have a line ending in space or tab."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "grep", "-nIP", r"[ \t]+$"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        # git grep found matches — list them
        lines = result.stdout.strip().splitlines()
        assert False, (
            f"Found {len(lines)} line(s) with trailing whitespace:\n"
            + "\n".join(lines)
        )
    # returncode == 1 means no matches (clean); 128+ is a real error
    assert result.returncode == 1, (
        f"git grep exited with unexpected code {result.returncode}: "
        + result.stderr
    )
