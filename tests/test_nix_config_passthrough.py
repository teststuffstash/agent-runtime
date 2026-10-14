"""The entrypoint's nix-cache block must APPEND to an inbound NIX_CONFIG, never replace it.

The homelab launcher (agents/agent-session.sh, the nix-registry-env block) pins
`flake-registry =` in the ride's pod env so nix stops fetching channels.nixos.org/flake-registry.json
(egress-denied — teststuffstash/sleep-tracking#67). NIX_CONFIG is ONE variable: the old
`export NIX_CONFIG="substituters = …"` silently discarded that pin on every ride. These tests run
the real block (extracted between its markers) under sh, so the assertion is on behaviour, not text.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "agent-base" / "entrypoint.sh"


def _block() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    start = text.index("# >>>NIX-CACHE-CONFIG>>>")
    end = text.index("# <<<NIX-CACHE-CONFIG<<<")
    return text[start:end]


def _run(env_nix_config):
    env = {"PATH": "/usr/bin:/bin", "NIX_CACHE_URL": "http://cache.example"}
    if env_nix_config is not None:
        env["NIX_CONFIG"] = env_nix_config
    out = subprocess.run(
        ["sh", "-c", _block() + '\nprintf "%s" "$NIX_CONFIG"'],
        env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.splitlines()


def test_inbound_registry_pin_survives():
    lines = _run("flake-registry =")
    assert lines == [
        "flake-registry =",
        "substituters = http://cache.example?priority=10",
        "extra-trusted-substituters = http://cache.example",
    ]


def test_no_inbound_config_has_no_blank_line():
    # An unset/empty inbound value must not leave a leading empty line (the `:+` guard).
    for inbound in (None, ""):
        assert _run(inbound) == [
            "substituters = http://cache.example?priority=10",
            "extra-trusted-substituters = http://cache.example",
        ]
