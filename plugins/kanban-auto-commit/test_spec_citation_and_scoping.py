"""Tests for the spec-citation and staging-scope fixes in kanban-auto-commit.

Covers the two defects from critique t_4b7e63b0:

* Defect B — auto-commit messages must cite the completing task's
  ``I{NNN}`` spec/registry ID so a ``src/`` auto-commit passes the hard
  spec-citation gate; when no ID is known the commit is blocked by default.
* Defect C — staging must be scoped to the task's own declared files so a
  finishing worker cannot sweep a concurrent worker's untracked files.

Run against the real gate script when it is available (via
``$REALM_FORGE_REPO``) so the pass/fail behaviour matches CI.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import __init__ as pkg  # noqa: E402

import importlib  # noqa: E402

importlib.reload(pkg)


# --- pure helpers ---------------------------------------------------------


def test_extract_spec_id_finds_first_token():
    assert pkg._extract_spec_id("fix stuff I140 now") == "I140"
    assert pkg._extract_spec_id("no id here") is None
    assert pkg._extract_spec_id(None, "", "later I479") == "I479"
    # Word-boundary: AI140 / I1400 must not match.
    assert pkg._extract_spec_id("AI140") is None
    assert pkg._extract_spec_id("I1400") is None


def test_normalize_repo_path_rejects_escape():
    repo = "/tmp/repo"
    assert pkg._normalize_repo_path(repo, "src/a.py") == "src/a.py"
    assert pkg._normalize_repo_path(repo, "./src/a.py") == "src/a.py"
    assert pkg._normalize_repo_path(repo, "/tmp/repo/src/a.py") == "src/a.py"
    assert pkg._normalize_repo_path(repo, "../evil.py") is None
    assert pkg._normalize_repo_path(repo, "") is None
    assert pkg._normalize_repo_path(repo, ".") is None


# --- end-to-end against a real temp git repo ------------------------------


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Worker"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()
    return repo, base


def _make_board_db(tmp_path, repo, task_id, body=None, title="task",
                   run_metadata=None, with_runs=False):
    db = tmp_path / "board.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, "
        "workspace_kind TEXT, workspace_path TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks(id, title, body, workspace_kind, workspace_path) "
        "VALUES(?,?,?,?,?)",
        (task_id, title, body, "dir", str(repo)),
    )
    if with_runs:
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, "
            "status TEXT, summary TEXT, metadata TEXT)"
        )
        conn.execute(
            "INSERT INTO task_runs(id, task_id, status, summary, metadata) "
            "VALUES(?,?,?,?,?)",
            (1, task_id, "completed", None, run_metadata),
        )
    conn.commit()
    conn.close()
    return db


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()


def _subject(repo):
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], capture_output=True, text=True, cwd=repo, check=True
    ).stdout.strip()


def _in_head(repo, path):
    return subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{path}"], cwd=repo, capture_output=True
    ).returncode == 0


def test_scoped_commit_excludes_sibling_untracked(tmp_path, git_repo, monkeypatch):
    """A declared-file task must not sweep a sibling's untracked file."""
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_scoped")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_SPEC_ID", "I140")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")
    (repo / "sibling_guard.py").write_text("sibling work\n")  # untracked, other task

    pkg._auto_commit("t_scoped", None)

    assert _head(repo) != base
    assert _in_head(repo, "src/a.py")
    assert not _in_head(repo, "sibling_guard.py")
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=repo
    ).stdout
    assert "sibling_guard.py" in status  # left for its owner


def test_declared_files_sourced_from_run_metadata(tmp_path, git_repo, monkeypatch):
    repo, base = git_repo
    md = '{"changed_files": ["src/a.py"]}'
    db = _make_board_db(tmp_path, repo, "t_meta", run_metadata=md, with_runs=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("KANBAN_AUTO_COMMIT_FILES", raising=False)
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_SPEC_ID", "I140")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")
    (repo / "other.py").write_text("untracked other\n")

    pkg._auto_commit("t_meta", None, run_id=1)

    assert _in_head(repo, "src/a.py")
    assert not _in_head(repo, "other.py")


def test_spec_id_from_task_body_is_cited(tmp_path, git_repo, monkeypatch):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_body", body="Fix regression I140 in combat")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    pkg._auto_commit("t_body", None)

    assert _subject(repo) == "auto: kanban task t_body completed (I140)"


def test_spec_id_env_override(tmp_path, git_repo, monkeypatch):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_env")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_SPEC_ID", "I479")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    pkg._auto_commit("t_env", None)

    assert _subject(repo) == "auto: kanban task t_env completed (I479)"


def test_src_without_spec_id_blocks_commit(tmp_path, git_repo, monkeypatch, caplog):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_block")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")
    monkeypatch.delenv("KANBAN_AUTO_COMMIT_SPEC_ID", raising=False)
    monkeypatch.delenv("KANBAN_AUTO_COMMIT_SPEC_ID_POLICY", raising=False)

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_block", None)

    assert _head(repo) == base  # no commit created
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "t_block" in " ".join(errors)
    assert "spec ID" in " ".join(errors)


def test_src_without_spec_id_policy_commit_optin(tmp_path, git_repo, monkeypatch, caplog):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_optin")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_SPEC_ID_POLICY", "commit")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_optin", None)

    assert _head(repo) != base
    assert _subject(repo) == "auto: kanban task t_optin completed"
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors


def test_nonsrc_without_spec_id_commits(tmp_path, git_repo, monkeypatch, caplog):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_docs")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "docs/note.md")

    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("hi\n")

    with caplog.at_level(logging.ERROR):
        pkg._auto_commit("t_docs", None)

    assert _head(repo) != base
    assert _subject(repo) == "auto: kanban task t_docs completed"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_no_declaration_fallback_does_not_sweep_untracked(tmp_path, git_repo, monkeypatch):
    """Without a declared list, only tracked modifications are committed."""
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_fallback")  # no run metadata
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("KANBAN_AUTO_COMMIT_FILES", raising=False)

    (repo / "README.md").write_text("init\nchanged\n")  # tracked modification
    (repo / "sibling_guard.py").write_text("untracked sibling\n")

    pkg._auto_commit("t_fallback", None)

    assert _head(repo) != base
    assert not _in_head(repo, "sibling_guard.py")
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=repo
    ).stdout
    assert "sibling_guard.py" in status


# --- real gate integration ------------------------------------------------

_GATE = Path(os.environ.get("REALM_FORGE_REPO", "/home/michael/src/realm-forge-game")) / (
    "project-quality-gates/scripts/check_spec_citation.py"
)


def _run_gate(repo, rev_range):
    env = dict(os.environ)
    env["REALM_FORGE_GATE_SPEC_WARN_ONLY"] = "0"
    return subprocess.run(
        [sys.executable, str(_GATE), "--ci", "--range", rev_range],
        capture_output=True, text=True, cwd=repo, env=env,
    )


@pytest.mark.skipif(not _GATE.is_file(), reason="gate script not present")
def test_scoped_src_autocommit_passes_real_gate(tmp_path, git_repo, monkeypatch):
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_gate", body="Implement I140")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    pkg._auto_commit("t_gate", None)

    proc = _run_gate(repo, f"{base}..HEAD")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "I140" in _subject(repo)


@pytest.mark.skipif(not _GATE.is_file(), reason="gate script not present")
def test_autocommit_without_spec_id_would_fail_gate(tmp_path, git_repo, monkeypatch):
    """Policy=commit produces a commit the real gate rejects (documents why
    the default is to block)."""
    repo, base = git_repo
    db = _make_board_db(tmp_path, repo, "t_gate_red")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_FILES", "src/a.py")
    monkeypatch.setenv("KANBAN_AUTO_COMMIT_SPEC_ID_POLICY", "commit")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("new a\n")

    pkg._auto_commit("t_gate_red", None)

    proc = _run_gate(repo, f"{base}..HEAD")
    assert proc.returncode == 1, proc.stdout + proc.stderr
