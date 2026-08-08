# agent-runtime review rubric (appended to the reviewer's system prompt; read by arbitration as the maturity policy)

This repo builds the SUBSTRATE every agent ride runs on: the `agent-base` image, the entrypoint,
and `agent-finalize` — the ledger writer whose numbers the coordinator's budget gate charges
(homelab `a9d89c9`). Blast radius is fleet-wide and delayed: a bad image ships to every ride at
the next pin bump; a wrong `agent_run_cost_usd` silently corrupts admission decisions for days.
The repo is **public**.

## Maturity split — judge by PATH, not repo-wide

- `agent-base/agent-finalize`, the entrypoint, `Dockerfile`, lockfiles: **PROD-SERVING.** The
  merge-forward bias does NOT apply here; pinned invariants below block.
- `tests/`, docs, `.agents/`: pre-prod — approve-with-follow-ups bias, ordinary bar.

## BLOCKING

- **A literal secret or credential value, anywhere.** References only. Public repo.
- **A metric or status that fails INTO a value.** `cost_usd`, `exit_status`, token counts —
  on any error path they must go loud (`null` + an error marker), never a fabricated number
  (the #12 class: a dead key reporting ~$0 undercharges the budget gate).
- **An exit path that skips finalize.** Deadline reap, OOM, harness death — every termination
  must still bank the ledger entry and the strike/stats comment (the #36 class: a silent death
  wedges its stack via stuck labels and phantom wip).
- **A diff outside the declared `Touches:` footprint** landing in the governor paths CODEOWNERS
  names (`.github/`, `Dockerfile`, lockfiles, `.agents/`). A worker editing its own governor is
  ungated whatever the ruleset says.
- **A floating version** — tool pins live in devbox/nix and image tags are date+sha; `latest`
  anywhere in the build is the #35 lesson's sibling.
- **`ci`/`unit` not green at head, or a claim of green that does not name the jobs run** when
  `agent-finalize` or the entrypoint changed.

## NOT blocking — follow-ups instead

Naming, comment gaps, test-coverage wishes on paths the diff does not touch, refactors of
working shell. Harvest bar per the org default: a follow-up must be worth a human opening it.
