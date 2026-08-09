"""agent-storm-watchdog's repetition rule (#13), replayed against saved-log shapes.

Two things about how this suite is built, both deliberate:

⚠ It shells out. The watchdog is bash, not Python, so there is nothing to import — `--check` IS the
  seam: it runs the same arithmetic as the live poll on a log you hand it. Everything here is
  `bash agent-base/agent-storm-watchdog --check <fixture>`, hermetic (no network, no cluster, no
  `gh` — only the log file on disk).

⚠ It never starts the live poll loop. `terminate_harness()` runs `pkill -x goose|opencode|claude|
  .claude-wrapped`; a test that let the daemon reach its kill path would SIGTERM the harness running
  the tests. So the arithmetic is tested here and the arm/confirm/kill wiring stays eyeball-reviewed.
  The one consequence worth naming: `--check` cannot model the two-poll + still-growing
  confirmation, so a fixture whose tail is a transient burst reads as WOULD KILL here while the live
  rule would need it to persist across a poll. Fixtures are shaped as sustained loops accordingly.

Every fixture reproduces a run that actually happened — the issue thread's four documented live
loops plus the healthy rides that must survive them. Real logs are unreachable from this pod
(`s3://agent-transcripts/…` is off the egress allowlist), so they are synthesised to the measured
shape, and each fixture's docstring names the ride and the number it is reproducing.
"""
import os
import pathlib
import re
import subprocess
import time
from typing import NamedTuple

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WATCHDOG = ROOT / "agent-base" / "agent-storm-watchdog"

# A healthy ride's tail, measured: 31 distinct lines per 40-line window (homelab meta-watch
# separation check, 2026-08-07) and a worst single-line share of 8% (mimo) / 0% (opus). This is the
# negative control every rule below is argued against, so it has to reproduce the 31 exactly.
#
# ⚠ 32 entries, 31 DISTINCT — the tool banner recurs, as it does in any real log. The count that
#   matters is the distinct one: `healthy()` cycles, and 40 ≥ 32 means every window covers the whole
#   cycle, so the tail signal reports one number per distinct VALUE. 31 entries with a duplicate
#   among them reported 30 and quietly cost a line of the documented margin (#45). The worst share
#   that falls out is 6% — between the two measured healthy rides, and nowhere near the 50% rule.
HEALTHY = (
    "◒  Reading agent-base/agent-finalize",
    "─── text_editor | developer ──────────────────────────",
    "path: /work/repo/agent-base/agent-finalize",
    "command: view",
    "I'll start by reading the issue body.",
    "─── shell | developer ────────────────────────────────",
    "command: gh issue view 13 --json title,body",
    "running: devbox run ci",
    "============================= test session starts =====",
    "collected 47 items",
    "tests/test_classify.py ................ [ 34%]",
    "tests/test_cost.py .................... [ 78%]",
    "tests/test_parse_outcome.py ........... [100%]",
    "47 passed in 1.82s",
    "Now let me look at how classify() reads the marker.",
    "command: git status --short",
    "M  agent-base/agent-storm-watchdog",
    "A  tests/test_watchdog_repetition.py",
    "command: git add -A && git commit -m 'test: reproduce the slow loop'",
    "[fix/issue-13 4d21aa0] test: reproduce the slow loop",
    " 2 files changed, 118 insertions(+)",
    "command: git push -u origin HEAD",
    "remote: Create a pull request for 'fix/issue-13' on GitHub:",
    "To https://github.com/teststuffstash/agent-runtime.git",
    "The failing test is banked; now the minimal fix.",
    "─── text_editor | developer ──────────────────────────",
    "path: /work/repo/agent-base/agent-storm-watchdog",
    "command: str_replace",
    "The window is the problem, not the threshold.",
    "command: devbox run scan-secrets",
    "no leaks found",
    "Opening the PR now.",
)

# The lines the four documented loops actually repeated.
NAG = "Please resend your message to try again.You MUST call the `final_output` tool NOW with the final output for the user."
SKELETON = "Now let me commit and push the skeleton early."
CIRCUIT = "Circuit breaker is open, retrying in 100ms (attempt 41)"
CI_CLEAN = "Both CI and scan-secrets are clean. Now let me create the fix branch, commit, and push:"


def healthy(n, offset=0):
    """`n` lines of a healthy ride's output."""
    return [HEALTHY[(offset + i) % len(HEALTHY)] for i in range(n)]


def write(tmp_path, lines):
    p = tmp_path / "run.log"
    p.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return p


class Verdict(NamedTuple):
    share: int  # worst single-line share of a full REPEAT_WINDOW, in whole %
    distinct: int  # fewest distinct lines seen in a REPEAT_TAIL-line non-blank tail
    kill: bool
    out: str


def check(path, **env):
    """Replay a log through `--check` and parse its verdict.

    STORM_* is scrubbed from the ambient environment for the same reason conftest scrubs
    HARNESS_EXIT: these tests must assert the SHIPPED defaults, not whatever the pod exported.
    """
    clean = {k: v for k, v in os.environ.items() if not k.startswith("STORM_")}
    r = subprocess.run(
        ["bash", str(WATCHDOG), "--check", str(path)],
        capture_output=True, text=True, env={**clean, **env},
    )
    assert r.returncode == 0, f"--check failed: {r.returncode}\n{r.stdout}\n{r.stderr}"
    share = re.search(r"worst window: (\d+)%", r.stdout)
    distinct = re.search(r"tightest tail: (\d+) distinct", r.stdout)
    assert share and distinct, f"--check must report both signals, got:\n{r.stdout}"
    return Verdict(int(share.group(1)), int(distinct.group(1)), "WOULD KILL" in r.stdout, r.stdout)


def signals(path, **env):
    """The LIVE poll's two numbers at EOF, through the three shipped shell helpers.

    `--check` no longer shells those helpers per sample — it re-expresses them in one awk pass (#43)
    — so this is the seam that keeps the two from drifting apart.
    """
    clean = {k: v for k, v in os.environ.items() if not k.startswith("STORM_")}
    r = subprocess.run(
        ["bash", str(WATCHDOG), "--signals", str(path)],
        capture_output=True, text=True, env={**clean, **env},
    )
    assert r.returncode == 0, f"--signals failed: {r.returncode}\n{r.stdout}\n{r.stderr}"
    share = re.search(r"share: (\d+)", r.stdout)
    distinct = re.search(r"distinct: (\d+)", r.stdout)
    assert share and distinct, f"--signals must report both, got:\n{r.stdout}"
    return int(share.group(1)), int(distinct.group(1))


class TestSlowLoopsAreCaught:
    """The loops that ran on images already containing #29 and were killed by a human."""

    def test_slow_nag_loop(self, tmp_path):
        """circles#42 r1, 2026-08-07: 65 repeats of one nag line over 2h14m, pod Running throughout.

        One line per completion, so the loop never dilutes a 200-line window past ~32% — the share
        rule alone cannot see this, which is why it ran for 2h14m and banked nothing.
        """
        v = check(write(tmp_path, healthy(140) + [NAG] * 65))
        assert v.share < 50, "the share rule genuinely does not fire here — that is the bug"
        assert v.distinct <= 3
        assert v.kill

    def test_tail_collapses_to_two_lines(self, tmp_path):
        """2026-08-08 00:2xZ, deepseek-v4-flash: last 40 log lines collapse to 2 distinct.

        A two-line cycle tops out at a 50% share only if it is perfectly balanced; diluted over 200
        lines it is nowhere near. The tail is unambiguous.
        """
        cycle = [CI_CLEAN, "─── shell | developer ────────────────────────────────"] * 20
        v = check(write(tmp_path, healthy(160) + cycle))
        assert v.share < 50
        assert v.distinct == 2
        assert v.kill


class TestLoudLoopsStillCaught:
    """The two shapes #29 already killed. Narrowing the rule must not cost these."""

    def test_degenerate_text_repetition(self, tmp_path):
        """circles FU-126 fan-out, deepseek-0731: 1,635 repeats, 42% of the log blank.

        The blank lines are the point — a naive most-frequent-line check calls a healthy log 100%.
        """
        loop = []
        for _ in range(1635):
            loop += [SKELETON, "", ""]
        v = check(write(tmp_path, healthy(120) + loop))
        assert v.share >= 50
        assert v.kill

    def test_terminal_error_retry_storm(self, tmp_path):
        """kimi-k3 r1: 171 circuit-open retries in 18s. From run.log a retry storm IS a repetition
        loop, so this rule covers it; classify() still reports budget-403 because the budget
        signature outranks the marker."""
        v = check(write(tmp_path, healthy(60) + [CIRCUIT] * 171))
        assert v.kill


class TestHealthyRidesSurvive:
    """The regression bar: a narrower window must not start killing honest slow rides."""

    def test_fixture_supplies_the_measured_31_distinct_lines(self, tmp_path):
        """The negative control is only worth the margin it reproduces (#45).

        `HEALTHY` cycles, so a 40-line window covers every entry and the tail signal sees exactly
        as many distinct lines as the fixture has distinct VALUES — not as many as it has entries.
        With one value duplicated the fixture argued the ≤3 threshold against 30, while the header
        of the watchdog (and the comment above the fixture) both pin the measured healthy tail at
        31. Assert the number the walk actually reports, not just the length of the tuple, because
        it is the reported number the rest of this class leans on.
        """
        assert len(set(HEALTHY)) == 31, "the fixture must supply 31 DISTINCT lines"
        assert check(write(tmp_path, healthy(600))).distinct == 31

    def test_healthy_ride(self, tmp_path):
        """mimo/opus shape — 31 distinct lines per 40-line tail, worst share 6% (measured: 8%/0%)."""
        v = check(write(tmp_path, healthy(600)))
        assert v.share < 50
        assert v.distinct > 3, "31 distinct vs a threshold of 3 is the whole margin"
        assert not v.kill

    def test_blank_padded_healthy_ride(self, tmp_path):
        """Half-blank log. Blanks must be dropped BEFORE the tail is taken, or a healthy ride whose
        last 40 raw lines are mostly empty reads as a one-line loop."""
        padded = []
        for line in healthy(400):
            padded += [line, ""]
        v = check(write(tmp_path, padded))
        assert v.distinct > 3
        assert not v.kill

    def test_barely_started_run(self, tmp_path):
        """A run that has printed nothing but its banner must never be killed on it."""
        v = check(write(tmp_path, ["agent-session: starting ride (round 1)"] * 12))
        assert not v.kill

    def test_startup_banner_then_real_work(self, tmp_path):
        """A repeated banner scrolls out of a 40-line tail as soon as work starts."""
        v = check(write(tmp_path, ["=== agent-base image 2026.8.5 ==="] * 30 + healthy(300)))
        assert not v.kill

    def test_rule_is_disabled_by_env(self, tmp_path):
        """STORM_REPEAT_PCT=0 is the documented off switch — it must disable BOTH signals, so the
        knob still means what the header says after this change."""
        v = check(write(tmp_path, healthy(140) + [NAG] * 65), STORM_REPEAT_PCT="0")
        assert not v.kill


class TestReplayIsFaithful:
    """`--check` is the only tool anyone has for arguing about a post-mortem, so it must report the
    windows the live loop actually sees — `tail -n N`, i.e. windows that END on a written line.
    Walking window STARTS manufactures a short, loop-only window at EOF and reports its 100% share,
    which reads as "the rule would have killed this" for logs the live rule silently ignored."""

    def test_share_matches_the_live_tail(self, tmp_path):
        v = check(write(tmp_path, healthy(140) + [NAG] * 65))
        # 65 repeats in the live 200-line tail = 32%. A start-stepping walk reports 61% here.
        assert v.share == 32


# The issue's own fixture: 233 healthy lines, then 40 identical ones (ending at line 273), then 500
# more healthy ones. The burst never reaches EOF, so it is only scoreable at a window end inside it.
BURST_TAIL_END = 273
BURST_TRUNCATED = healthy(233) + [NAG] * 40
BURST = BURST_TRUNCATED + healthy(500, offset=273)


def tail_at(out):
    """The line number `--check` attributes its tightest tail to."""
    m = re.search(r"tightest tail: \d+ distinct at line (\d+)", out)
    assert m, f"--check must report where the tightest tail sits, got:\n{out}"
    return int(m.group(1))


class TestNoBurstHidesBetweenSamples:
    """#43. The walk scores window ENDS, so a burst is seen only if some scored end lands on it —
    and the tail signal's landing zone is TINY. A verdict of ≤3 distinct needs almost all of the
    tail's 40 non-blank lines inside the burst, so the shortest scoreable burst (exactly
    `REPEAT_TAIL` lines) is visible from about five line-ends and no more. Stepping
    `REPEAT_WINDOW/4` = 50 walks straight over it; so would a step of `REPEAT_TAIL` = 40, or any
    other coarse step. Only scoring every end is faithful.

    Why this is the failure worth a test: `--check` is what a human replays a saved log through when
    arguing about a post-mortem, and a false CLEAR there is the harder direction to notice — nobody
    re-derives a `would not kill`. The same log truncated where the burst ends reads WOULD KILL,
    which is the contradiction the issue opened on.
    """

    def test_mid_log_burst_is_scored(self, tmp_path):
        """The burst at lines 234-273 sits between the old samples (200, 250, 300, …) and was lost."""
        v = check(write(tmp_path, BURST))
        assert v.distinct == 1, "the 40 identical lines collapse to one distinct line"
        assert tail_at(v.out) == BURST_TAIL_END
        assert v.kill
        assert v.share < 50, "the tail signal is the one that must fire — 40 of 200 lines is 20%"

    def test_agrees_with_the_same_log_truncated_at_the_burst(self, tmp_path):
        """`--check` reports the WORST view over the walk, so appending healthy lines after a burst
        can never soften the verdict — the truncated log's number must still be reachable."""
        trunc = tmp_path / "trunc"
        trunc.mkdir()
        assert check(write(trunc, BURST_TRUNCATED)).distinct == check(write(tmp_path, BURST)).distinct

    def test_walk_does_not_re_scan_the_log_per_step(self, tmp_path):
        """The other half of the same edit. The shipped walk re-sliced the whole file TWICE per
        window end (~12s on this 52k-line log), so scoring more ends multiplies a cost that was
        already the reviewer's complaint on #42; one pass over the log pays for the fidelity above.

        The bound is deliberately loose — it fails when per-step re-scanning comes back, not when
        the runner is busy.
        """
        t0 = time.monotonic()
        v = check(write(tmp_path, healthy(52_000)))
        assert time.monotonic() - t0 < 5.0, "the walk is re-reading the log per window end again"
        assert not v.kill


# Logs SHORTER than REPEAT_WINDOW, where the walk scores exactly one position — EOF — so `--check`
# and the live helpers must print the same pair of numbers or one of them is wrong.
SHORT_LOGS = {
    "healthy": healthy(150),
    "loop": healthy(5) + [NAG] * 40,
    "half_blank": [x for line in healthy(60) for x in (line, "")],
    "barely_started": ["agent-session: starting ride (round 1)"] * 12,
    "untrimmed": [f"  {line}\t" for line in healthy(80)] + ["   ", "\t\t"],
}


class TestReplayMatchesTheLiveArithmetic:
    """The structural guarantee that #43 traded away, restored as a test.

    Until now `--check` COULD not drift from the live rule, because it shelled the same three
    helpers per sample. That is precisely what made it too slow to sample finely, so the walk is now
    one awk pass that re-states them — trim, drop blanks, share of the last 200 raw lines, distinct
    of the last 40 non-blank ones, and both guards. Two implementations of one rule need a test
    holding them together, or the replay quietly stops describing the thing that kills rides.
    """

    @pytest.mark.parametrize("shape", sorted(SHORT_LOGS))
    def test_same_numbers_as_the_live_helpers(self, tmp_path, shape):
        p = write(tmp_path, SHORT_LOGS[shape])
        v = check(p)
        assert (v.share, v.distinct) == signals(p)

    def test_a_sparse_log_is_unjudgeable_in_both(self, tmp_path):
        """40 identical lines spread one per 30 raw lines. `tail -n 1000` reaches only 33 of them,
        so `_distinct` refuses to judge (999) — and the walk must refuse identically. Dropping that
        guard while porting to awk would report 1 distinct and kill a log the live rule cannot see.
        """
        sparse = []
        for _ in range(40):
            sparse += [NAG] + [""] * 29
        p = write(tmp_path, sparse)
        v = check(p)
        assert v.distinct == 999
        assert not v.kill
        assert signals(p)[1] == 999
