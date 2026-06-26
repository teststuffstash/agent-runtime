#!/usr/bin/env bash
# agent-base entrypoint — boot-from-git session prep, then hand off to the harness.
#
# The ONLY seam in/out of the sandbox is git (idea borrowed from Daytona's opencode-plugin):
# clone at BASE_REF → branch → work → push branch + open PR. No code is mounted, nothing is
# watched live. Interactive and non-interactive runs prep identically; only the exec'd CMD differs.
set -euo pipefail

: "${REPO_URL:?set REPO_URL (https with the proxy-injected git token, or ssh)}"
BASE_REF="${BASE_REF:-master}"
WORK_BRANCH="${WORK_BRANCH:-agent/$(date -u +%Y%m%d-%H%M%S)}"
WORKDIR="${WORKDIR:-/work/repo}"

if [ ! -d "$WORKDIR/.git" ]; then
  echo "→ cloning $REPO_URL @ $BASE_REF"
  git clone --depth 50 --branch "$BASE_REF" "$REPO_URL" "$WORKDIR"
fi
cd "$WORKDIR"
git checkout -B "$WORK_BRANCH"

# Materialize the PROJECT toolchain from its own devbox.json (cold from the nix cache the first
# time; near-instant once a shared store / attic cache is in place — see agents/README.md).
if [ -f devbox.json ]; then
  echo "→ devbox install (project toolchain)"
  devbox install
fi

echo "→ ready: branch=$WORK_BRANCH  goose=$(command -v goose)  opencode=$(command -v opencode)"
exec "$@"
