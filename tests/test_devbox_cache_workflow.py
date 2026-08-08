"""The devbox-cache publisher caller (#52) — a repo invariant, checked the same way as the rest.

Every other test here drives harness *logic*; this one gates a workflow FILE, because the thing
that goes wrong with this caller is not a runtime bug, it is a wiring mistake that is invisible
until a ride is already cold. agent-runtime had no devbox-cache package at all (stack-lint
CACHE-01), so every ride paid the cold nix evaluation the other repos skip — a measured 16-minute
first `unit` run against seconds warm.

The one assertion that carries real weight is WHICH LOCK the caller watches. This repo has two:

  devbox.lock              (root)  — the ride's own toolchain; pytest/gh/gitleaks
  agent-base/devbox.lock           — the IMAGE's toolchain, baked at docker build time

`agent-base/entrypoint.sh` seeds the eval cache only when the mounted artifact's lock is
byte-identical to the repo's ROOT lock (`cmp -s devbox.lock "$STACK_CACHE_DIR/devbox.lock"`), and
homelab's reusable builds from the root lock too. A caller keyed to the image lock would publish
happily, go green, and then miss that guard on every single ride — the failure mode is a silent
cold eval, never a red build. Hence a test, and hence it names both files.

Hermetic: this reads files out of the checkout. No network, no `gh`, no Actions runner.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "devbox-cache.yml"
REUSABLE = "teststuffstash/homelab/.github/workflows/devbox-cache.reusable.yml@master"


def _text():
    """The caller's source, or a readable failure naming the file that is missing."""
    assert WORKFLOW.is_file(), f"missing {WORKFLOW.relative_to(ROOT)} — agent-runtime publishes no devbox-cache"
    return WORKFLOW.read_text(encoding="utf-8")


def _push_paths(text):
    """The `paths:` list of the push trigger, as written.

    A hand-rolled reader on purpose: PyYAML is not in devbox.json, and devbox.json is codeowned —
    a test is not a good enough reason to add a dependency to the ride's toolchain. The shape being
    read is three lines of list items, so the parser can be three lines too.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "paths:")
    except StopIteration:
        return []
    items = []
    for ln in lines[start + 1 :]:
        m = re.match(r"\s*-\s+(.+?)\s*$", ln)
        if not m:
            break
        items.append(m.group(1).split("#")[0].strip().strip("\"'"))
    return items


class TestThinCaller:
    """The caller declares intent; homelab's reusable owns how the cache is built and pushed."""

    def test_calls_the_homelab_reusable(self):
        assert f"uses: {REUSABLE}" in _text()

    def test_does_not_inline_the_build(self):
        """`runs-on` / `steps` here means the publish logic forked from the org's copy.

        The reusable already pins the tier (`homelab-ephemeral`, same as this repo's ci.yaml) and
        owns the content-addressed `lock-<sha256[0:12]>` tagging. A caller that re-implements any
        of that drifts the moment the reusable changes, and nothing tells us.
        """
        text = _text()
        assert "runs-on:" not in text
        assert "steps:" not in text

    def test_grants_packages_write(self):
        """No ghcr push without it — and `secrets: inherit` cannot supply a missing permission."""
        assert re.search(r"^\s+packages:\s+write\s*$", _text(), re.M)


class TestTrigger:
    def test_watches_the_root_lock(self):
        """The root lock is what the entrypoint's seed guard compares the artifact against."""
        assert "devbox.lock" in _push_paths(_text())

    def test_does_not_watch_the_image_lock(self):
        """agent-base/devbox.lock belongs to build-image.yaml, which ships the image.

        Keying the cache to it would publish an artifact whose lock never matches the one
        `entrypoint.sh` compares, so the eval seed would be skipped on every ride while CI stayed
        green — exactly the cold-eval cost this issue exists to remove.
        """
        assert "agent-base/devbox.lock" not in _push_paths(_text())

    def test_watches_itself(self):
        """A change to the caller must republish; otherwise a fixed caller sits unproven until
        the next unrelated lock bump happens to trigger it."""
        assert ".github/workflows/devbox-cache.yml" in _push_paths(_text())

    def test_is_level_triggered_not_scheduled(self):
        """Republish when the lock changes, not on a clock.

        The runner-image lesson the reusable's own header calls out: a cron offset guarantees
        nothing about ordering, so a ride can start against a cache built for the previous lock.
        The lock changing IS the event.
        """
        assert "schedule:" not in _text()

    def test_keeps_the_manual_dispatch(self):
        """The first publish after merge is a hand-run — there is no lock change to ride in on."""
        assert "workflow_dispatch" in _text()

    def test_publishes_from_master_only(self):
        """A cache published from a PR head would key `:latest` to an unmerged lock."""
        m = re.search(r"^\s+branches:\s*\[?\s*(.+?)\s*\]?\s*$", _text(), re.M)
        assert m, "push trigger declares no branches"
        assert [b.strip().strip("\"'") for b in m.group(1).split(",")] == ["master"]
