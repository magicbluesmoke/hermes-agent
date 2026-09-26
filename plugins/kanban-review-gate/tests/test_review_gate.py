"""Tests for kanban-review-gate plugin."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import sys
import os
import subprocess

plugin_dir = (
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    / "plugins"
    / "kanban-review-gate"
)
sys.path.insert(0, str(plugin_dir))
from __init__ import _qualify, _qualification_reason


def _setup_db(path: Path) -> Path:
    db = path / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT,
            priority INTEGER,
            created_by TEXT,
            created_at INTEGER,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT,
            workspace_path TEXT,
            branch_name TEXT,
            claim_lock TEXT,
            claim_expires INTEGER,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            consecutive_failures INTEGER,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            model_override TEXT,
            max_retries INTEGER,
            session_id TEXT,
            project_id TEXT,
            block_kind TEXT,
            block_recurrences INTEGER,
            provider_override TEXT,
            reasoning_effort TEXT,
            tags TEXT,
            source_status TEXT
        );
        CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, step_key TEXT, status TEXT, claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
        """
    )
    conn.close()
    return db


def _insert_task(conn: sqlite3.Connection, **overrides):
    row = {
        "id": "task-1",
        "title": "Task",
        "body": "",
        "assignee": "kanban-worker",
        "status": "done",
        "priority": 2,
        "created_by": "main-session",
        "created_at": 0,
        "started_at": 0,
        "completed_at": 0,
        "workspace_kind": None,
        "workspace_path": None,
        "branch_name": None,
        "claim_lock": None,
        "claim_expires": None,
        "tenant": None,
        "result": None,
        "idempotency_key": None,
        "consecutive_failures": 0,
        "worker_pid": None,
        "last_failure_error": None,
        "max_runtime_seconds": None,
        "last_heartbeat_at": None,
        "current_run_id": None,
        "workflow_template_id": None,
        "current_step_key": None,
        "skills": None,
        "model_override": None,
        "max_retries": 0,
        "session_id": None,
        "project_id": None,
        "block_kind": None,
        "block_recurrences": 0,
        "provider_override": None,
        "reasoning_effort": None,
        "tags": None,
        "source_status": None,
    }
    row.update({k: v for k, v in overrides.items() if v is not None})
    conn.execute(
        """
        INSERT INTO tasks (
            id,title,body,assignee,status,priority,created_by,created_at,started_at,completed_at,
            workspace_kind,workspace_path,branch_name,claim_lock,claim_expires,tenant,result,
            idempotency_key,consecutive_failures,worker_pid,last_failure_error,max_runtime_seconds,
            last_heartbeat_at,current_run_id,workflow_template_id,current_step_key,skills,model_override,
            max_retries,session_id,project_id,block_kind,block_recurrences,provider_override,reasoning_effort,tags,source_status
        ) VALUES (
            :id,:title,:body,:assignee,:status,:priority,:created_by,:created_at,:started_at,:completed_at,
            :workspace_kind,:workspace_path,:branch_name,:claim_lock,:claim_expires,:tenant,:result,
            :idempotency_key,:consecutive_failures,:worker_pid,:last_failure_error,:max_runtime_seconds,
            :last_heartbeat_at,:current_run_id,:workflow_template_id,:current_step_key,:skills,:model_override,
            :max_retries,:session_id,:project_id,:block_kind,:block_recurrences,:provider_override,:reasoning_effort,:tags,:source_status
        )
        """,
        row,
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _override_paths(monkeypatch, tmp_path):
    db = _setup_db(tmp_path)
    monkeypatch.setattr("__init__.HERMES_HOME", tmp_path)
    monkeypatch.setattr("__init__.KANBAN_HOME", tmp_path / "kanban")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))


def test_qualify_missing():
    assert _qualify("missing", None) is None


def test_required_review_qualifies():
    db_path = _qualify.__globals__["_board_db_path"](None)
    conn = sqlite3.connect(str(db_path))
    _insert_task(conn, body="review: required")
    conn.close()
    task = _qualify("task-1", None)
    assert task is not None
    assert task["status"] == "completed"
    reason = _qualification_reason(task, None, 0)
    assert reason == "flagged for required review"


def test_docs_skips_review():
    db_path = _qualify.__globals__["_board_db_path"](None)
    conn = sqlite3.connect(str(db_path))
    _insert_task(conn, body="docs", title="docs task")
    conn.close()
    task = _qualify("task-1", None)
    reason = _qualification_reason(task, None, 0)
    assert reason is None


def test_risk_tiered_always_review_forces_review(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = _qualify.__globals__["_board_db_path"](None)
    _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _insert_task(
        conn,
        workspace_kind="dir",
        workspace_path=str(repo),
        title="engine state fix",
        body="",
        status="done",
        claim_lock=None,
        claim_expires=None,
        current_run_id=None,
    )
    conn.close()
    (repo / "src" / "engine").mkdir(parents=True)
    (repo / "src" / "engine" / "state.py").write_text("# state", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "add", "src/engine/state.py"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (repo / "src" / "engine" / "state.py").write_text("# state changed", encoding="utf-8")
    subprocess.run(["git", "add", "src/engine/state.py"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with patch("__init__._get_config", return_value={"plugins": {"kanban_review_gate": {"always_review": [], "never_review": ["docs"], "min_changed_lines": 20, "risk_tiers": {"always": ["src/engine/state.py", "src/engine/parser/"], "content": ["data/campaigns/", "data/items/"], "never": ["tests/regressions/"]}}}}):
        task = _qualify("task-1", None)
        assert task is not None
        direct_changed = _qualify.__globals__["_changed_files"](str(repo), "dir")
        direct_tier = _qualify.__globals__["_file_risk_tier"](
            "src/engine/state.py",
            {"always": ["src/engine/state.py", "src/engine/parser/"], "content": ["data/campaigns/", "data/items/"], "never": ["tests/regressions/"]},
        )
        print("DBG direct_changed=", direct_changed, "direct_tier=", direct_tier, "task_status=", task["status"], "workspace_path=", task.get("workspace_path"))
        reason = _qualification_reason(task, 1, 0)
    assert reason is not None
    assert reason.startswith("risk-tiered: always-review path touched:")


def test_risk_tiered_content_only_skips_review(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = _qualify.__globals__["_board_db_path"](None)
    _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    _insert_task(
        conn,
        workspace_kind="dir",
        workspace_path=str(repo),
        title="update campaign text",
        body="",
        status="done",
        claim_lock=None,
        claim_expires=None,
        current_run_id=None,
    )
    conn.close()
    campaign = repo / "data" / "campaigns"
    campaign.mkdir(parents=True)
    (campaign / "intro.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "add", "data/campaigns/intro.json"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (campaign / "intro.json").write_text('{"v":1}', encoding="utf-8")
    subprocess.run(["git", "add", "data/campaigns/intro.json"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with patch("__init__._get_config", return_value={"plugins": {"kanban_review_gate": {"always_review": [], "never_review": ["docs"], "min_changed_lines": 20, "risk_tiers": {"always": ["src/engine/state.py"], "content": ["data/campaigns/", "data/items/"], "never": ["tests/regressions/"]}}}}):
        task = _qualify("task-1", None)
    assert task is not None
    reason = _qualification_reason(task, 1, 0)
    assert reason is None
