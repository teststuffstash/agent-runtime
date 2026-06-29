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

# Materialize the PROJECT toolchain from its own devbox.json (cold from the nix cache the first
# time; near-instant once a shared store / attic cache is in place — see agents/README.md).
if [ -f devbox.json ]; then
  echo "→ devbox install (project toolchain)"
  devbox install
fi

echo "→ ready: branch=$WORK_BRANCH  goose=$(command -v goose)  opencode=$(command -v opencode)"
exec "$@"
