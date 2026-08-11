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
# FU-089: prove identity to the broker — the projected SA token (agentstack-worker) rides as a
# Bearer; the proxy TokenReviews it. Absent token (older pods, docker mode) stays legal until
# the proxy sets GIT_TOKEN_REQUIRE_AUTH=1.
SA=\$(cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null || true)
curl -fsS --max-time 10 \${SA:+-H "Authorization: Bearer \$SA"} '$GIT_CRED_BROKER_URL' 2>/dev/null \
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

# FU-160 in-pod half (agent-runtime#66): stamp the spans that end BEFORE `agent-finalize
# --snapshot`, which is the earliest timestamp finalize holds — so this is the only place in the
# pod that can see them. finalize emits them at the end as `agent_run_phase_seconds{phase=...}`,
# breaking the launcher's opaque `ride` bar open. `|| true` on every mark: `set -e` is on from the
# top of this file and an observability stamp may never fail a ride.
if [ ! -d "$WORKDIR/.git" ]; then
  echo "→ cloning $REPO_URL @ $BASE_REF"
  _phase_t0=$(date +%s)
  git clone --depth 50 --branch "$BASE_REF" "$REPO_URL" "$WORKDIR"
  agent-finalize --mark clone "$(( $(date +%s) - _phase_t0 ))" || true
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
# Override with NIX_CACHE_URL, or set it empty to disable.
#
# homelab FU-130: this used `extra-substituters`, which ADDS the mirror and leaves cache.nixos.org
# in the list — so every LAN miss also went to the WAN (28 cache.nixos.org lookups in one ride's DNS
# harvest). Harmless in monitor mode, a HANG once the egress policy enforces: nix waits on a
# blackholed connect rather than failing. `substituters` REPLACES the list, which is the honest
# posture for a sandboxed ride: the pull-through mirror is the only way out, and it fetches upstream
# on the ride's behalf. Signatures still come from cache.nixos.org (bodies pass through
# byte-for-byte), so no trusted-key change. If the mirror is down the closure genuinely is
# unavailable — that surfaces as a failure instead of silently escaping the sandbox.
NIX_CACHE_URL="${NIX_CACHE_URL:-http://nixcache.nix-cache.svc.cluster.local}"
if [ -n "$NIX_CACHE_URL" ]; then
  export NIX_CONFIG="substituters = ${NIX_CACHE_URL}?priority=10
extra-trusted-substituters = ${NIX_CACHE_URL}"
fi

# Stack devbox-cache (homelab FU-096): the launcher mounts the stack's CI-published cache artifact
# read-only at /stack-cache (k8s ImageVolume; built by homelab devbox-cache.reusable.yml). Two
# halves: (a) EVAL-CACHE SEED — copy /stack-cache/xdg/{nix,devbox} into ~/.cache. Nix evaluation,
# not fetching, was measured to be the bring-up tax (homelab FU-015: 94s `devbox install` on a
# fully-warm store; 2026-07-27 repro: 55s cold-cache vs 4s seeded), and the cache is portable
# across project path AND HOME by plain copy (measured same day). Guarded by EXACT devbox.lock
# match — a stale :latest artifact or an old BASE_REF skips the seed loudly and the run degrades
# to cold eval, never a wrong toolchain. (b) LOCAL SUBSTITUTER — /stack-cache/store is a file://
# nix binary cache of the project closure with upstream signatures intact (`nix copy` preserves
# narinfo Sig), so no trusted-key changes; a miss falls through to the LAN mirror above.
STACK_CACHE_DIR="${STACK_CACHE_DIR:-/stack-cache}"
if [ -d "$STACK_CACHE_DIR" ]; then
  if [ -f devbox.lock ] && [ -f "$STACK_CACHE_DIR/devbox.lock" ] \
     && cmp -s devbox.lock "$STACK_CACHE_DIR/devbox.lock"; then
    XDG_DEST="${XDG_CACHE_HOME:-$HOME/.cache}"
    mkdir -p "$XDG_DEST"
    # cp -rf, not -r: ~/.cache already holds the HARNESS's own nix caches from the image build,
    # including READ-ONLY git-object files (0444) the stack seed overlaps — plain cp cannot
    # overwrite those and the whole seed failed on the first live ride (2026-07-27; the jail
    # simulation copied into an EMPTY home and never saw it). -f unlinks-and-retries. chmod is
    # DECOUPLED so a partial copy still comes out writable, and stderr stays visible.
    if cp -rf "$STACK_CACHE_DIR/xdg/." "$XDG_DEST/"; then
      chmod -R u+w "$XDG_DEST" 2>/dev/null || true
      echo "→ stack cache: eval-cache seeded (lock match)"
    else
      chmod -R u+w "$XDG_DEST" 2>/dev/null || true
      echo "WARN: stack cache eval-seed copy failed (see cp errors above) — continuing with cold eval cache" >&2
    fi
  else
    echo "WARN: stack cache mounted but its devbox.lock differs from the repo's — eval seed skipped (stale artifact or old base ref; next lock-push rebuilds it)" >&2
  fi
  if [ -f "$STACK_CACHE_DIR/store/nix-cache-info" ]; then
    export NIX_CONFIG="${NIX_CONFIG:+$NIX_CONFIG
}extra-substituters = file://$STACK_CACHE_DIR/store?priority=5
extra-trusted-substituters = file://$STACK_CACHE_DIR/store"
    echo "→ stack cache: local substituter file://$STACK_CACHE_DIR/store"
  fi
fi

# Materialize the PROJECT toolchain from its own devbox.json. Cold the very first time across all
# pods (populates the cache above); near-instant for the same closure afterwards.
if [ -f devbox.json ]; then
  echo "→ devbox install (project toolchain)"
  _phase_t0=$(date +%s)
  devbox install
  # The row FU-096's cache work is judged by: cold nix eval was measured at 94s, seeded at 4s.
  agent-finalize --mark devbox-install "$(( $(date +%s) - _phase_t0 ))" || true
fi

# claude harness (FU-066): the image pre-seeds trust for the default /work/repo, but WORKDIR is
# env-overridable — regenerate ~/.claude.json for the ACTUAL workdir so a headless `claude -p`
# never hangs on the trust dialog. jq-composed so an exotic WORKDIR can't silently corrupt the
# JSON (the failure mode is a hang, not a crash — reviewer finding, agent-runtime#14). Settings
# (bypass-permissions warning skip) stay as baked.
if command -v claude >/dev/null 2>&1; then
  jq -n --arg d "$WORKDIR" \
    '{hasCompletedOnboarding: true, projects: {($d): {hasTrustDialogAccepted: true}}}' \
    > "$HOME/.claude.json"
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
