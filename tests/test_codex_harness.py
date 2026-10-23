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
import tomllib

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


# The recipe's structured end-of-run report, as a Codex ride emits it: the prose is the LAST
# `agent_message` event's `item.text`, so the contract lands on disk JSON-STRING ESCAPED.
CODEX_CONTRACT = {
    "reproduced": True,
    "ci_passed": True,
    "root_cause": "no bug — built the seam",
    "branch": "fix/issue-146-codex-headless-seam",
    "pr_url": "https://github.com/teststuffstash/agent-runtime/pull/148",
}


def _native_stream(contract=CODEX_CONTRACT, extra=()):
    """The four native `codex exec --json` events, as JSONL — the review's (PR#148) shape."""
    events = [
        {"type": "thread.started", "thread_id": "thread_abc"},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "item_0", "type": "agent_message",
                  "text": "Final report:\n" + json.dumps(contract) + "\n"}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 42, "cached_input_tokens": 12, "output_tokens": 5}},
    ]
    return "".join(json.dumps(e) + "\n" for e in list(events) + list(extra))


def _fake_codex_emitting(tmp_path, jsonl):
    """A fake `codex` that writes `jsonl` to stdout (a file it cats, so quoting stays honest)."""
    stream = tmp_path / "stream.jsonl"
    stream.write_text(jsonl, encoding="utf-8")
    return _fake_codex(tmp_path, 'cat "%s"\n' % stream)


class TestFinalizationCompatibility:
    """The seam's run log must be USABLE BY agent-finalize, not merely valid JSONL (#146 req 2).

    The review's core point (RasmusSoot, PR#148): a fixture emitting only `thread.started` /
    `turn.completed` proves JSON syntax, not compatibility with finalization. So the fixture here
    emits the four NATIVE events with the contract escaped inside the final `agent_message` — the
    real on-disk shape — and the assertions run agent-finalize's own `parse_outcome()` over the log
    the seam actually wrote.
    """

    def test_the_seam_writes_a_log_finalize_can_read(self, tmp_path, af):
        proc, run_log, _ = _run_seam(tmp_path, _fake_codex_emitting(tmp_path, _native_stream()))
        assert proc.returncode == 0, proc.stderr
        assert af.parse_outcome(str(run_log)) == CODEX_CONTRACT

    def test_a_failed_codex_run_is_not_read_as_a_finished_round(self, tmp_path, af):
        """`turn.failed` + a nonzero Codex: the exit status survives the tee AND finalize finds no
        contract, so the round is strikable rather than read as one that reported its own end."""
        stream = (
            json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"
            + json.dumps({"type": "turn.failed",
                          "error": {"message": "stream error: 401 Unauthorized"}}) + "\n"
        )
        codex = _fake_codex_emitting(tmp_path, stream)
        codex.write_text(codex.read_text(encoding="utf-8") + "exit 7\n", encoding="utf-8")
        proc, run_log, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 7, proc.stderr
        assert af.parse_outcome(str(run_log)) == {}


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
        """The FIXTURE is the four native events, not two stubs: JSONL-ness is pinned on the shape
        a real Codex emits, so `TestFinalizationCompatibility` can read the same log."""
        proc, run_log, _ = _run_seam(tmp_path, _fake_codex_emitting(tmp_path, _native_stream()))
        assert proc.returncode == 0, proc.stderr
        assert [e["type"] for e in _jsonl(run_log)] == [
            "thread.started",
            "turn.started",
            "item.completed",
            "turn.completed",
        ]

    def test_run_log_captures_stderr_too(self, tmp_path):
        """agent-finalize reads ONE file; a Codex diagnostic on stderr must land in it."""
        codex = _fake_codex(tmp_path, 'echo "boom" >&2\n')
        proc, run_log, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert "boom" in run_log.read_text(encoding="utf-8")


class TestGatewayProviderBlock:
    """The gateway cell (homelab FU-270, GW-SESSIONS-ENV codex rows): the dispatch env's three values
    become the provider block the CLI actually reads; the token never lands in the file."""

    ENV = {"OPENAI_BASE_URL": "http://agent-gateway.agent-egress.svc.cluster.local:8080/openai/v1",
           "OPENAI_API_KEY": "gws_DUMMY-SECRET-session-token", "CODEX_MODEL": "gpt-6-astra"}

    def test_renders_the_block_from_the_env_values(self, tmp_path):
        codex = _fake_codex(tmp_path, 'cat "$CODEX_HOME/config.toml" > "$FAKE_SEEN"\necho \'{"type":"thread.started"}\'\n')
        seen = tmp_path / "seen.toml"
        proc, _, _ = _run_seam(tmp_path, codex, env={**self.ENV, "FAKE_SEEN": str(seen)})
        assert proc.returncode == 0, proc.stderr
        cfg = (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8")
        assert cfg == seen.read_text(encoding="utf-8"), "the CLI saw the rendered file"
        lines = cfg.splitlines()
        assert 'model = "gpt-6-astra"' in lines
        assert 'model_provider = "agent-gateway"' in lines
        assert "[model_providers.agent-gateway]" in lines
        assert 'base_url = "http://agent-gateway.agent-egress.svc.cluster.local:8080/openai/v1"' in lines
        assert 'wire_api = "responses"' in lines
        assert 'env_key = "OPENAI_API_KEY"' in lines
        assert "requires_openai_auth = false" in lines

    def test_the_token_never_lands_in_the_file_and_the_file_is_private(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, run_log, _ = _run_seam(tmp_path, codex, env=self.ENV)
        assert proc.returncode == 0, proc.stderr
        cfg_path = tmp_path / "codex-home" / "config.toml"
        assert "DUMMY-SECRET" not in cfg_path.read_text(encoding="utf-8")
        assert "DUMMY-SECRET" not in run_log.read_text(encoding="utf-8")
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600

    def test_no_gateway_env_means_no_render(self, tmp_path):
        """A mounted config (the auth.command shape) stands: the seam renders only for a gateway ride."""
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text("model_provider = \"mounted\"\n", encoding="utf-8")
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, _, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert (home / "config.toml").read_text(encoding="utf-8") == "model_provider = \"mounted\"\n"

    def test_the_mcp_knob_appends_the_remote_server_block(self, tmp_path):
        """homelab #1041's stack knob on the codex harness (FU-270): CODEX_MCP_URL → the pinned CLI's
        `[mcp_servers.<name>] url` shape, after the provider block, in the same private file."""
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        url = "http://stack-mcp.oracle-fleet.svc.cluster.local:8080/mcp"
        proc, run_log, _ = _run_seam(tmp_path, codex, env={**self.ENV, "CODEX_MCP_URL": url})
        assert proc.returncode == 0, proc.stderr
        cfg = (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8")
        assert cfg.endswith("[mcp_servers.stack-mcp]\nurl = \"%s\"\n" % url), cfg
        assert cfg.index("[model_providers.agent-gateway]") < cfg.index("[mcp_servers.stack-mcp]")
        assert "stack-mcp=" + url in proc.stdout, "the seam's ready line names the attach"

    def test_no_mcp_knob_means_no_server_block(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, _, _ = _run_seam(tmp_path, codex, env=self.ENV)
        assert proc.returncode == 0, proc.stderr
        assert "mcp_servers" not in (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8")

    def test_an_mcp_url_that_would_break_toml_is_refused(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, _, _ = _run_seam(tmp_path, codex, env={**self.ENV, "CODEX_MCP_URL": 'http://x"\n[evil]'})
        assert proc.returncode == 78
        assert not (tmp_path / "codex-home" / "config.toml").exists()

    def test_a_value_that_would_break_toml_is_refused(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, _, _ = _run_seam(tmp_path, codex, env={**self.ENV, "CODEX_MODEL": 'x" \nmodel_provider = "evil'})
        assert proc.returncode == 78
        assert not (tmp_path / "codex-home" / "config.toml").exists()


# The optional phone-home / update switches a gateway worker turns OFF (agent-runtime#181, pinned to
# codex-cli 0.162.1 — see docs/codex-worker-config.md). Each is a (table, key) path in the TOML.
OPT_OUTS = [
    ("check_for_update_on_startup",),
    ("analytics", "enabled"),
    ("feedback", "enabled"),
    ("features", "plugins"),
    ("features", "apps"),
    ("features", "api_key_model_discovery"),
]


def _toml_at(cfg, path):
    """The value at `path` in a parsed TOML document, or None if any table on the way is missing."""
    for key in path:
        if not isinstance(cfg, dict) or key not in cfg:
            return None
        cfg = cfg[key]
    return cfg


def _child_config(tmp_path, env):
    """Run the seam with a fake codex that copies the config it was LAUNCHED with, then parse that
    copy. The assertions read the bytes the child sees, not the shell source that wrote them."""
    seen = tmp_path / "child-config.toml"
    codex = _fake_codex(
        tmp_path,
        'cat "$CODEX_HOME/config.toml" > "$FAKE_SEEN"\necho \'{"type":"thread.started"}\'\n',
    )
    proc, _, _ = _run_seam(tmp_path, codex, env={**env, "FAKE_SEEN": str(seen)})
    assert proc.returncode == 0, proc.stderr
    return proc, tomllib.loads(seen.read_text(encoding="utf-8"))


class TestGatewayTelemetryOptOuts:
    """agent-runtime#181 (homelab FU-270): a gateway-rendered worker config turns the six optional
    phone-home / update switches OFF at their pinned paths, and leaves provider, auth, transport and
    retry settings exactly as they were. Parsed with tomllib, so a key in the wrong table, a string
    "false", or a key stranded after a table header all fail here."""

    ENV = TestGatewayProviderBlock.ENV

    def test_every_optional_switch_is_explicitly_false_at_its_path(self, tmp_path):
        _, cfg = _child_config(tmp_path, self.ENV)
        for path in OPT_OUTS:
            assert _toml_at(cfg, path) is False, ".".join(path)

    def test_provider_auth_transport_and_retry_settings_keep_their_values(self, tmp_path):
        _, cfg = _child_config(tmp_path, self.ENV)
        assert cfg["model"] == "gpt-6-astra"
        assert cfg["model_provider"] == "agent-gateway"
        assert cfg["cli_auth_credentials_store"] == "file"
        assert cfg["model_providers"] == {
            "agent-gateway": {
                "name": "agent-gateway",
                "base_url": self.ENV["OPENAI_BASE_URL"],
                "wire_api": "responses",
                "env_key": "OPENAI_API_KEY",
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            }
        }

    def test_no_mcp_block_without_the_mcp_knob(self, tmp_path):
        _, cfg = _child_config(tmp_path, self.ENV)
        assert "mcp_servers" not in cfg

    def test_stack_mcp_survives_the_opt_outs(self, tmp_path):
        """Disabling plugins/apps must not disable the explicitly configured stack MCP server."""
        url = "http://stack-mcp.oracle-fleet.svc.cluster.local:8080/mcp"
        _, cfg = _child_config(tmp_path, {**self.ENV, "CODEX_MCP_URL": url})
        assert cfg["mcp_servers"] == {"stack-mcp": {"url": url}}
        for path in OPT_OUTS:
            assert _toml_at(cfg, path) is False, ".".join(path)

    def test_a_recipe_shaped_prompt_gets_the_same_policy_and_an_intact_argv(self, tmp_path):
        """Both calling styles reach the child through one seam: the policy is the same, and the
        prompt and caller flags arrive as the exact argv the launcher passed."""
        prompt = 'Fix issue #181.\n\nRun `devbox run ci`; report "done" and stop.\n'
        argv_file = tmp_path / "argv"
        seen = tmp_path / "child-config.toml"
        codex = _fake_codex(
            tmp_path,
            r"""cat "$CODEX_HOME/config.toml" > "$FAKE_SEEN"
printf '%s\0' "$@" > "$FAKE_ARGV"
echo '{"type":"thread.started"}'
""",
        )
        proc, _, _ = _run_seam(
            tmp_path,
            codex,
            args=("--skip-git-repo-check", prompt),
            env={**self.ENV, "FAKE_SEEN": str(seen), "FAKE_ARGV": str(argv_file)},
        )
        assert proc.returncode == 0, proc.stderr
        assert argv_file.read_bytes().split(b"\0")[:-1] == [
            b"exec",
            b"--json",
            b"--skip-git-repo-check",
            prompt.encode("utf-8"),
        ]
        cfg = tomllib.loads(seen.read_text(encoding="utf-8"))
        for path in OPT_OUTS:
            assert _toml_at(cfg, path) is False, ".".join(path)

    def test_the_opt_outs_keep_the_private_file_and_the_exit_status(self, tmp_path):
        codex = _fake_codex(tmp_path, 'echo \'{"type":"turn.failed"}\'\nexit 7\n')
        proc, run_log, _ = _run_seam(tmp_path, codex, env=self.ENV)
        assert proc.returncode == 7, proc.stderr
        cfg_path = tmp_path / "codex-home" / "config.toml"
        assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
        assert "DUMMY-SECRET" not in cfg_path.read_text(encoding="utf-8")
        assert "DUMMY-SECRET" not in run_log.read_text(encoding="utf-8")

    def test_a_mounted_config_is_never_given_the_opt_outs(self, tmp_path):
        """No gateway env: the mounted config stands byte-for-byte, native jail included."""
        home = tmp_path / "codex-home"
        home.mkdir()
        mounted = 'model_provider = "mounted"\n'
        (home / "config.toml").write_text(mounted, encoding="utf-8")
        codex = _fake_codex(tmp_path, 'echo \'{"type":"thread.started"}\'\n')
        proc, _, _ = _run_seam(tmp_path, codex)
        assert proc.returncode == 0, proc.stderr
        assert (home / "config.toml").read_text(encoding="utf-8") == mounted


class TestWorkerConfigDocs:
    """The contract is written down where an operator will look, and linked from the README."""

    DOC = ROOT / "docs" / "codex-worker-config.md"

    def test_each_switch_is_documented(self):
        text = self.DOC.read_text(encoding="utf-8")
        for path in OPT_OUTS:
            assert ".".join(path) in text, ".".join(path)

    def test_the_readme_links_the_contract(self):
        assert "docs/codex-worker-config.md" in (ROOT / "README.md").read_text(encoding="utf-8")


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
