"""Tests for turn-cap, tool-loop, block-repetition, and no-op detection (agent-runtime#134)."""
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest


class TestTurnCapDetection:
    """Detect when goose reaches the turn cap and report turn-cap class."""

    def test_turn_cap_with_tool_progress_is_turn_cap(self, af, logfile, tmp_path):
        """Goose cap reached + tool results made = turn-cap class."""
        log = "I've reached the maximum number of actions…\n"
        # Create a mock session db with tool results.
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        # Insert a toolResponse (tool result).
        conn.execute(
            "INSERT INTO messages (content_json) VALUES (?)",
            (json.dumps({"toolResult": {"value": "output"}}),),
        )
        conn.commit()
        conn.close()
        # Mock HOME to use test db.
        with unittest.mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            s, e = af.classify(logfile(log), {"harness": "goose"})
        assert (s, e) == ("harness-death", "turn-cap")

    def test_turn_cap_without_tool_progress_is_tool_loop(self, af, logfile, tmp_path):
        """Goose cap reached + no tool results = tool-loop class."""
        log = "I've reached the maximum number of actions…\n"
        # Create an empty mock session db (no tool results).
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        # No tool results inserted.
        conn.commit()
        conn.close()
        # Mock HOME to use test db.
        with unittest.mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            s, e = af.classify(logfile(log), {"harness": "goose"})
        assert (s, e) == ("harness-death", "tool-loop")

    def test_turn_cap_only_for_goose(self, af, logfile):
        """Claude harness cannot die of goose turn-cap signature."""
        log = "I've reached the maximum number of actions…\n"
        s, e = af.classify(logfile(log), {"harness": "claude"})
        assert (s, e) is None or (s, e) != ("harness-death", "turn-cap")

    def test_turn_cap_signature_in_content_is_not_a_death(self, af, logfile):
        """The turn-cap phrase in user content (docs, issue body) is not a death."""
        log = (
            "Reading the issue: 'I've reached the maximum number of actions…'\n"
            "Now implementing the fix.\n"
        )
        s, e = af.classify(logfile(log), {"harness": "goose", "pr_url": "http://x/1"})
        assert (s, e) == ("clean", "")


class TestToolLoopDetection:
    """tool-loop = cap death with zero tool-result progress."""

    def test_tool_loop_from_session_db_zero_progress(self, af, logfile, tmp_path):
        """No toolResponse entries in session db = tool-loop."""
        log = "I've reached the maximum number of actions…\n"
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        # Only toolCall entries, no toolResponse.
        conn.execute(
            "INSERT INTO messages (content_json) VALUES (?)",
            (json.dumps({"toolCall": {"value": {"name": "cat"}}}),),
        )
        conn.commit()
        conn.close()
        with unittest.mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            s, e = af.classify(logfile(log), {"harness": "goose"})
        assert (s, e) == ("harness-death", "tool-loop")

    def test_tool_loop_prefers_other_classes(self, af, logfile, tmp_path):
        """Budget exhaustion outranks tool-loop."""
        log = (
            "I've reached the maximum number of actions…\n"
            "402 payment required\n"
        )
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        conn.commit()
        conn.close()
        with unittest.mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            s, e = af.classify(logfile(log), {"harness": "goose"})
        assert (s, e) == ("budget-403", "budget-exhausted-account")


class TestNoOpDetection:
    """no-op rounds: LLM loop left HEAD unmoved."""

    def test_no_op_when_head_equals_merge_base(self, af, logfile, tmp_path):
        """HEAD == merge-base(HEAD, master) = no-op round."""
        # Create a mock git repo where HEAD hasn't diverged from master.
        git_dir = tmp_path / "repo"
        git_dir.mkdir()
        subprocess.run(
            ["git", "init", "-b", "master"],
            cwd=git_dir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=git_dir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=git_dir,
            capture_output=True,
        )
        # Create an initial commit.
        (git_dir / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, capture_output=True)
        # Stay on master (HEAD == master, so HEAD == merge-base).
        with unittest.mock.patch.dict(os.environ, {"WORKDIR": str(git_dir)}):
            s, e = af.classify(logfile("no changes\n"), {
                "pr_url": "http://x/1",
                "ci_passed": True,
            })
        assert (s, e) == ("no-op", "")

    def test_not_no_op_when_head_diverged(self, af, logfile, tmp_path):
        """HEAD != merge-base = not a no-op (normal clean run)."""
        git_dir = tmp_path / "repo"
        git_dir.mkdir()
        subprocess.run(
            ["git", "init", "-b", "master"],
            cwd=git_dir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=git_dir,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=git_dir,
            capture_output=True,
        )
        (git_dir / "file.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=git_dir, capture_output=True)
        # Create a branch and make a commit.
        subprocess.run(
            ["git", "checkout", "-b", "fix/issue-1"],
            cwd=git_dir,
            capture_output=True,
        )
        (git_dir / "file.txt").write_text("changed")
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fix"], cwd=git_dir, capture_output=True)
        with unittest.mock.patch.dict(os.environ, {"WORKDIR": str(git_dir)}):
            s, e = af.classify(logfile("work done\n"), {
                "pr_url": "http://x/1",
                "ci_passed": True,
            })
        assert (s, e) == ("clean", "")


class TestBlockRepetitionMarker:
    """Watchdog block-repetition marker is classified correctly."""

    def test_block_repetition_marker_classified(self, af, logfile, tmp_path):
        """Marker containing 'block-repetition' is classified as such."""
        marker_path = tmp_path / "marker"
        marker_path.write_text("block-repetition")
        with unittest.mock.patch.dict(
            os.environ,
            {"AGENT_WATCHDOG_MARKER": str(marker_path)}
        ):
            s, e = af.classify(logfile("some log\n"), {})
        assert (s, e) == ("harness-death", "block-repetition")

    def test_block_repetition_with_pr_still_strikes(self, af, logfile, tmp_path):
        """Block-repetition death overrides PR existence."""
        marker_path = tmp_path / "marker"
        marker_path.write_text("block-repetition")
        with unittest.mock.patch.dict(
            os.environ,
            {"AGENT_WATCHDOG_MARKER": str(marker_path)}
        ):
            s, e = af.classify(logfile("some log\n"), {"pr_url": "http://x/1"})
        assert s == "harness-death"
        assert e == "block-repetition"


import unittest.mock
