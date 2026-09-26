"""Live acceptance tests for kanban-review-gate."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys
import os
import time

plugin_dir = (
    Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    / "plugins"
    / "kanban-review-gate"
)
sys.path.insert(0, str(plugin_dir))
import __init__ as rg
from __init__ import _qualify, _qualification_reason, _request, _on_complete


def _setup_db(tmp_path: Path) -> Path:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
            priority INTEGER, created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER,
            workspace_kind TEXT, workspace_path TEXT, branch_name TEXT, claim_lock TEXT, claim_expires INTEGER,
            tenant TEXT, result TEXT, idempotency_key TEXT, consecutive_failures INTEGER, worker_pid INTEGER,
            last_failure_error TEXT, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER,
            workflow_template_id TEXT, current_step_key TEXT, skills TEXT, model_override TEXT, max_retries INTEGER,
            session_id TEXT, project_id TEXT, block_kind TEXT, block_recurrences INTEGER, provider_override TEXT,
            reasoning_effort TEXT, tags TEXT
        );
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, step_key TEXT, status TEXT, claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
        """
    )
    conn.close()
    return db


def _insert_task(conn: sqlite3.Connection, **overrides):
    row = {
        "id": "task-1", "title": "Task", "body": "", "assignee": "kanban-worker", "status": "done",
        "priority": 2, "created_by": "main-session", "created_at": 0, "started_at": 0, "completed_at": 0,
        "workspace_kind": None, "workspace_path": None, "branch_name": None, "claim_lock": None, "claim_expires": None,
        "tenant": None, "result": None, "idempotency_key": None, "consecutive_failures": 0, "worker_pid": None,
        "last_failure_error": None, "max_runtime_seconds": None, "last_heartbeat_at": None, "current_run_id": None,
        "workflow_template_id": None, "current_step_key": None, "skills": None, "model_override": None,
        "max_retries": 0, "session_id": None, "project_id": None, "block_kind": None, "block_recurrences": 0,
        "provider_override": None, "reasoning_effort": None, "tags": None,
    }
    row.update({k: v for k, v in overrides.items() if v is not None})
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,priority,created_by,created_at,started_at,completed_at,workspace_kind,workspace_path,branch_name,claim_lock,claim_expires,tenant,result,idempotency_key,consecutive_failures,worker_pid,last_failure_error,max_runtime_seconds,last_heartbeat_at,current_run_id,workflow_template_id,current_step_key,skills,model_override,max_retries,session_id,project_id,block_kind,block_recurrences,provider_override,reasoning_effort,tags) VALUES (:id,:title,:body,:assignee,:status,:priority,:created_by,:created_at,:started_at,:completed_at,:workspace_kind,:workspace_path,:branch_name,:claim_lock,:claim_expires,:tenant,:result,:idempotency_key,:consecutive_failures,:worker_pid,:last_failure_error,:max_runtime_seconds,:last_heartbeat_at,:current_run_id,:workflow_template_id,:current_step_key,:skills,:model_override,:max_retries,:session_id,:project_id,:block_kind,:block_recurrences,:provider_override,:reasoning_effort,:tags)",
        row,
    )
    conn.commit()


def _event(conn, task_id, kind, payload=None):
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, 0)", (task_id, kind, json.dumps(payload or {})))
    conn.commit()


@pytest.fixture(autouse=True)
def _override_paths(monkeypatch, tmp_path):
    db = _setup_db(tmp_path)
    monkeypatch.setattr(rg, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(rg, "KANBAN_HOME", tmp_path / "kanban")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))


def test_required_review_dispatches():
    conn = sqlite3.connect(str(rg._board_db_path(None)))
    _insert_task(conn, body="review: required")
    conn.close()
    captured = {}
    def fake_request(task_id, board, summary, metadata, reviewer):
        captured.update({'task_id': task_id, 'summary': summary, 'reviewer': reviewer, 'metadata': metadata})
        return True
    with patch.object(rg, '_request', side_effect=fake_request):
        _on_complete('task-1', None, None, None)
    assert captured['task_id'] == 'task-1'
    assert 'required review' in captured['summary']
    assert captured['metadata']['plugin'] == 'kanban-review-gate'


def test_docs_tag_skips():
    conn = sqlite3.connect(str(rg._board_db_path(None)))
    _insert_task(conn, tags='docs')
    conn.close()
    captured = {}
    def fake_request(*a, **kw):
        captured['called'] = True
        return True
    with patch.object(rg, '_request', side_effect=fake_request):
        _on_complete('task-1', None, None, None)
    assert not captured.get('called')


def test_changed_lines_threshold_triggers(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'f').write_text('a\n')
    conn = sqlite3.connect(str(rg._board_db_path(None)))
    _insert_task(conn, workspace_kind='dir', workspace_path=str(repo))
    conn.close()
    captured = {}
    def fake_request(*a, **kw):
        captured['called'] = True
        return True
    with patch.object(rg, '_request', side_effect=fake_request):
        with patch.object(rg, '_changed_lines', return_value=(21, 1)):
            _on_complete('task-1', None, str(repo), 'dir')
    assert captured.get('called') is True


def test_dispatcher_escalates_after_max_iterations():
    db_path = rg._board_db_path(None)
    conn = sqlite3.connect(str(db_path))
    _insert_task(conn, status='review', claim_lock=None, current_run_id=None)
    _event(conn, 'task-1', 'review_reopened')
    _event(conn, 'task-1', 'review_reopened')
    _event(conn, 'task-1', 'review_reopened')
    conn.close()
    from scripts import review_dispatcher as rd
    result = rd.dispatch(None, max_iterations=3)
    actions = [r['action'] for r in result['results']]
    assert 'escalated' in actions


def test_dispatcher_approves_on_subagent_pass():
    db_path = rg._board_db_path(None)
    conn = sqlite3.connect(str(db_path))
    _insert_task(conn, status='review', claim_lock=None, current_run_id=None)
    conn.close()
    from scripts import review_dispatcher as rd

    def fake_delegate_review(task, role, model):
        return {"verdict": "pass", "gaps": [], "evidence": f"{role} ok"}

    with patch.object(rd, '_delegate_review', side_effect=fake_delegate_review):
        result = rd.dispatch(None, max_iterations=3)
    assert result['results'][0]['verdict'] == 'pass'
    assert result['results'][0]['action'] in {'completed', 'approve_failed', 'changes_failed', 'skipped'}


def test_dispatcher_requests_changes_on_subagent_fail():
    db_path = rg._board_db_path(None)
    conn = sqlite3.connect(str(db_path))
    _insert_task(conn, status='review', claim_lock=None, current_run_id=None)
    conn.close()
    from scripts import review_dispatcher as rd

    def fake_delegate_review(task, role, model):
        return {"verdict": "fail", "gaps": [f"{role} gap"], "evidence": f"{role} fail"}

    with patch.object(rd, '_delegate_review', side_effect=fake_delegate_review):
        result = rd.dispatch(None, max_iterations=3)
    r = result['results'][0]
    assert r['verdict'] == 'fail'
    assert any('gap' in g for g in r.get('gaps', []))
    assert r['action'] in {'request_changes', 'changes_failed', 'approve_failed', 'skipped'}


def test_single_owner_review_lane_skips_double_review():
    task = {
        "id": "task-1", "status": "in_review", "title": "Task", "body": "",
        "current_run_id": None, "claim_lock": "review-lane-owner", "claim_expires": None,
        "workspace_kind": None, "workspace_path": None, "tags": "",
    }
    captured = {}
    def fake_request(*a, **kw):
        captured['called'] = True
        return True
    with patch.object(rg, '_qualify', return_value=task):
        with patch.object(rg, '_request', side_effect=fake_request):
            _on_complete('task-1', None, None, None)
    assert not captured.get('called')


def test_claim_expiry_falls_back_to_comment_and_request():
    task = {
        "id": "task-1", "status": "in_review", "title": "Task", "body": "review: required",
        "current_run_id": None, "claim_lock": "expired-owner", "claim_expires": int(time.time()) - 1,
        "workspace_kind": None, "workspace_path": None, "tags": "",
    }
    captured = {}
    comments = []
    def fake_request(task_id, board, summary, metadata, reviewer):
        captured['summary'] = summary
        return True
    def fake_comment(*a, **kw):
        comments.append((a, kw))
        return None
    with patch.object(rg, '_qualify', return_value=task):
        with patch.object(rg, '_request', side_effect=fake_request):
            with patch.object(rg, '_comment', side_effect=fake_comment):
                _on_complete('task-1', None, None, None)
    assert 'required review' in captured.get('summary', '')
    assert comments
