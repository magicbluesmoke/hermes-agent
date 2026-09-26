"""Tests for the git conflict-marker guard in kanban-auto-commit.

Covers:
* Pure detection (`_conflict_marker_findings`): false-positive and
  false-negative cases for the marker patterns.
* End-to-end: a staged file with real conflict markers blocks the
  auto-commit (HEAD unchanged, ERROR logged, naming file + task); a clean
  staged file commits successfully.

Regression for t_d53f8ef7 (conflict markers leaked via the auto-commit
pipeline).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Import the plugin module directly (matches the pattern in
# test_path_resolution.py so the test is runnable without installing the
# plugin into a live agent).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import __init__ as pkg  # noqa: E402
import importlib  # noqa: E402

importlib.reload(pkg)


# --- pure detection -------------------------------------------------------


def test_detects_full_conflict_block():
    content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    f = pkg._conflict_marker_findings(content)
    assert "1:<<<<<<<" in f
    assert "3:=======" in f
    assert "5:>>>>>>>" in f
    assert len(f) == 3


def test_detects_start_marker_without_end():
    content = "head\n<<<<<<< HEAD\nno end here\n"
    f = pkg._conflict_marker_findings(content)
    assert "2:<<<<<<<" in f
    assert len(f) == 1


def test_detects_end_marker_alone():
    content = "stuff\n>>>>>>> branch\n"
    f = pkg._conflict_marker_findings(content)
    assert "2:>>>>>>>" in f
    assert len(f) == 1


def test_bare_equals_separator_is_not_flagged():
    # Legit Markdown/RST section underline, table separator, long divider.
    content = "Title\n=======\n\n| a | b |\n=======|======|\n\n========\n"
    assert pkg._conflict_marker_findings(content) == []


def test_six_char_prefix_is_not_a_marker():
    # Five or six < are not git markers; the 7-char prefix is required.
    assert pkg._conflict_marker_findings("<<<<<<\n") == []
    assert pkg._conflict_marker_findings("<<<<<\n") == []


def test_marker_without_trailing_space_is_flagged():
    # git always emits `<<<<<<< <label>`; `<<<<<<<` with no label is still a marker.
    f = pkg._conflict_marker_findings("<<<<<<<\n>>>>>>>\n")
    assert any(x == "1:<<<<<<<" for x in f)
    assert any(x == "2:>>>>>>>" for x in f)


def test_marker_not_at_line_start_is_not_flagged():
    # A string literal containing marker chars mid-line is not a real marker.
    content = "msg = '<<<<<<< oops'\nrest = '>>>>>>> done'\n"
    assert pkg._conflict_marker_findings(content) == []


def test_normal_code_is_clean():
    content = "def f():\n    return a == b\n# ===-\n"
    assert pkg._conflict_marker_findings(content) == []


def test_separator_only_with_no_block_is_clean():
    # A lone ======= never appears in a real conflict without the <<<<<<<
    # opener, so it must not raise a false positive.
    assert pkg._conflict_marker_findings("=======\n") == []


# --- end-to-end against a real temp git repo ------------------------------


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test Worker"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()
    return repo, base


def _make_board_db(tmp_path, repo, task_id):
    db = tmp_path / "board.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (id TEXT, workspace_kind TEXT, workspace_path TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks(id, workspace_kind, workspace_path) VALUES(?,?,?)",
        (task_id, "dir", str(repo)),
    )
    conn.commit()
    conn.close()
    return db


def _current_head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()


def _last_commit_subject(repo):
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()


def test_blocks_auto_commit_with_conflict_markers(git_repo, tmp_path, monkeypatch, caplog):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_test_marker")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    conflict = repo / "conflicted.py"
    conflict.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    subprocess.run(["git", "add", "conflicted.py"], cwd=repo, check=True)

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_test_marker", None)

    # No commit was created: HEAD did not move.
    assert _current_head(repo) == base

    # The block was announced loudly (ERROR), naming the file and the task.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "expected an ERROR log when conflict markers are staged"
    blob = " ".join(r.getMessage() for r in errors)
    assert "conflicted.py" in blob
    assert "t_test_marker" in blob
    assert "conflict markers" in blob


def test_clean_staged_file_commits_through_guard(git_repo, tmp_path, monkeypatch, caplog):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_test_clean")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    (repo / "clean.py").write_text("def f():\n    return 1\n# === a comment\n")
    subprocess.run(["git", "add", "clean.py"], cwd=repo, check=True)

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_test_clean", None)

    # A commit WAS created with the expected auto message.
    assert _current_head(repo) != base
    assert _last_commit_subject(repo) == "auto: kanban task t_test_clean completed"

    # No error was logged: the clean file is not a false positive.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors
    assert "clean.py" not in " ".join(r.getMessage() for r in errors)


def test_bare_equals_only_in_staged_file_does_not_block(git_repo, tmp_path, monkeypatch, caplog):
    """A staged file with a legit `=======` divider but no git conflict
    markers must still commit (no false positive)."""
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_test_divider")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))

    (repo / "doc.md").write_text("# Title\n=======\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    subprocess.run(["git", "add", "doc.md"], cwd=repo, check=True)

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_test_divider", None)

    assert _current_head(repo) != base
    assert _last_commit_subject(repo) == "auto: kanban task t_test_divider completed"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors
