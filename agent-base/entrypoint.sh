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

# Git auth — READ AT USE TIME, not frozen at start (homelab FU-064b). ESO mints ~1h App tokens and
# rewrites the Secret on its refreshInterval, but an env var snapshots the token at pod start: any
# run >~55 min pushes with a dead token (oracle-fleet#1 attempts 1+3, 2026-07-09). When the Secret is
# VOLUME-mounted (GIT_TOKEN_FILE), the credential helper cats the file per git operation and a `gh`
# wrapper re-reads it per invocation — kubelet rewrites mounted Secret files on rotation, so long
# runs always push with a live token. Env GH_TOKEN stays as the fallback for launchers that predate
# the mount. (v2 / ADR-081 FU-018: injected by the egress proxy, never held in the pod at all.)
GIT_TOKEN_FILE="${GIT_TOKEN_FILE:-/secrets/git/token}"
# ADR-087 leg B: when the launcher provides GIT_CRED_BROKER_URL, git/gh fetch the LIVE token per
# operation from the egress proxy's /git-token endpoint — the pod holds no git credential at all
# (the mount/env below remain as fallback until FU-020 removes them). curl is in the image.
if [ -n "${GIT_CRED_BROKER_URL:-}" ]; then
  mkdir -p "$HOME/bin"
  # Fetch-token script (single source for the helper AND the gh wrapper): broker → mounted file →
  # env, in that order; each fallback is loud-capable but never blocks the operation chain.
  cat > "$HOME/bin/agent-git-token" <<TOKENFETCH
#!/bin/sh
curl -fsS --max-time 10 '$GIT_CRED_BROKER_URL' 2>/dev/null \
  || cat '$GIT_TOKEN_FILE' 2>/dev/null \
  || printf %s "\${GH_TOKEN:-}"
TOKENFETCH
  chmod +x "$HOME/bin/agent-git-token"
  cat > "$HOME/bin/git-cred-helper" <<'CREDHELPER'
#!/bin/sh
echo username=x-access-token
echo "password=$($HOME/bin/agent-git-token)"
CREDHELPER
  chmod +x "$HOME/bin/git-cred-helper"
  git config --global credential.helper "$HOME/bin/git-cred-helper"
  GH_BIN="$(command -v gh || true)"
  if [ -n "$GH_BIN" ]; then
    cat > "$HOME/bin/gh" <<GHWRAP
#!/bin/sh
GH_TOKEN="\$(\$HOME/bin/agent-git-token)" exec $GH_BIN "\$@"
GHWRAP
    chmod +x "$HOME/bin/gh"
  fi
  export PATH="$HOME/bin:$PATH"
  git config --global user.name  "${GIT_AUTHOR_NAME:-homelab-agent}"
  git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@teststuff.net}"
elif [ -s "$GIT_TOKEN_FILE" ]; then
  git config --global credential.helper \
    '!f() { echo username=x-access-token; echo "password=$(cat '"$GIT_TOKEN_FILE"')"; }; f'
  GH_BIN="$(command -v gh || true)"
  if [ -n "$GH_BIN" ]; then
    mkdir -p "$HOME/bin"
    printf '#!/bin/sh\nGH_TOKEN="$(cat %s)" exec %s "$@"\n' "$GIT_TOKEN_FILE" "$GH_BIN" > "$HOME/bin/gh"
    chmod +x "$HOME/bin/gh"
    export PATH="$HOME/bin:$PATH"
  fi
  git config --global user.name  "${GIT_AUTHOR_NAME:-homelab-agent}"
  git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@teststuff.net}"
elif [ -n "${GH_TOKEN:-}" ]; then
  git config --global credential.helper '!f() { echo username=x-access-token; echo "password=${GH_TOKEN}"; }; f'
  git config --global user.name  "${GIT_AUTHOR_NAME:-homelab-agent}"
  git config --global user.email "${GIT_AUTHOR_EMAIL:-agent@teststuff.net}"
fi

if [ ! -d "$WORKDIR/.git" ]; then
  echo "→ cloning $REPO_URL @ $BASE_REF"
  git clone --depth 50 --branch "$BASE_REF" "$REPO_URL" "$WORKDIR"
fi
cd "$WORKDIR"
# Deterministic branch state (old finding C, homelab TICK-LOG 2026-07-09): when the caller names an
# EXISTING remote branch (a fix round resuming its PR branch, or a salvaged WIP branch), check it out
# tracking the remote head — never leave "which branch" to the model (round 3 of oracle-fleet#1 died
# working a throwaway branch that could never reach its PR). A fresh WORK_BRANCH forks from BASE_REF.
if git ls-remote --exit-code --heads origin "$WORK_BRANCH" >/dev/null 2>&1; then
  echo "→ resuming existing remote branch $WORK_BRANCH"
  git fetch origin "$WORK_BRANCH"
  git checkout -B "$WORK_BRANCH" FETCH_HEAD
else
  git checkout -B "$WORK_BRANCH"
fi

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

# claude harness (FU-066): the image pre-seeds trust for the default /work/repo, but WORKDIR is
# env-overridable — regenerate ~/.claude.json for the ACTUAL workdir so a headless `claude -p`
# never hangs on the trust dialog. Settings (bypass-permissions warning skip) stay as baked.
if command -v claude >/dev/null 2>&1; then
  printf '{"hasCompletedOnboarding":true,"projects":{"%s":{"hasTrustDialogAccepted":true}}}\n' \
    "$WORKDIR" > "$HOME/.claude.json"
fi

echo "→ ready: branch=$WORK_BRANCH  goose=$(command -v goose)  opencode=$(command -v opencode)  claude=$(command -v claude)"

# Baseline for the end-of-run stats (agent-finalize): session start time + OpenRouter usage now, so the
# post-run delta = this run's cost/duration. Best-effort; never blocks the run.
agent-finalize --snapshot || true

# Storm hard-stop (#8, homelab FU-021): supervise /tmp/run.log and SIGTERM the harness (never the
# shell pipeline — finalize must still run) on a sustained dead-key auth/credit retry storm.
# Backgrounded before the exec, so it rides along as a child of PID 1. STORM_WATCHDOG=off disables.
agent-storm-watchdog &

exec "$@"
