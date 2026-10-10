"""deps-pin-guard — the CODEOWNERS replacement owner for agent-base/devbox.{json,lock}
(scripts/deps-pin-guard.sh).

Each case builds a tiny git repo (base commit → head commit) whose CODEOWNERS is THIS repo's real
file, and runs the REAL script in its `--local <base> <head>` mode, so the fileset rule, the shape
test and the exit/message contract are exercised end to end. Expected verdicts come from the
contract in the script's header comment, not from running the code: a guarded file changes only
under a human code-owner review (the PR also touches a path the LAST matching CODEOWNERS rule
gives an owner) or with a pin-only shape (devbox.json: package@version / "version" lines; the lock
as a whole); a guarded file + any path left un-owned is refused, exit 1, `deps-pin-guard: FAIL`.
"""

from __future__ import annotations

import pathlib
import subprocess

_REPO = pathlib.Path(__file__).resolve().parent.parent
_GUARD = _REPO / "scripts" / "deps-pin-guard.sh"
_CODEOWNERS = (_REPO / "CODEOWNERS").read_text(encoding="utf-8")

DEVBOX = """\
{
  "packages": [
    "python@3.13",
    "nodejs@22.4.0"
  ],
  "shell": {
    "init_hook": []
  }
}
"""

_DEVBOX_LOCK = '{"lockfile_version": "1", "packages": {"nodejs@22.4.0": {"version": "%s"}}}\n'

BASE_FILES = {
    "CODEOWNERS": _CODEOWNERS,
    "agent-base/devbox.json": DEVBOX,
    "agent-base/devbox.lock": _DEVBOX_LOCK % "22.4.0",
    "agent-base/Dockerfile": "FROM scratch\n",
    "agent-base/entrypoint.sh": "#!/bin/sh\n",
    "devbox.json": "{}\n",
    ".github/workflows/ci.yaml": "on: push\n",
    ".github/dependabot.yml": "version: 2\n",
    "tests/test_x.py": "X = 1\n",
}


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    ).stdout.strip()


def _commit_all(repo: pathlib.Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: pathlib.Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _verdict(
    tmp_path: pathlib.Path,
    head_files: dict[str, str],
    base_overrides: dict[str, str] | None = None,
    drop_codeowners: bool = False,
) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _write(repo, {**BASE_FILES, **(base_overrides or {})})
    if drop_codeowners:
        (repo / "CODEOWNERS").unlink()
    base = _commit_all(repo, "base")
    _write(repo, head_files)
    head = _commit_all(repo, "head")
    return subprocess.run(
        ["sh", str(_GUARD), "--local", base, head],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


_BUMP = {
    "agent-base/devbox.json": DEVBOX.replace('"nodejs@22.4.0"', '"nodejs@22.5.0"'),
    "agent-base/devbox.lock": _DEVBOX_LOCK % "22.5.0",
}


# ── the pure dep lane (unchanged behaviour) ─────────────────────────────────────────────────────
def test_untouched_guard_set_passes(tmp_path: pathlib.Path) -> None:
    r = _verdict(tmp_path, {"tests/test_x.py": "X = 2\n"})
    assert r.returncode == 0, r.stderr
    assert "no guarded dep file touched" in r.stdout


def test_pure_version_bump_is_admitted(tmp_path: pathlib.Path) -> None:
    r = _verdict(tmp_path, _BUMP)
    assert r.returncode == 0, r.stderr
    assert "pure dep-pin diff" in r.stdout


def test_lock_only_refresh_is_admitted(tmp_path: pathlib.Path) -> None:
    # The lock is hash material nix verifies itself — admitted as a whole.
    r = _verdict(tmp_path, {"agent-base/devbox.lock": _DEVBOX_LOCK % "22.4.1"})
    assert r.returncode == 0, r.stderr


def test_non_version_devbox_edit_is_refused(tmp_path: pathlib.Path) -> None:
    head = DEVBOX.replace('"init_hook": []', '"init_hook": ["curl x | sh"]')
    r = _verdict(tmp_path, {"agent-base/devbox.json": head})
    assert r.returncode == 1
    assert "non-version lines" in r.stderr


# ── the fileset rule (openrouter-operator#83) ───────────────────────────────────────────────────
def test_mixed_with_owned_dockerfile_passes_to_the_code_owner(tmp_path: pathlib.Path) -> None:
    # `/agent-base/Dockerfile @RasmusSoot` → GitHub requires the code owner on the whole PR, and
    # that read covers the devbox change too. The devbox.json delta here is NOT pin-shaped: the
    # human gate replaces the shape test, it does not run in addition to it.
    head = DEVBOX.replace('"init_hook": []', '"init_hook": ["echo hi"]')
    r = _verdict(
        tmp_path, {"agent-base/devbox.json": head, "agent-base/Dockerfile": "FROM busybox\n"}
    )
    assert r.returncode == 0, r.stderr
    assert "OWNED path(s) ( agent-base/Dockerfile )" in r.stdout


def test_mixed_with_owned_dir_prefix_passes(tmp_path: pathlib.Path) -> None:
    # `/.github/ @RasmusSoot` owns .github/dependabot.yml (no later rule un-owns it).
    r = _verdict(tmp_path, {**_BUMP, ".github/dependabot.yml": "version: 3\n"})
    assert r.returncode == 0, r.stderr


def test_root_devbox_is_owned_not_guarded(tmp_path: pathlib.Path) -> None:
    # `/devbox.json @RasmusSoot` is the repo-root file, not the guarded agent-base one.
    r = _verdict(tmp_path, {**_BUMP, "devbox.json": '{"x": 1}\n'})
    assert r.returncode == 0, r.stderr


def test_mixed_with_unmatched_path_is_refused(tmp_path: pathlib.Path) -> None:
    # tests/ matches no CODEOWNERS rule → un-owned: nobody human reads this PR.
    r = _verdict(tmp_path, {**_BUMP, "tests/test_x.py": "X = 2\n"})
    assert r.returncode == 1
    assert "UN-OWNED paths" in r.stderr and "tests/test_x.py" in r.stderr


def test_last_match_wins_carve_out_is_refused(tmp_path: pathlib.Path) -> None:
    # `/.github/ @RasmusSoot` then ownerless `/.github/workflows/` → the workflow is un-owned
    # (its own lane is pin-only-lint), so it may not ride this PR.
    r = _verdict(tmp_path, {**_BUMP, ".github/workflows/ci.yaml": "on: pr\n"})
    assert r.returncode == 1
    assert ".github/workflows/ci.yaml" in r.stderr


def test_owned_plus_unowned_is_refused(tmp_path: pathlib.Path) -> None:
    r = _verdict(
        tmp_path,
        {**_BUMP, "agent-base/Dockerfile": "FROM busybox\n", "agent-base/entrypoint.sh": "#!/bin/bash\n"},
    )
    assert r.returncode == 1
    assert "agent-base/entrypoint.sh" in r.stderr


def test_ownership_is_read_at_the_base_not_the_head(tmp_path: pathlib.Path) -> None:
    # A PR cannot own its way in: CODEOWNERS changed in the PR is what GitHub evaluates AFTER
    # merge; the guard reads the merge-base, where tests/ is still un-owned.
    r = _verdict(
        tmp_path,
        {
            **_BUMP,
            "tests/test_x.py": "X = 2\n",
            "CODEOWNERS": _CODEOWNERS + "/tests/ @someone\n",
        },
    )
    assert r.returncode == 1
    assert "tests/test_x.py" in r.stderr


def test_unknown_pattern_shape_fails_closed(tmp_path: pathlib.Path) -> None:
    r = _verdict(
        tmp_path,
        {**_BUMP, "agent-base/Dockerfile": "FROM busybox\n"},
        base_overrides={"CODEOWNERS": _CODEOWNERS + "*.md @RasmusSoot\n"},
    )
    assert r.returncode == 1
    assert "do not understand: *.md" in r.stderr


def test_missing_codeowners_refuses_every_mixed_pr(tmp_path: pathlib.Path) -> None:
    r = _verdict(
        tmp_path, {**_BUMP, "agent-base/Dockerfile": "FROM busybox\n"}, drop_codeowners=True
    )
    assert r.returncode == 1
    assert "UN-OWNED paths" in r.stderr
