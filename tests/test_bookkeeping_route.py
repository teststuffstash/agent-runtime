"""Which terminal GitHub action a run gets — agent-runtime#49, the third surface of #36.

#36 unified "did THIS ROUND produce the artifact?" behind `died_this_round()` and wired it into
`classify()` and `salvage_push()`. `bookkeeping()` was left branching on `stats["pr_url"]` alone,
so a fix round — which by definition always has a PR from round 2 on — that DIED still armed
auto-merge and posted a stats comment. The run was correctly classified `harness-death` and no
`AGENT_STRIKE:` comment was ever posted, which is the one place a coordinator greps to walk the
model chain on re-dispatch (agents/coordinator/README.md §MODEL). The strike was lost exactly on
the rounds it was invented for.

The routing is three-way, not two, because the #32 issue-link guarantee lives on the PR leg and a
died round with an inherited PR needs it MORE, not less (an unlinked PR reads as "worker terminal,
no PR" to the coordinator scan and gets re-dispatched onto finished work):

| PR? | died this round? | arm + stats comment | AGENT_STRIKE | issue link |
|---|---|---|---|---|
| yes | no  | yes | no  | yes |
| yes | yes | NO  | YES | yes |
| no  | —   | no  | yes | n/a (no PR to link) |

Hermetic: the decision is a pure function over `stats`, so most of this is dict-in, string-out. The
wiring tests stub `shutil.which` + `subprocess.run` and assert on the recorded `gh` argv — no gh,
no network, no remote.

The death-class coverage here reads `failure_signature`'s SOURCE (#61). It used to iterate
`DEATH_EXIT_STATUSES` — the very tuple `bookkeeping_route` consults — which is self-referential: a
fifth signature added to `failure_signature()` alone kept the loop green while the new class took
the pre-#49 route (arm + stats comment, no `AGENT_STRIKE`). The table is hand-maintained, so the
test has to pin it to something the table cannot move.
"""
import ast


def _signature_exit_statuses(source):
    """Every `exit_status` literal `failure_signature()` can return, read out of the source.

    The one thing this must not do is ask the module what it thinks its death classes are — that is
    the bug (#61). So: parse the file, find the function, take the first element of each returned
    tuple. Anything it cannot read as a literal is a hard failure, not a skip; a silently-dropped
    return would restore exactly the blind spot this exists to close.
    """
    fn = next((n for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.FunctionDef) and n.name == "failure_signature"), None)
    assert fn is not None, "agent-finalize has no `failure_signature` — renamed? this pin is blind"

    def own_returns(node):
        """Returns in `failure_signature`'s OWN body — not its nested `n(pat)` helper's, which
        returns a match count and would otherwise read as an unparseable signature."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return):
                yield child
            yield from own_returns(child)

    statuses = set()
    for node in own_returns(fn):
        value = node.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            continue  # `return None` — no machine-detectable failure in the log
        where = f"agent-base/agent-finalize:{node.lineno}"
        assert isinstance(value, ast.Tuple) and value.elts, (
            f"{where}: failure_signature must return (exit_status, error_class) or None")
        first = value.elts[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"{where}: exit_status is not a string literal, so this pin cannot read it. Either keep "
            "it literal or teach `_signature_exit_statuses` to resolve it — do NOT drop the case.")
        statuses.add(first.value)
    assert statuses, "failure_signature returns no signatures at all — the pin read nothing"
    return statuses


class TestBookkeepingRoute:
    """`bookkeeping_route(stats) -> "arm" | "strike" | "deliberate-stop"` — the pure decision."""

    def test_pr_and_clean_arms(self, af):
        """The ordinary green fix round: artifact exists, round ended well."""
        assert af.bookkeeping_route({"pr_url": "http://x/1", "exit_status": "clean"}) == "arm"

    def test_pr_and_ci_red_still_arms(self, af):
        """`ci-failed` is a verdict on the WORK, not a death of the round — the PR still gets
        armed and commented (the updater/review reflex owns the red). Unchanged by #49."""
        assert af.bookkeeping_route({"pr_url": "http://x/1", "exit_status": "ci-failed"}) == "arm"

    def test_pr_and_harness_death_strikes(self, af):
        """THE bug (#49). circles#32 r3 shape: an inherited PR on a round killed by a `-32602`
        truncation. #36 made `classify()` say `harness-death`; bookkeeping still armed it."""
        assert af.bookkeeping_route(
            {"pr_url": "http://x/1", "exit_status": "harness-death"}) == "strike"

    def test_every_death_class_strikes_with_a_pr(self, af, af_source):
        """Every class `failure_signature()` can return through `died_this_round()` — enumerated
        from ITS SOURCE, not from `DEATH_EXIT_STATUSES` (#61). Iterating the tuple that
        `bookkeeping_route` itself consults proves only that the module agrees with itself; a fifth
        signature added to `failure_signature()` alone would arm auto-merge here and never be
        noticed. Read from the source, the new class fails this loop the day it lands."""
        for status in sorted(_signature_exit_statuses(af_source) - {"no-artifact"}):
            assert af.bookkeeping_route(
                {"pr_url": "http://x/1", "exit_status": status}) == "strike", (
                f"`{status}` is a failure_signature death class that still arms auto-merge — "
                "add it to DEATH_EXIT_STATUSES (#49 re-opened for the new class)")

    def test_death_table_is_exactly_the_signatures_minus_no_artifact(self, af, af_source):
        """The table pin itself: `DEATH_EXIT_STATUSES ∪ {no-artifact}` == the signature literals.

        Set-EQUALITY, both directions. A missing entry re-opens #49 for the new class (the loop
        above catches that too); a stale extra entry is a status nothing can produce, which quietly
        strikes a run on a class `classify()` assigns for some other reason. `no-artifact` is the
        one deliberate exclusion — see `died_this_round`."""
        assert _signature_exit_statuses(af_source) == set(af.DEATH_EXIT_STATUSES) | {"no-artifact"}

    def test_no_artifact_is_not_a_death(self, af):
        """`no-artifact`/token-expiry is a claim about the ARTIFACT ("green work never reached
        GitHub"), which an existing PR contradicts — `died_this_round()` excludes it on purpose
        and so must this. Same exclusion, same reason, same answer."""
        assert "no-artifact" not in af.DEATH_EXIT_STATUSES
        assert af.bookkeeping_route(
            {"pr_url": "http://x/1", "exit_status": "no-artifact"}) == "arm"

    def test_no_pr_strikes(self, af):
        """The first-round shape — unchanged."""
        assert af.bookkeeping_route({"exit_status": "harness-death"}) == "strike"

    def test_no_pr_and_deliberate_stop_reports(self, af):
        """A worker that stopped on its own judgment is not a strike (retro r1 F2)."""
        assert af.bookkeeping_route({"exit_status": "blocked-deliberate"}) == "deliberate-stop"


class TestBookkeepingWiring:
    """`bookkeeping()` actually routes on that answer — the pure function is not the bug unless it
    is wired in. Asserts on the `gh` argv the function would have run."""

    def _run(self, af, monkeypatch, log_path, stats):
        calls = []

        class _Done:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def _fake_run(argv, **kw):
            calls.append(tuple(argv))
            # Canned answers for the two ADR-103 reads (#62), so the arm leg gets far enough to
            # emit its channels here too. `[]` is a READABLE empty timeline — an empty stdout is
            # not, and `post_summary_event` fails closed on it by design; the channel's own cases
            # live in tests/test_summary_channel.py.
            if argv[1:3] == ["pr", "view"]:
                return _Done(0, "deadbee\n")
            if "api" in argv and "--method" not in argv:
                return _Done(0, "[]")
            return _Done()

        monkeypatch.setattr(af.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        monkeypatch.setattr(af, "_fresh_gh_env", lambda: {})
        monkeypatch.setenv("AGENT_TASK", "issue-49")
        monkeypatch.setenv("REPO_URL", "https://github.com/o/r")
        monkeypatch.setenv("MODEL", "some/model")
        monkeypatch.setenv("AGENT_ROUND", "3")
        af.bookkeeping(stats, log_path)
        return calls

    @staticmethod
    def _find(calls, *prefix):
        return [c for c in calls if c[1:1 + len(prefix)] == prefix]

    def test_clean_round_with_a_pr_arms_and_comments(self, af, monkeypatch, logfile):
        """The path that must NOT change — except for WHERE the stats land (#62/ADR-103): the
        table is now the `agent-ride` check-run and the index line is an append to the single
        `<!-- agent-summary -->` comment, so there is no `gh pr comment` any more. The routing
        (arm + stats channel, no strike) and both `*_by_pod` flags are unchanged."""
        stats = {"pr_url": "https://github.com/o/r/pull/1", "exit_status": "clean", "pod": "p"}
        calls = self._run(af, monkeypatch, logfile("all fine\n"), stats)
        assert self._find(calls, "pr", "merge")
        assert not self._find(calls, "pr", "comment")
        assert not self._find(calls, "issue", "comment")
        assert stats.get("armed_by_pod") is True
        assert stats.get("stats_comment_by_pod") is True

    def test_died_round_with_an_inherited_pr_strikes_instead_of_arming(
            self, af, monkeypatch, logfile):
        """#49, end to end: no arm, no stats comment, an `AGENT_STRIKE:` comment on the issue."""
        stats = {"pr_url": "http://x/1", "exit_status": "harness-death",
                 "error_class": "goose-32602-truncation", "pod": "p"}
        calls = self._run(af, monkeypatch, logfile("boom\n"), stats)
        assert not self._find(calls, "pr", "merge")
        assert not self._find(calls, "pr", "comment")
        # False, not absent: the launcher fallback arms whatever the pod left UNKNOWN, and a died
        # round's PR must stay un-armed there too. Absent would mean "we never got to it".
        assert stats.get("armed_by_pod") is False
        assert "stats_comment_by_pod" not in stats
        posted = self._find(calls, "issue", "comment")
        assert len(posted) == 1
        assert stats.get("strike_by_pod") is True

    def test_the_strike_first_line_stays_byte_stable(self, af, monkeypatch, logfile):
        """The coordinator greps this line to walk the model chain — format is a contract, and a
        died round with a PR must produce the SAME one as a died round without."""
        stats = {"pr_url": "http://x/1", "exit_status": "harness-death",
                 "error_class": "goose-32602-truncation", "pod": "pod-7"}
        calls = self._run(af, monkeypatch, logfile("boom\n"), stats)
        body = self._find(calls, "issue", "comment")[0][-1]
        assert body.splitlines()[0] == (
            "AGENT_STRIKE: model=some/model error_class=goose-32602-truncation "
            "round=3 session=pod-7")

    def test_a_died_round_still_guarantees_the_issue_link(self, af, monkeypatch, logfile):
        """#32 holds on both legs: the artifact is real either way, and an unlinked PR gets the
        issue re-dispatched onto finished work."""
        stats = {"pr_url": "http://x/1", "exit_status": "harness-death",
                 "error_class": "goose-panic", "pod": "p"}
        calls = self._run(af, monkeypatch, logfile("boom\n"), stats)
        edited = self._find(calls, "pr", "edit")
        assert len(edited) == 1
        assert edited[0][-1].startswith("Implements #49")
        assert stats.get("issue_link_added_by_pod") is True

    def test_died_round_respects_no_arm(self, af, monkeypatch, logfile):
        """AGENT_ARM_PR=0 (human-gated roles) must stay un-armed on the death path too — and the
        skip is recorded, so the launcher fallback does not arm it either."""
        monkeypatch.setenv("AGENT_ARM_PR", "0")
        stats = {"pr_url": "http://x/1", "exit_status": "harness-death",
                 "error_class": "goose-panic", "pod": "p"}
        calls = self._run(af, monkeypatch, logfile("boom\n"), stats)
        assert not self._find(calls, "pr", "merge")
        assert stats.get("armed_by_pod") is False
