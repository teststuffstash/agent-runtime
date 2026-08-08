"""The strike comment's resumable-branch answer — agent-runtime#33.

The coordinator reads the strike comment to decide whether to re-dispatch with `--work-branch`.
The question it is asking is **issue-scoped** ("is there anything to resume for this issue?");
finalize used to answer a **round-scoped** one (`stats["salvaged_branch"]`, set only when THIS
round pushed something). The two agree on the first round and diverge on every resumed round that
fails — exactly the situation where resuming matters most.

circles#22, 2026-08-05, verbatim:

| round | what happened | strike comment | verdict |
|---|---|---|---|
| r1 | truncation, finalize salvaged `agent/20260805-130300` (1 commit, +1051) | "with a resumable branch ... pushed" | correct |
| r2 | same truncation, RESUMED that branch, added no commit | "(no resumable branch — nothing was committed)" | **false about the issue** |

r2's sentence is true about the round and false about the issue, and it fails in the direction that
loses data: the next dispatch restarts from base and discards the banked +1051.

Hermetic: the decision lives in a pure function over (stats, env), so these tests are dict-in,
string-out. The one salvage test that touches `subprocess` stubs it — no git, no remote, no network.
"""
import pytest


class TestResumableBranch:
    """`resumable_branch(stats, env) -> (kind, branch)` — the issue-scoped answer."""

    def test_round_that_pushed_reports_that_branch(self, af):
        """circles#22 r1: salvage pushed, so the round's own branch is the answer."""
        kind, branch = af.resumable_branch({"salvaged_branch": "agent/20260805-130300"}, env={})
        assert (kind, branch) == ("pushed", "agent/20260805-130300")

    def test_dispatched_branch_answers_a_round_that_committed_nothing(self, af):
        """circles#22 r2 — THE bug. Dispatched with `--work-branch B`, struck without committing.

        B existed on entry and still exists; it holds the entire deliverable. The comment must
        name it.
        """
        kind, branch = af.resumable_branch({}, env={"WORK_BRANCH": "agent/20260805-130300"})
        assert (kind, branch) == ("resuming", "agent/20260805-130300")

    def test_pushed_wins_over_dispatched_when_both_are_known(self, af):
        """A resumed round that DID commit: "pushed" is the stronger, more recent statement."""
        kind, branch = af.resumable_branch(
            {"salvaged_branch": "agent/x"}, env={"WORK_BRANCH": "agent/x"})
        assert (kind, branch) == ("pushed", "agent/x")

    def test_undetermined_is_unknown_not_none(self, af):
        """Failure direction (pre-decided in #33): an unknown is not a "no".

        A false "nothing to resume" discards work; a false "resumable" wastes a checkout.
        """
        kind, branch = af.resumable_branch({"salvage_undetermined": True}, env={})
        assert kind == "unknown"
        assert branch is None

    def test_nothing_anywhere_is_none(self, af):
        """A fresh round that reached a work branch and genuinely banked nothing."""
        assert af.resumable_branch({}, env={}) == ("none", None)

    def test_blank_values_are_not_branches(self, af):
        """`WORK_BRANCH=` in the pod env must not become a branch named "" (#33: never guess)."""
        assert af.resumable_branch({"salvaged_branch": ""}, env={"WORK_BRANCH": "  "}) == ("none", None)

    def test_env_defaults_to_the_process_environment(self, af, monkeypatch):
        """finalize calls it with no env argument; the pod's WORK_BRANCH is the live signal."""
        monkeypatch.setenv("WORK_BRANCH", "agent/from-env")
        assert af.resumable_branch({}) == ("resuming", "agent/from-env")


class TestStrikeNote:
    """`resumable_note(stats, env)` — the sentence pasted into the strike comment.

    #33 pre-decided that the cases must be DISTINGUISHABLE: "resuming B (no new commits)" is
    materially different from "pushed B", and the coordinator play can act on the distinction.
    """

    def test_resumed_round_names_the_branch_and_never_denies_it(self, af):
        """The acceptance case: dispatched with B, struck without committing → B is named."""
        note = af.resumable_note({}, env={"WORK_BRANCH": "agent/20260805-130300"})
        assert "agent/20260805-130300" in note
        assert "no resumable branch" not in note

    def test_resumed_round_says_it_added_nothing_this_round(self, af):
        """Distinguishable from a push: the branch is unchanged, so say so."""
        note = af.resumable_note({}, env={"WORK_BRANCH": "agent/b"}).lower()
        assert "resum" in note
        assert "no new commits" in note

    def test_pushed_round_still_says_pushed(self, af):
        """The r1 wording is correct and load-bearing — don't regress it into the resumed one."""
        note = af.resumable_note({"salvaged_branch": "agent/b"}, env={})
        assert "pushed" in note.lower()
        assert "agent/b" in note

    def test_unknown_never_asserts_there_is_none(self, af):
        note = af.resumable_note({"salvage_undetermined": True}, env={})
        assert "unknown" in note.lower()
        assert "no resumable branch" not in note

    def test_none_is_still_said_plainly(self, af):
        """When finalize genuinely knows there is nothing, it must still say so."""
        assert "no resumable branch" in af.resumable_note({}, env={})


class TestDeliberateStopReport:
    """The SECOND consumer of the same answer: the `blocked-deliberate` report (#33 round 2).

    `bookkeeping()` posts two different issue comments and the coordinator reads both to decide
    whether to re-dispatch with `--work-branch`. The strike comment was fixed above; this report
    inlined the round-scoped `stats["salvaged_branch"]` check, so circles#22 r2 reaches the same
    data loss through the other door — and worse: on an unset `salvaged_branch` it appended NOTHING,
    and the coordinator acts on the line's PRESENCE, so an omitted line and a false
    "(no resumable branch)" produce the identical decision — fork from base, discard the branch.

    The body is built by a pure function so this stays hermetic — no `gh`, no network. The report's
    *wording* may differ from the strike's (a deliberate stop is not a strike); the *answer* may not.
    """

    LOG = "…tail…\n"

    def test_resumed_round_that_stopped_deliberately_names_the_branch(self, af):
        """The acceptance case from #33 round 2: dispatched with B, stopped deliberately, no
        commit → the report names B."""
        body = af.deliberate_stop_body({}, self.LOG, env={"WORK_BRANCH": "agent/20260805-130300"})
        assert "agent/20260805-130300" in body
        assert "no resumable branch" not in body

    def test_the_none_case_is_said_out_loud_not_omitted(self, af):
        """Silence is not safer than a false negative when the consumer treats absence as "none"."""
        body = af.deliberate_stop_body({}, self.LOG, env={})
        assert "no resumable branch" in body

    def test_pushed_round_still_reports_the_push(self, af):
        body = af.deliberate_stop_body({"salvaged_branch": "agent/b"}, self.LOG, env={})
        assert "pushed" in body.lower()
        assert "agent/b" in body

    def test_undetermined_never_asserts_there_is_none(self, af):
        """Failure direction survives on this path too: an unknown is not a "no"."""
        body = af.deliberate_stop_body({"salvage_undetermined": True}, self.LOG, env={})
        assert "unknown" in body.lower()
        assert "no resumable branch" not in body

    @pytest.mark.parametrize("stats,env", [
        ({}, {"WORK_BRANCH": "agent/b"}),
        ({"salvaged_branch": "agent/b"}, {}),
        ({"salvage_undetermined": True}, {}),
        ({}, {}),
    ])
    def test_one_source_for_both_call_sites(self, af, stats, env):
        """The point of the round-2 fix: this report does not re-derive the answer, it shares it.

        A second inlined check that happens to agree today is what produced this bug the first time.
        """
        assert af.resumable_note(stats, env=env) in af.deliberate_stop_body(stats, self.LOG, env=env)

    def test_the_report_still_says_what_it_always_said(self, af):
        """Guard the extraction: the coordinator matches `AGENT_REPORT: deliberate stop` and reads
        model/round/pod and the log tail out of this body."""
        body = af.deliberate_stop_body(
            {"pod": "agent-pod-1", "model": "stats-model"}, self.LOG,
            env={"MODEL": "env-model", "AGENT_ROUND": "3"})
        assert body.startswith("AGENT_REPORT: deliberate stop")
        assert "model=env-model" in body      # the pod's MODEL wins over the parsed one, as before
        assert "round=3" in body
        assert "session=agent-pod-1" in body
        assert self.LOG in body

    def test_model_falls_back_to_the_parsed_stats_value(self, af):
        body = af.deliberate_stop_body({"model": "stats-model"}, self.LOG, env={})
        assert "model=stats-model" in body
        assert "round=1" in body  # the unset-AGENT_ROUND default, unchanged


class TestSalvageMarksUnknown:
    """`salvage_push` is what knows a branch could not be confirmed — it must record that.

    Without a marker the "unknown" case above is unreachable in production and every failed
    salvage collapses back into the false "nothing was committed".
    """

    def test_unreadable_workdir_is_undetermined(self, af, monkeypatch, tmp_path):
        """No `.git` at teardown: finalize can see nothing, so it knows nothing. No subprocess
        runs on this path."""
        monkeypatch.setenv("WORKDIR", str(tmp_path))
        stats = {}
        af.salvage_push(stats)
        assert stats.get("salvage_undetermined") is True
        assert "salvaged_branch" not in stats

    def test_failed_push_is_undetermined_not_silence(self, af, monkeypatch, tmp_path):
        """Commits exist locally but the push was rejected — the remote state is unknown."""
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("WORKDIR", str(tmp_path))
        monkeypatch.setenv("BASE_REF", "master")

        class _Done:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def _fake_run(argv, **kw):
            if "rev-parse" in argv:
                return _Done(out="agent/20260805-130300\n")
            if "status" in argv:
                return _Done(out="")
            if "rev-list" in argv:
                return _Done(out="1\n")
            if "push" in argv:
                return _Done(rc=1, err="remote: rejected")
            return _Done()

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        stats = {}
        af.salvage_push(stats)
        assert "salvaged_branch" not in stats
        assert stats.get("salvage_undetermined") is True

    def test_a_clean_work_branch_with_nothing_ahead_is_a_real_none(self, af, monkeypatch, tmp_path):
        """The honest "nothing was committed": on a work branch, zero commits ahead of base.

        This case must NOT be smeared into "unknown" — then the note would never say "none".
        """
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("WORKDIR", str(tmp_path))

        class _Done:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def _fake_run(argv, **kw):
            if "rev-parse" in argv:
                return _Done(out="agent/fresh\n")
            if "rev-list" in argv:
                return _Done(out="0\n")
            return _Done(out="")

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        stats = {}
        af.salvage_push(stats)
        assert stats == {}


class TestSalvageGuardOnAFixRound:
    """agent-runtime#36's second surface: `salvage_push`'s early return on `pr_url`.

    Same root as the classify() bug — "a pr_url exists" read as "this round succeeded" — and the
    same fix: the run log decides. A death signature with no end-of-run report means this round
    did not finish, so its working tree still has to be banked; anything else leaves the guard
    exactly where it was.
    """

    def test_a_died_round_salvages_even_though_a_pr_url_exists(self, af, monkeypatch, tmp_path):
        """agent-runtime#36's second surface: the guard's "a pr_url exists" is not "this round
        succeeded".

        On a FIX round a PR always exists — that is what a fix round is — so the early return
        disabled salvage for exactly the rounds that iterate on review feedback. The run log is
        the tiebreak: a death signature with no end-of-run report means this round did not finish,
        whatever PR its predecessor left behind. circles#32 r3 (2026-08-06) is the case.
        """
        (tmp_path / ".git").mkdir()
        log = tmp_path / "run.log"
        log.write_text("-32602: the response may have been truncated.\n", encoding="utf-8")
        monkeypatch.setenv("WORKDIR", str(tmp_path))
        monkeypatch.setenv("BASE_REF", "master")

        class _Done:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def _fake_run(argv, **kw):
            if "rev-parse" in argv:
                return _Done(out="fix/issue-32-arcs\n")
            if "status" in argv:
                return _Done(out=" M tests/test_render.py\n")
            if "rev-list" in argv:
                return _Done(out="1\n")
            return _Done()

        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        stats = {"pr_url": "https://github.com/x/circles/pull/39"}
        af.salvage_push(stats, str(log))
        assert stats.get("salvaged_branch") == "fix/issue-32-arcs"

    def test_a_round_that_reported_its_own_end_still_skips_salvage(self, af, monkeypatch, tmp_path):
        """The guard the fix must KEEP: a round that opened a PR and reported its end is done.

        Salvaging there would `git add -A` whatever junk the tree holds and force-push a
        wip commit onto a PR that is already under review. Nothing may run on this path — not
        even `git status`.
        """
        (tmp_path / ".git").mkdir()
        log = tmp_path / "run.log"
        log.write_text("all fine\n", encoding="utf-8")
        monkeypatch.setenv("WORKDIR", str(tmp_path))

        def _boom(*a, **kw):  # noqa: ARG001 — the point is that it is never called
            raise AssertionError("salvage must not touch git on a round that reported success")

        monkeypatch.setattr(af.subprocess, "run", _boom)
        stats = {"pr_url": "https://github.com/x/circles/pull/39", "ci_passed": True,
                 "reproduced": True}
        af.salvage_push(stats, str(log))
        assert stats == {"pr_url": "https://github.com/x/circles/pull/39", "ci_passed": True,
                         "reproduced": True}

    def test_no_log_to_read_keeps_the_pr_url_skip(self, af, monkeypatch, tmp_path):
        """No log path (the arg finalize passes can be empty): no death is detectable, so the
        conservative pre-#36 behaviour stands rather than force-pushing on a guess."""
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("WORKDIR", str(tmp_path))

        def _boom(*a, **kw):
            raise AssertionError("salvage must not touch git when no death is detectable")

        monkeypatch.setattr(af.subprocess, "run", _boom)
        af.salvage_push({"pr_url": "https://github.com/x/circles/pull/39"})
