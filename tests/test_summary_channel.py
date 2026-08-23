"""ADR-103 leg 2 (agent-runtime#62, homelab#210): the run-stats table leaves the PR conversation.

Before this, every machine event on a PR was another comment: the launcher's "picking this up
(round N)", finalize's run-stats table, the strike, the deferral. A three-round ride buried the
human review under machine residue, and "one round = one more comment" was load-bearing for the
coordinator's round counter — a counter that broke the moment anything else commented.

Two channels replace it:
  - **the `agent-ride` check-run** carries the run-stats table (markdown output, checks tab,
    `conclusion=neutral` so an informational check never colours mergeability);
  - **one `<!-- agent-summary -->` comment per timeline** carries a one-line index entry per
    machine event, APPENDED in place, never re-posted.

The homelab twin (PR #219, merged 2026-08-09) ships the reference implementation as
`agents/machine-comment.sh` and is already emitting these shapes from the launcher fallback leg.
This side must match them byte-for-byte: they are the machine interface, not cosmetics.

⚠ `kind=stats` is LOAD-BEARING. `agents/coordinator-scan.sh` counts
`<!-- agent-event kind=stats ts=([^ ]+) -->` inside the summary comment as the **round counter**.
A different kind string, a missing marker, or a REPLACING write (which would erase round 1's
marker) silently pins ci-red `attempts` at 0 — the exact red-loop livelock `RED_ROUNDS_MAX` bounds.
Hence: the shape tests below are byte-exact, and the append tests assert on the round-1 marker
SURVIVING and on the comment COUNT, not just on the content.

Hermetic, and the fixtures are the recorded world: `FakeGitHub` answers the three REST endpoints
ADR-103 touches from an in-memory store, and every timestamp is passed in. No network, no clock,
no `gh` — the seams are the point (deliverable 4).
"""
import json
import re

import pytest

# The regex `agents/coordinator-scan.sh` counts. Copied here verbatim: if a change to the line
# shape stops this matching, the round counter is already broken on the homelab side.
SCAN_ROUND_COUNTER = re.compile(r"<!-- agent-event kind=stats ts=([^ ]+) -->")

TS1 = "2026-08-09T12:45:51Z"
TS2 = "2026-08-09T13:11:02Z"


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeGitHub:
    """The recorded world: `gh api` over an in-memory issue timeline + check-run store.

    Serves exactly the calls ADR-103 makes — list/create/patch issue comments, create a check-run,
    read a PR head sha — and records every argv + stdin so a test can assert on COUNTS (one create,
    one patch, zero `pr comment`) rather than on prose.
    """

    def __init__(self, comments=None, list_rc=0, list_raw=None, create_rc=0, patch_rc=0,
                 check_rc=0, sha_rc=0, sha="deadbee"):
        self.comments = [dict(c) for c in (comments or [])]
        self.check_runs = []
        self.calls = []
        self.list_rc, self.list_raw = list_rc, list_raw
        self.create_rc, self.patch_rc, self.check_rc = create_rc, patch_rc, check_rc
        self.sha_rc, self.sha = sha_rc, sha
        self._next_id = 900

    # -- the seam agent-finalize calls: gh(*argv, stdin=..., timeout=...) -----------------------
    def __call__(self, *args, stdin=None, timeout=30):
        self.calls.append((tuple(args), stdin))
        argv = list(args)
        if argv[:2] == ["pr", "view"]:
            if "headRefOid" in argv:
                return _Done(self.sha_rc, "" if self.sha_rc else self.sha + "\n")
            return _Done(0, "a PR body naming no issue\n")  # the #32 issue-link read
        if argv[0] != "api":
            return _Done(0)
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        path = [a for a in argv[1:] if not a.startswith("-") and a != method][0]
        body = (json.loads(stdin) if stdin else {}).get("body")
        if path.endswith("/check-runs"):
            if self.check_rc:
                return _Done(self.check_rc, "", "HTTP 403: Resource not accessible by integration")
            self.check_runs.append(json.loads(stdin))
            return _Done(0, json.dumps({"id": 7}))
        if method == "GET":
            if self.list_rc:
                return _Done(self.list_rc, "", "HTTP 502")
            if self.list_raw is not None:
                return _Done(0, self.list_raw)
            return _Done(0, json.dumps(self.comments))
        if method == "POST":
            if self.create_rc:
                return _Done(self.create_rc, "", "HTTP 403")
            self._next_id += 1
            self.comments.append({"id": self._next_id, "node_id": "IC_node%d" % self._next_id,
                                  "created_at": "2026-08-09T14:00:00Z", "body": body})
            return _Done(0, json.dumps(self.comments[-1]))
        if method == "PATCH":
            if self.patch_rc:
                return _Done(self.patch_rc, "", "HTTP 403")
            cid = int(path.rsplit("/", 1)[-1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body
                    return _Done(0, json.dumps(c))
            return _Done(1, "", "HTTP 404")
        return _Done(0)

    # -- assertion helpers ----------------------------------------------------------------------
    def n_comment_creates(self):
        return len([c for (c, _s) in self.calls
                    if "POST" in c and any(a.endswith("/comments") for a in c)])

    def n_comment_patches(self):
        return len([c for (c, _s) in self.calls if "PATCH" in c])

    def n_check_runs(self):
        return len([c for (c, _s) in self.calls if any(a.endswith("/check-runs") for a in c)])

    def summary_body(self):
        marked = [c for c in self.comments if "<!-- agent-summary -->" in (c["body"] or "")]
        assert len(marked) == 1, "expected exactly one summary comment, got %d" % len(marked)
        return marked[0]["body"]


class TestEventLineShape:
    """`summary_event_line()` — byte-exact, because the launcher writes the same bytes."""

    def test_the_line_is_the_homelab_shape(self, af):
        """`- \\`<ISO8601Z>\\` · <markdown> <!-- agent-event kind=<kind> ts=<ISO8601Z> -->`, one
        line, marker last. Compared literally against the shape homelab#219 emits."""
        line = af.summary_event_line("**run stats (round 2)** — `clean`", "stats", TS1)
        assert line == (
            "- `2026-08-09T12:45:51Z` · **run stats (round 2)** — `clean` "
            "<!-- agent-event kind=stats ts=2026-08-09T12:45:51Z -->")

    def test_the_stats_marker_is_what_the_scan_counts(self, af):
        """The round counter's regex must match, and capture the timestamp."""
        line = af.summary_event_line("anything", "stats", TS1)
        assert SCAN_ROUND_COUNTER.findall(line) == [TS1]

    def test_a_folded_table_keeps_the_marker_on_the_first_line(self, af):
        """Degraded mode (check-run refused, point 5): the table folds INTO this one entry — never
        a second comment. The marker stays on the first physical line so a line-oriented grep
        (the scan's) is unaffected, and the fold rides as an indented list continuation."""
        line = af.summary_event_line("**run stats**", "stats", TS1, fold="| metric | value |\n")
        first = line.splitlines()[0]
        assert first.endswith("<!-- agent-event kind=stats ts=%s -->" % TS1)
        assert SCAN_ROUND_COUNTER.findall(line) == [TS1]
        assert "<details><summary>run stats (check-run unavailable" in line
        assert "| metric | value |" in line


class TestCommentPageParsing:
    """`parse_comment_pages()` — `gh api --paginate` concatenates one JSON array per page.

    Fail-closed is the whole contract here (point 3): an unreadable timeline must return None so
    the caller REFUSES to create. A probe that fails into "[]" would mint a second summary comment
    on every event — the exact residue this work removes (rule #6, the probe-fails-into-a-value
    class).
    """

    def test_one_page(self, af):
        assert af.parse_comment_pages('[{"id": 1}]') == [{"id": 1}]

    def test_pages_are_flattened_in_order(self, af):
        assert af.parse_comment_pages('[{"id": 1}]\n[{"id": 2}]') == [{"id": 1}, {"id": 2}]

    def test_an_empty_page_is_an_empty_timeline(self, af):
        assert af.parse_comment_pages("[]") == []

    def test_empty_output_is_unreadable_not_empty(self, af):
        assert af.parse_comment_pages("") is None
        assert af.parse_comment_pages("   \n") is None

    def test_a_non_array_payload_is_unreadable(self, af):
        """An API error body (`{"message": "Not Found"}`) is not an empty timeline."""
        assert af.parse_comment_pages('{"message": "Not Found"}') is None

    def test_garbage_is_unreadable(self, af):
        assert af.parse_comment_pages("<html>502</html>") is None


class TestSummaryPick:
    """`summary_pick()` — find-or-create's find half."""

    def test_none_when_unmarked(self, af):
        assert af.summary_pick([{"id": 1, "body": "AGENT_STRIKE: model=x"}]) is None

    def test_finds_the_marked_comment(self, af):
        got = af.summary_pick([{"id": 1, "body": "hi"},
                               {"id": 2, "body": "<!-- agent-summary -->\nx"}])
        assert got["id"] == 2

    def test_ties_break_on_the_oldest(self, af):
        """A race (or a human paste of the marker) must converge back onto the FIRST comment
        instead of alternating between two — `sort_by(.created_at) | .[0]`."""
        got = af.summary_pick([
            {"id": 9, "created_at": "2026-08-09T13:00:00Z", "body": "<!-- agent-summary -->b"},
            {"id": 4, "created_at": "2026-08-09T12:00:00Z", "body": "<!-- agent-summary -->a"},
        ])
        assert got["id"] == 4

    def test_a_malformed_entry_does_not_abort_the_scan(self, af):
        got = af.summary_pick(["nonsense", None, {"id": 3, "body": "<!-- agent-summary -->"}])
        assert got["id"] == 3


class TestPostSummaryEvent:
    """`post_summary_event()` — find-or-create + APPEND, over the fake world."""

    def test_first_touch_creates_exactly_one_comment(self, af):
        gh = FakeGitHub(comments=[{"id": 1, "created_at": TS1, "body": "a human review"}])
        assert af.post_summary_event(gh, "o/r", "62", "**run stats**", "stats", TS1) is True
        assert gh.n_comment_creates() == 1
        assert gh.n_comment_patches() == 0
        body = gh.summary_body()
        assert body.startswith("<!-- agent-summary -->")
        assert SCAN_ROUND_COUNTER.findall(body) == [TS1]

    def test_a_later_event_edits_and_never_adds(self, af):
        """Deliverable 2, and the round counter: round 2 APPENDS, so round 1's marker survives and
        the scan reads attempts=2. A replacing write would pin it at 1 forever."""
        gh = FakeGitHub()
        af.post_summary_event(gh, "o/r", "62", "**round 1**", "stats", TS1)
        af.post_summary_event(gh, "o/r", "62", "**round 2**", "stats", TS2)
        assert gh.n_comment_creates() == 1
        assert gh.n_comment_patches() == 1
        body = gh.summary_body()
        assert SCAN_ROUND_COUNTER.findall(body) == [TS1, TS2]
        assert "**round 1**" in body and "**round 2**" in body

    def test_the_patch_uses_the_rest_numeric_id(self, af):
        """A GraphQL node id 404s `repos/{slug}/issues/comments/{id}` — point 3."""
        gh = FakeGitHub(comments=[{"id": 8899, "node_id": "IC_kwDO_node",
                                   "created_at": TS1, "body": "<!-- agent-summary -->\nhdr"}])
        assert af.post_summary_event(gh, "o/r", "62", "**stats**", "stats", TS2) is True
        patched = [c for (c, _s) in gh.calls if "PATCH" in c]
        assert len(patched) == 1
        assert any(a.endswith("repos/o/r/issues/comments/8899") for a in patched[0])
        assert not any("IC_kwDO_node" in a for a in patched[0])

    def test_an_unreadable_timeline_creates_nothing(self, af):
        """Fail closed (point 3): a probe failure that CREATED would mint a second summary comment
        on every event. Return non-zero, write nothing."""
        gh = FakeGitHub(list_raw="<html>502 Bad Gateway</html>")
        assert af.post_summary_event(gh, "o/r", "62", "**stats**", "stats", TS1) is False
        assert gh.n_comment_creates() == 0
        assert gh.n_comment_patches() == 0

    def test_a_failed_read_creates_nothing(self, af):
        gh = FakeGitHub(list_rc=1)
        assert af.post_summary_event(gh, "o/r", "62", "**stats**", "stats", TS1) is False
        assert gh.n_comment_creates() == 0

    def test_a_refused_write_is_reported_not_raised(self, af):
        """Bookkeeping never fails a ride (point 3, last rule)."""
        gh = FakeGitHub(create_rc=1)
        assert af.post_summary_event(gh, "o/r", "62", "**stats**", "stats", TS1) is False

    def test_a_refused_patch_is_reported_not_raised(self, af):
        gh = FakeGitHub(comments=[{"id": 5, "created_at": TS1,
                                   "body": "<!-- agent-summary -->\nhdr"}], patch_rc=1)
        assert af.post_summary_event(gh, "o/r", "62", "**stats**", "stats", TS2) is False


class TestCheckRun:
    """`post_check_run()` — deliverable 1, the shape homelab#219 fixed."""

    def test_the_payload_is_the_adr103_shape(self, af):
        gh = FakeGitHub(sha="abc123")
        assert af.post_check_run(gh, "o/r", "https://github.com/o/r/pull/9", "issue-62",
                                 "| metric | value |\n") is True
        assert gh.n_check_runs() == 1
        payload = gh.check_runs[0]
        assert payload["name"] == "agent-ride"
        assert payload["head_sha"] == "abc123"
        assert payload["status"] == "completed"
        # `neutral`: an informational check must never colour mergeability.
        assert payload["conclusion"] == "neutral"
        assert payload["output"]["title"] == "agent ride — issue-62"
        assert payload["output"]["summary"] == "| metric | value |\n"

    def test_a_refused_check_run_is_false_not_fatal(self, af):
        """Check-runs are App-token-only; a classic PAT 403s whatever its scopes (point 5)."""
        gh = FakeGitHub(check_rc=1)
        assert af.post_check_run(gh, "o/r", "https://github.com/o/r/pull/9", "issue-62", "t") is False

    def test_no_head_sha_means_no_check_run(self, af):
        """Never POST a check-run against a guessed sha."""
        gh = FakeGitHub(sha_rc=1)
        assert af.post_check_run(gh, "o/r", "https://github.com/o/r/pull/9", "issue-62", "t") is False
        assert gh.n_check_runs() == 0


class TestEmitRunStats:
    """`emit_run_stats()` — the two channels together, and the `stats_comment_by_pod` pact.

    The flag is a SUPPRESSION CONTRACT with the merged homelab launcher: `agents/agent-session.sh`
    skips its entire fallback bookkeeping leg only when `armed_by_pod` AND `stats_comment_by_pod`
    are both true. So it may only be set when the pod actually emitted the ADR-103 pair.
    """

    STATS = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "clean",
             "model": "some/model", "harness": "goose", "cost_usd": 0.42, "duration_s": 128,
             "ci_passed": True, "pod": "pod-7", "project": "agent-runtime"}

    def test_the_happy_path_emits_the_pair_and_no_pr_comment(self, af):
        stats = dict(self.STATS)
        gh = FakeGitHub()
        assert af.emit_run_stats(gh, stats, "issue-62", "o/r", now=TS1) is True
        assert gh.n_check_runs() == 1
        assert gh.n_comment_creates() == 1
        # The residue this issue removes: the old `gh pr comment <url> --body <table>`.
        assert not [c for (c, _s) in gh.calls if c[:2] == ("pr", "comment")]
        assert stats.get("stats_comment_by_pod") is True

    def test_the_table_rides_the_check_run_not_the_comment(self, af):
        stats = dict(self.STATS)
        gh = FakeGitHub()
        af.emit_run_stats(gh, stats, "issue-62", "o/r", now=TS1)
        summary = gh.check_runs[0]["output"]["summary"]
        assert "| model | `some/model` (goose) |" in summary
        assert "$0.42" in summary
        assert "s3://agent-transcripts/agent-runtime/issue-62/" in summary
        # The index line is an index, not a second copy of the table.
        assert "| metric | value |" not in gh.summary_body()

    def test_a_refused_check_run_folds_into_the_one_line(self, af):
        """Point 5: degrade INTO the summary line, never into a second comment."""
        stats = dict(self.STATS)
        gh = FakeGitHub(check_rc=1)
        assert af.emit_run_stats(gh, stats, "issue-62", "o/r", now=TS1) is True
        assert gh.n_comment_creates() == 1
        body = gh.summary_body()
        assert "check-run unavailable" in body
        assert "| model | `some/model` (goose) |" in body
        assert SCAN_ROUND_COUNTER.findall(body) == [TS1]
        # Still a true ADR-103 emission — the launcher must not re-post the old shape over it.
        assert stats.get("stats_comment_by_pod") is True

    def test_a_failed_summary_write_leaves_the_flag_unset(self, af):
        """The other direction of the pact: unset means "the pod never got to it", so the
        launcher's fallback runs. Setting it here would lose the stats entirely."""
        stats = dict(self.STATS)
        gh = FakeGitHub(list_raw="<html>502</html>")
        assert af.emit_run_stats(gh, stats, "issue-62", "o/r", now=TS1) is False
        assert "stats_comment_by_pod" not in stats

    def test_two_rounds_leave_one_comment_and_two_markers(self, af):
        """The acceptance, counted: a two-round ride produces ONE machine comment and a round
        counter of 2 — the behaviour fixture `ci-red-rounds-two-channels` pins on the homelab side."""
        gh = FakeGitHub()
        af.emit_run_stats(gh, dict(self.STATS), "issue-62", "o/r", now=TS1)
        af.emit_run_stats(gh, dict(self.STATS), "issue-62", "o/r", now=TS2)
        assert gh.n_comment_creates() == 1
        assert gh.n_comment_patches() == 1
        assert gh.n_check_runs() == 2
        assert SCAN_ROUND_COUNTER.findall(gh.summary_body()) == [TS1, TS2]

    def test_the_slug_falls_back_to_the_pr_url(self, af):
        """REPO_URL is launcher-injected and a coordinator-path ride can arrive without it; the PR
        URL always names the repo."""
        stats = dict(self.STATS)
        gh = FakeGitHub()
        assert af.emit_run_stats(gh, stats, "issue-62", "", now=TS1) is True
        assert any("repos/o/r/check-runs" in a for (c, _s) in gh.calls for a in c)


class TestRunStatsTableRail:
    """The `rail` row in the run-stats table — agent-runtime#81.

    When stats carries a `rail` key, the table must include a `rail` row showing its value.
    When stats has no `rail` key, the table must omit the row entirely.
    """

    STATS = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "clean",
             "model": "some/model", "harness": "goose", "cost_usd": 0.42, "duration_s": 128,
             "ci_passed": True, "pod": "pod-7", "project": "agent-runtime"}

    def test_rail_row_present_when_rail_in_stats(self, af):
        """AGENT_RAIL set → stats has 'rail' → table includes a rail row."""
        stats = dict(self.STATS, rail="subscription-fallback")
        table = af.run_stats_table(stats, "issue-81")
        assert "| rail | `subscription-fallback` |" in table

    def test_rail_row_absent_when_rail_not_in_stats(self, af):
        """AGENT_RAIL absent → stats has no 'rail' → table omits the rail row."""
        stats = dict(self.STATS)
        table = af.run_stats_table(stats, "issue-81")
        assert "| rail |" not in table

    def test_rail_row_in_check_run_when_present(self, af):
        """The rail row rides the check-run output when present."""
        stats = dict(self.STATS, rail="openrouter")
        gh = FakeGitHub()
        af.emit_run_stats(gh, stats, "issue-81", "o/r", now=TS1)
        summary = gh.check_runs[0]["output"]["summary"]
        assert "| rail | `openrouter` |" in summary

    def test_rail_row_absent_from_check_run_when_missing(self, af):
        """No rail row in the check-run output when rail is absent from stats."""
        stats = dict(self.STATS)
        gh = FakeGitHub()
        af.emit_run_stats(gh, stats, "issue-81", "o/r", now=TS1)
        summary = gh.check_runs[0]["output"]["summary"]
        assert "| rail |" not in summary


class TestBookkeepingUsesTheChannels:
    """The wiring: `bookkeeping()`'s arm leg emits the pair instead of `gh pr comment`.

    The bug named in the issue is exactly here — finalize posted the OLD table and set
    `stats_comment_by_pod`, which suppressed the merged launcher's new-shape leg, so a real ride
    today yields the old residue and NO check-run at all.
    """

    def _run(self, af, monkeypatch, log_path, stats, world=None):
        world = world or FakeGitHub()
        calls = []

        def _fake_run(argv, **kw):
            calls.append(tuple(argv))
            assert argv[0] == "gh"
            return world(*argv[1:], stdin=kw.get("input"))

        monkeypatch.setattr(af.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(af.subprocess, "run", _fake_run)
        monkeypatch.setattr(af, "_fresh_gh_env", lambda: {})
        monkeypatch.setenv("AGENT_TASK", "issue-62")
        monkeypatch.setenv("REPO_URL", "https://github.com/o/r")
        monkeypatch.setenv("MODEL", "some/model")
        monkeypatch.setenv("AGENT_ROUND", "2")
        af.bookkeeping(stats, log_path)
        return calls, world

    def test_the_arm_leg_emits_the_pair(self, af, monkeypatch, logfile):
        stats = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "clean", "pod": "p"}
        calls, world = self._run(af, monkeypatch, logfile("all fine\n"), stats)
        assert [c for c in calls if c[1:3] == ("pr", "merge")]
        assert not [c for c in calls if c[1:3] == ("pr", "comment")]
        assert world.n_check_runs() == 1
        assert world.n_comment_creates() == 1
        assert stats.get("stats_comment_by_pod") is True

    def test_a_died_round_emits_neither(self, af, monkeypatch, logfile):
        """#49 unchanged: a died round holding an earlier round's PR gets no stats channel at all —
        and now that includes the check-run."""
        stats = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "harness-death",
                 "error_class": "goose-32602-truncation", "pod": "p"}
        calls, world = self._run(af, monkeypatch, logfile("boom\n"), stats)
        assert world.n_check_runs() == 0
        assert world.n_comment_creates() == 0
        assert "stats_comment_by_pod" not in stats

    def test_the_strike_stays_an_ordinary_comment(self, af, monkeypatch, logfile):
        """Out of scope by decision (point 7): `AGENT_STRIKE:` is a load-bearing store the
        coordinator greps, not residue. It moves in a later replay-first issue, not this one."""
        stats = {"pr_url": "https://github.com/o/r/pull/9", "exit_status": "harness-death",
                 "error_class": "goose-panic", "pod": "p"}
        calls, _world = self._run(af, monkeypatch, logfile("boom\n"), stats)
        posted = [c for c in calls if c[1:3] == ("issue", "comment")]
        assert len(posted) == 1
        assert posted[0][-1].startswith("AGENT_STRIKE: ")
