#!/usr/bin/env bash
# agent-base entrypoint — boot-from-git session prep, then hand off to the harness.
#
# The ONLY seam in/out of the sandbox is git (idea borrowed from Daytona's opencode-plugin):
# clone at BASE_REF → branch → work → push branch + open PR. No code is mounted, nothing is
# watched live. Interactive and non-interactive runs prep identically; only the exec'd CMD differs.
set -euo pipefail

: "${REPO_URL:?set REPO_URL (https; GH_TOKEN enables private clone+push)}"
BASE_REF="${BASE_REF:-master}"
WORK_BRANCH="${WORK_BRANCH:-agent/$(date -u +%Y%m%d-%H%M%S)}"
WORKDIR="${WORKDIR:-/work/repo}"

# Git auth: if a scoped token is present (minted by the ESO GithubAccessToken generator → GH_TOKEN),
# use it for github.com over HTTPS so the agent can clone PRIVATE repos and push its branch. `gh`
# picks up GH_TOKEN automatically for `gh pr create`. (v2 / ADR-081: injected by the egress proxy,
# never held in the pod.) HOME is writable here (unlike the jail), so a global credential helper is fine.
if [ -n "${GH_TOKEN:-}" ]; then
  git config --global credential.helper '!f() { echo username=x-access-token; echo "password=${GH_TOKEN}"; }; f'
  git config --global user.name  "${GIT_AUTHOR_NAME:-homelab-agent}"
  git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@teststuff.net}"
fi

if [ ! -d "$WORKDIR/.git" ]; then
  echo "→ cloning $REPO_URL @ $BASE_REF"
  git clone --depth 50 --branch "$BASE_REF" "$REPO_URL" "$WORKDIR"
fi
cd "$WORKDIR"
git checkout -B "$WORK_BRANCH"

# Provenance, not config: record WHICH harness + model produced each commit. The model is a call-time
# arg (agent-session --model → GOOSE_MODEL), deliberately NOT pinned in the repo, so history is where
# you find out what wrote a change. A prepare-commit-msg hook stamps it on every commit — including
# `git commit -m`, so it doesn't depend on the agent remembering to add a trailer.
cat > .git/hooks/prepare-commit-msg <<HOOK
#!/bin/sh
grep -q '^Agent-Model:' "\$1" 2>/dev/null && exit 0
printf '\nAgent-Harness: %s\nAgent-Model: %s\n' "${HARNESS:-goose}" "${GOOSE_MODEL:-${MODEL:-unknown}}" >> "\$1"
HOOK
chmod +x .git/hooks/prepare-commit-msg

# Nix pull-through cache (homelab argocd/resources/nix-cache): prefer the in-cluster mirror of
# cache.nixos.org so the project closure is fetched over the WAN once, then served LAN-speed on every
# later run. Bodies pass through unchanged, so upstream signatures stay valid — no extra trusted key.
# An unreachable cache degrades gracefully (nix falls back to the upstream substituter). Override with
# NIX_CACHE_URL, or set it empty to disable.
NIX_CACHE_URL="${NIX_CACHE_URL:-http://nixcache.nix-cache.svc.cluster.local}"
if [ -n "$NIX_CACHE_URL" ]; then
  export NIX_CONFIG="extra-substituters = ${NIX_CACHE_URL}?priority=10
extra-trusted-substituters = ${NIX_CACHE_URL}"
fi

# Materialize the PROJECT toolchain from its own devbox.json. Cold the very first time across all
# pods (populates the cache above); near-instant for the same closure afterwards.
if [ -f devbox.json ]; then
  echo "→ devbox install (project toolchain)"
  devbox install
fi

echo "→ ready: branch=$WORK_BRANCH  goose=$(command -v goose)  opencode=$(command -v opencode)"

# Baseline for the end-of-run stats (agent-finalize): session start time + OpenRouter usage now, so the
# post-run delta = this run's cost/duration. Best-effort; never blocks the run.
agent-finalize --snapshot || true

# Storm hard-stop (#8, homelab FU-021): supervise /tmp/run.log and SIGTERM the harness (never the
# shell pipeline — finalize must still run) on a sustained dead-key auth/credit retry storm.
# Backgrounded before the exec, so it rides along as a child of PID 1. STORM_WATCHDOG=off disables.
agent-storm-watchdog &

exec "$@"
