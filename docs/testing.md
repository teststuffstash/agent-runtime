# Testing the harness

`agent-base/` is the program every agent ride runs inside. It decides whether a model gets
**struck**, whether a round is **re-dispatched**, and whether a run's work is **salvaged**. Until
2026-08-06 the only gate on it was "the Docker image still builds", which cannot see any of that.

## Run it

```sh
devbox run ci      # the gate CI runs — pytest over tests/
devbox run test    # same thing, without the ci.sh wrapper
```

No venv, no `uv`, no lockfile to drift: pytest comes from devbox, and the harness is stdlib-only.

## Why the tests load the module by path

`agent-finalize` and `agent-storm-watchdog` ship **extensionless** — they are `COPY`d onto `PATH`
in the image, so they are executables, not importables. `tests/conftest.py` loads `agent-finalize`
via `importlib` from an explicit path. Import is side-effect free because the script guards its
entrypoint with `if __name__ == "__main__":`; `test_import_is_side_effect_free` exists so that
guarantee fails loudly if that guard is ever removed.

`agent-storm-watchdog` is bash, so there is nothing to import: its seam is `--check <run.log>`,
which runs the live rule's arithmetic over a saved log and prints the verdict.
`tests/test_watchdog_repetition.py` drives it with `subprocess` over synthetic logs — still
hermetic (a temp file in, a verdict out), just spelled in a different language.

⚠ Those tests never start the watchdog's poll loop. `terminate_harness()` runs
`pkill -x goose|opencode|claude|.claude-wrapped`, so a test that reached the kill path would
SIGTERM the harness running the tests. `--check` is the same arithmetic without the kill; the
arm/confirm/kill wiring around it is reviewed, not covered.

## The rules

- **Hermetic.** No network, no cluster, no `gh`, no real git remote. Every test writes a temp file
  and calls a function. If a fix seems to need more, the decision under test belongs in a pure
  function — extract it and test that.
- **Environment is explicit.** `classify()` reads `HARNESS_EXIT`, `AGENT_TASK` and the watchdog
  marker path. An autouse fixture clears all three, so a test's behaviour never depends on what the
  developer happened to export.
- **`xfail_strict` is on.** An `xfail` that starts passing FAILS the run. That is deliberate: an
  xfail here pins a known live bug, and when the bug is fixed the suite tells you to delete the
  marker instead of letting a fixed bug sit silently marked as broken.

## What the tests encode

Each case comes from a run that actually happened, named in its docstring. The suite exists because
of two bugs that shipped green under the build-only gate:

| bug | what a test would have caught |
|---|---|
| homelab FU-115b | a no-op detector reading `.commits[]?.commit.committedDate` — `gh` puts that field at top level, so it was `null` on every real input and the predicate returned "no-op" for every PR |
| **agent-runtime#36** | `classify()` returns `clean` for a run that DIED, whenever a PR already exists — pinned by the strict `xfail` in `tests/test_classify.py` until the fix landed, and by `TestDerivedPrUrlMasksDeath` + `TestSalvageGuardOnAFixRound` since |

#36 is worth reading in full, because it is the shape most likely to recur:

```python
succeeded = bool(stats.get("pr_url"))
if succeeded:
    return "clean", ""      # ← returns BEFORE the failure signatures are consulted
if fail: return fail        # truncation → harness-death, never reached on a fix round
```

Measured on circles#32, 2026-08-06 — the *same* `-32602` truncation, opposite verdicts:

| round | PR state | verdict |
|---|---|---|
| r1 | none yet | `harness-death` / `goose-32602-truncation` — model struck, router told |
| r3 | #39 existed, `pr_url` derived from it | `clean` / `""` — no strike, banked nothing |

r3 ran 1255s and $0.0462, pushed no commit, and left every reviewer finding unaddressed.

The fix is one predicate, `died_this_round(fail, stats)`, asked by both `classify()` and
`salvage_push()`: a `pr_url` says an artifact EXISTS, never that THIS ROUND produced it, so a
failure signature from a round that never emitted its structured end-of-run report outranks it.
Writing a test for it means holding both directions at once — a run that died must not read
`clean`, and a run that finished must not be struck for the signature strings its own log
contains. This suite is the live example of the second half: it writes `-32602` and
`401 unauthorized` into every green run.log a ride here produces.
