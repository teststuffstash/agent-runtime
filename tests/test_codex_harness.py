"""The Codex headless seam (agent-runtime#146) — hermetic and credential-free.

`agent-base/agent-codex` is the fourth harness's invocation seam: the launcher owns the command
(`codex exec --json <prompt>`), the image owns the contract around it. These tests pin that
contract with a FAKE `codex` on disk — no network, no OpenAI, no ChatGPT account, no real Codex
binary. The seam is bash, so its seam is `subprocess` over a temp file, the same shape
`tests/test_watchdog_repetition.py` uses for `agent-storm-watchdog`.

What each test would catch against the pre-build tree (no `agent-codex`, no codex in the
entrypoint): the file is absent, so every seam test errors on the missing path, and the
source-reading tests fail on the missing `codex=` ready-line token / `CODEX_HOME` block.
"""
import json
import os
import pathlib
import stat
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEAM = ROOT / "agent-base" / "agent-codex"
ENTRYPOINT = ROOT / "agent-base" / "entrypoint.sh"
DOCKERFILE = ROOT / "agent-base" / "Dockerfile"


def _write_exec(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_codex(tmp_path, body):
    """A stand-in `codex` executable. `body` is bash appended after a strict preamble."""
    return _write_exec(tmp_path / "codex", "#!/usr/bin/env bash\nset -uo pipefail\n" + body)


def _run_seam(tmp_path, codex, args=("do the thing",), env=None):
    """Run the seam against a fake codex in a temp WORKDIR; return (proc, run_log, workdir)."""
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True, exist_ok=True)
    run_log = tmp_path / "run.log"
    e = dict(os.environ)
    e.update(
        {
            "WORKDIR": str(workdir),
            "RUN_LOG": str(run_log),
            "CODEX_BIN": str(codex),
            "CODEX_HOME": str(tmp_path / "codex-home"),
        }
    )
    if env:
        e.update(env)
    proc = subprocess.run(
        ["bash", str(SEAM), *args], env=e, capture_output=True, text=True
    )
    return proc, run_log, workdir


def _jsonl(path):
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _code_only(text):
    """Drop comment lines, so a file may EXPLAIN that it bakes no auth.json without failing the
    assertion that it does not write one."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


class TestHeadlessContract:
    def test_seam_ships_executable(self):
        assert SEAM.is_file(), "agent-base/agent-codex is missing"
        assert SEAM.stat().st_mode & stat.S_IXUSR, "agent-codex must be executable"

    def test_defaults_to_the_prepared_workdir(self):
        """The launcher's WORKDIR is /work/repo; the seam must default there, not to cwd."""
        assert 'WORKDIR="${WORKDIR:-/work/repo}"' in SEAM.read_text(encoding="utf-8")

    def test_runs_in_the_prepared_workdir(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo "{\\"type\\":\\"thread.started\\",\\"cwd\\":\\"$PWD\\"}"\n')
        proc, run_log, workdir = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert _jsonl(run_log)[0]["cwd"] == str(workdir)

    def test_invokes_codex_exec_json(self, tmp_path):
        args_file = tmp_path / "args"
        codex = _fake_codex(tmp_path, 'printf "%s\\n" "$@" > "$FAKE_ARGS"\n')
        proc, _, _ = _run_seam(tmp_path, codex, env={"FAKE_ARGS": str(args_file)})
        assert proc.returncode == 0, proc.stderr
        assert args_file.read_text(encoding="utf-8").splitlines() == [
            "exec",
            "--json",
            "do the thing",
        ]

    def test_preserves_codex_exit_status(self, tmp_path):
        """A nonzero Codex must not be masked by the tee in the pipeline."""
        codex = _fake_codex(tmp_path, 'echo \'{"type":"turn.failed"}\'\nexit 7\n')
        proc, _, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 7

    def test_leaves_a_parseable_jsonl_run_log(self, tmp_path):
        codex = _fake_codex(
            tmp_path,
            'echo \'{"type":"thread.started"}\'\n'
            'echo \'{"type":"turn.completed"}\'\n',
        )
        proc, run_log, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert [e["type"] for e in _jsonl(run_log)] == ["thread.started", "turn.completed"]

    def test_run_log_captures_stderr_too(self, tmp_path):
        """agent-finalize reads ONE file; a Codex diagnostic on stderr must land in it."""
        codex = _fake_codex(tmp_path, 'echo "boom" >&2\n')
        proc, run_log, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert "boom" in run_log.read_text(encoding="utf-8")


class TestRuntimeConfigWithoutAuthJson:
    """A custom provider + command-backed bearer source, with no `auth.json` anywhere."""

    def _fixture(self, tmp_path):
        home = tmp_path / "codex-home"
        home.mkdir()
        marker = tmp_path / "auth-ran"
        auth = _write_exec(
            tmp_path / "mint-token",
            "#!/usr/bin/env bash\n"
            f"touch {marker}\n"
            "printf %s 'ref:homelab/codex-session'\n",
        )
        (home / "config.toml").write_text(
            'model_provider = "homelab"\n'
            "\n"
            "[model_providers.homelab]\n"
            'name = "Homelab"\n'
            'base_url = "https://proxy.example/v1"\n'
            "requires_openai_auth = false\n"
            "\n"
            "[model_providers.homelab.auth]\n"
            f'command = "{auth}"\n'
            'args = []\n'
            "timeout_ms = 5000\n"
            "refresh_interval_ms = 300000\n",
            encoding="utf-8",
        )
        # The fake codex resolves the provider + auth command from the runtime config, runs the
        # command (as Codex does), and emits an event — but never echoes the token.
        codex = _fake_codex(
            tmp_path,
            'cfg="$CODEX_HOME/config.toml"\n'
            'provider=$(sed -n \'s/^model_provider = "\\(.*\\)"/\\1/p\' "$cfg")\n'
            'cmd=$(sed -n \'s/^command = "\\(.*\\)"/\\1/p\' "$cfg")\n'
            'token=$("$cmd")\n'
            'echo "{\\"type\\":\\"thread.started\\",\\"provider\\":\\"$provider\\"}"\n',
        )
        return home, marker, codex

    def test_config_selects_custom_provider_and_command_auth(self, tmp_path):
        home, marker, codex = self._fixture(tmp_path)
        proc, run_log, _ = _run_seam(tmp_path, codex, env={"CODEX_HOME": str(home)})
        assert proc.returncode == 0, proc.stderr
        assert marker.exists(), "the command-backed auth source was never invoked"
        assert _jsonl(run_log)[0]["provider"] == "homelab"

    def test_no_auth_json_is_required_or_created(self, tmp_path):
        home, _, codex = self._fixture(tmp_path)
        proc, _, _ = _run_seam(tmp_path, codex, env={"CODEX_HOME": str(home)})
        assert proc.returncode == 0, proc.stderr
        assert not (home / "auth.json").exists()

    def test_token_never_reaches_the_run_log(self, tmp_path):
        home, _, codex = self._fixture(tmp_path)
        proc, run_log, _ = _run_seam(tmp_path, codex, env={"CODEX_HOME": str(home)})
        assert proc.returncode == 0, proc.stderr
        assert "ref:homelab/codex-session" not in run_log.read_text(encoding="utf-8")


class TestEntrypointProvenance:
    def test_ready_line_names_codex(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert "codex=$(command -v codex)" in text

    def test_sets_codex_home_without_writing_a_credential(self):
        text = ENTRYPOINT.read_text(encoding="utf-8")
        assert 'CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"' in text
        assert "auth.json" not in _code_only(text)


class TestImageWiring:
    def test_dockerfile_installs_the_seam(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY agent-base/agent-codex /usr/local/bin/agent-codex" in text
        assert "/usr/local/bin/agent-codex" in text.split("RUN chmod +x", 1)[1]

    def test_profile_is_on_path_for_normal_and_login_shells(self):
        """codex lives in the same devbox profile as goose/opencode/claude, so both PATH surfaces
        must carry it: the ENV for exec, and /etc/profile.d for `bash -l`."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "ENV PATH=/opt/agent/.devbox/nix/profile/default/bin:$PATH" in text
        assert "/etc/profile.d/agent-path.sh" in text

    def test_image_bakes_no_codex_credential(self):
        assert "auth.json" not in DOCKERFILE.read_text(encoding="utf-8")
