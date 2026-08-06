#!/usr/bin/env bash
# ci.sh — the agent-runtime CI gate. The seam: the workflow calls `devbox run ci`, so the logic and
# the tool versions live here + in devbox.json, never in CI YAML. Run it locally the same way.
#
# Until 2026-08-06 this repo's ONLY gate was "the image still builds". That cannot see a logic bug,
# and `agent-base/agent-finalize` is ~820 lines of Python deciding whether a model gets STRUCK and
# whether a round gets RE-DISPATCHED. Two of its bugs shipped green under that gate:
#   - a no-op detector reading `.commits[]?.commit.committedDate` where gh puts it top-level, so it
#     matched nothing and would have mislabelled every ci-red PR (homelab FU-115b, fixed 2026-08-06);
#   - classify() reading a DIED run as `clean` whenever a PR already existed (agent-runtime#36).
# tests/ is the gate that sees those. It is unit-only and hermetic — no network, no cluster, no gh.
set -euo pipefail

pytest tests/ -q
