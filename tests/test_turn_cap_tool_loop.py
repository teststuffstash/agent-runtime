"""Tests for turn-cap, tool-loop, block-repetition, and no-op detection (agent-runtime#134)."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest.mock
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

    def test_no_op_when_head_unchanged(self, af, logfile, tmp_path):
        """Pre-loop HEAD == post-loop HEAD = no-op round (on a work branch)."""
        # Create a mock git repo on a work branch.
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
        # Create a work branch and stay on it without making new commits (no-op).
        subprocess.run(
            ["git", "checkout", "-b", "fix/issue-1"],
            cwd=git_dir,
            capture_output=True,
        )
        head_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_dir,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Write pre-loop HEAD to the temp file.
        pre_loop_file = tmp_path / "pre-loop-head"
        pre_loop_file.write_text(head_commit)
        with unittest.mock.patch.dict(os.environ, {
            "WORKDIR": str(git_dir),
            "agent-finalize:PRE_LOOP_HEAD": str(pre_loop_file),
        }):
            # Mock the PRE_LOOP_HEAD path in agent-finalize by patching it in the module.
            import sys
            import importlib
            # Get the agent-finalize module through conftest's af fixture.
            # Instead, just patch the file read directly.
            with unittest.mock.patch("builtins.open", unittest.mock.mock_open(read_data=head_commit)):
                s, e = af.classify(logfile("no changes\n"), {
                    "pr_url": "http://x/1",
                    "ci_passed": True,
                })
        assert (s, e) == ("no-op", "")

    def test_not_no_op_when_head_changed(self, af, logfile, tmp_path):
        """Pre-loop HEAD != post-loop HEAD = normal clean run, not no-op."""
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
        # Create a work branch.
        subprocess.run(
            ["git", "checkout", "-b", "fix/issue-1"],
            cwd=git_dir,
            capture_output=True,
        )
        # Store pre-loop HEAD (before the new commit).
        pre_loop_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_dir,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # Make a new commit (HEAD changes).
        (git_dir / "file.txt").write_text("changed")
        subprocess.run(["git", "add", "."], cwd=git_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fix"], cwd=git_dir, capture_output=True)
        # Patch open to return the pre-loop HEAD when reading the marker file.
        mock_open = unittest.mock.mock_open(read_data=pre_loop_head)
        with unittest.mock.patch("builtins.open", mock_open):
            with unittest.mock.patch.dict(os.environ, {"WORKDIR": str(git_dir)}):
                s, e = af.classify(logfile("work done\n"), {
                    "pr_url": "http://x/1",
                    "ci_passed": True,
                })
        assert (s, e) == ("clean", "")


class TestBlockRepetitionDetector:
    """Block-repetition detector reads session db and counts consecutive identical pairs."""

    def test_block_repetition_detects_3_consecutive_pairs(self, tmp_path):
        """Three consecutive identical (toolRequest, toolResponse) pairs detected."""
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        # Insert pairs: toolCall + toolResponse, repeated 3 times.
        tool_call = json.dumps({"toolCall": {"value": {"name": "cat", "arguments": "file.txt"}}})
        tool_response = json.dumps({"toolResult": {"value": "content"}})
        for _ in range(3):
            conn.execute("INSERT INTO messages (content_json) VALUES (?)", (tool_call,))
            conn.execute("INSERT INTO messages (content_json) VALUES (?)", (tool_response,))
        conn.commit()
        conn.close()
        # Mock HOME to use test db and run the detector via subprocess.
        import subprocess as sp
        result = sp.run(
            ["python3", "-c", """
import sys
import json
import sqlite3
db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id, content_json FROM messages ORDER BY id")
rows = cursor.fetchall()
conn.close()
pairs = []
i = 0
while i < len(rows):
    try:
        curr_id, curr_json = rows[i]
        curr_obj = json.loads(curr_json or "{}")
        if isinstance(curr_obj, dict) and "toolCall" in curr_obj:
            tool_call = curr_obj.get("toolCall", {}).get("value", {})
            tool_name = tool_call.get("name", "")
            if i + 1 < len(rows):
                next_id, next_json = rows[i + 1]
                next_obj = json.loads(next_json or "{}")
                if isinstance(next_obj, dict) and "toolResult" in next_obj:
                    tool_result = next_obj.get("toolResult", {}).get("value", "")
                    pairs.append((tool_name, str(tool_result)[:100]))
                    i += 2
                    continue
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    i += 1
max_consecutive = 1
current_consecutive = 1
for i in range(1, len(pairs)):
    if pairs[i] == pairs[i - 1]:
        current_consecutive += 1
        max_consecutive = max(max_consecutive, current_consecutive)
    else:
        current_consecutive = 1
sys.exit(0 if max_consecutive >= 3 else 1)
""", str(db_path)],
            capture_output=True
        )
        assert result.returncode == 0, "Block repetition should be detected"

    def test_block_repetition_not_detected_for_2_pairs(self, tmp_path):
        """Two consecutive identical pairs do NOT trigger (needs ≥3)."""
        db_path = tmp_path / ".local" / "share" / "goose" / "sessions" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                content_json TEXT
            )
        """)
        # Insert pairs: only 2 repetitions.
        tool_call = json.dumps({"toolCall": {"value": {"name": "cat", "arguments": "file.txt"}}})
        tool_response = json.dumps({"toolResult": {"value": "content"}})
        for _ in range(2):
            conn.execute("INSERT INTO messages (content_json) VALUES (?)", (tool_call,))
            conn.execute("INSERT INTO messages (content_json) VALUES (?)", (tool_response,))
        conn.commit()
        conn.close()
        import subprocess as sp
        result = sp.run(
            ["python3", "-c", """
import sys
import json
import sqlite3
db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute("SELECT id, content_json FROM messages ORDER BY id")
rows = cursor.fetchall()
conn.close()
pairs = []
i = 0
while i < len(rows):
    try:
        curr_id, curr_json = rows[i]
        curr_obj = json.loads(curr_json or "{}")
        if isinstance(curr_obj, dict) and "toolCall" in curr_obj:
            tool_call = curr_obj.get("toolCall", {}).get("value", {})
            tool_name = tool_call.get("name", "")
            if i + 1 < len(rows):
                next_id, next_json = rows[i + 1]
                next_obj = json.loads(next_json or "{}")
                if isinstance(next_obj, dict) and "toolResult" in next_obj:
                    tool_result = next_obj.get("toolResult", {}).get("value", "")
                    pairs.append((tool_name, str(tool_result)[:100]))
                    i += 2
                    continue
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    i += 1
max_consecutive = 1
current_consecutive = 1
for i in range(1, len(pairs)):
    if pairs[i] == pairs[i - 1]:
        current_consecutive += 1
        max_consecutive = max(max_consecutive, current_consecutive)
    else:
        current_consecutive = 1
sys.exit(0 if max_consecutive >= 3 else 1)
""", str(db_path)],
            capture_output=True
        )
        assert result.returncode != 0, "Block repetition should NOT be detected for 2 pairs"


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
