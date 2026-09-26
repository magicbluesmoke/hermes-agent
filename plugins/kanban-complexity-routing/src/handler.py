"""Dispatcher/plugin-side complexity router for kanban and delegation.

Unified config source:
- kanban: hooks ``kanban_task_claimed``, writes ``task.model_override``
- delegation: returns override dict from ``delegate_task_routing``

Self-contained since 2026-08-03: routing logic lives in ``src/routing.py``
inside this plugin (survives ``hermes update`` which wipes in-tree local
edits to ``hermes_cli/complexity_routing.py``).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

try:  # preferred: local self-contained module
    from .routing import (  # type: ignore
        get_complexity_routing,
        laya_route,
        select_target,
    )
except Exception:  # pragma: no cover - fallback for flat import
    from routing import (  # type: ignore
        get_complexity_routing,
        laya_route,
        select_target,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kanban connectivity
# ---------------------------------------------------------------------------

def _load_known_assignees() -> set:
    """Return the set of valid assignee/profile names.

    Sources:
    1. kanban.valid_assignees from config.yaml (explicit allowlist; if
       present, this is the ONLY source — operator opt-in is required).
    2. Fallback: kanban.default_assignee + 'kanban-worker' + 'default'.
    """
    try:
        cfg = _load_config()
        kb_cfg = cfg.get("kanban", {})
        allowlist = kb_cfg.get("valid_assignees")
        if allowlist is not None:
            if isinstance(allowlist, list):
                names = {str(x).strip() for x in allowlist if str(x).strip()}
            else:
                raw = str(allowlist).strip()
                try:
                    import json
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        names = {str(x).strip() for x in parsed if str(x).strip()}
                    else:
                        names = set()
                except Exception:
                    names = set()
            if names:
                return names
    except Exception:
        pass
    known = {"kanban-worker", "default"}
    try:
        default_assignee = (
            (cfg.get("kanban", {}) if isinstance(cfg, dict) else {}).get("default_assignee") or ""
        ).strip()
        if default_assignee:
            known.add(default_assignee)
    except Exception:
        pass
    return known


def _rewrite_assignee(task_id: str, bad_assignee: str) -> None:
    """Rewrite an invalid assignee to default_assignee with a warning."""
    try:
        from hermes_cli.kanban_db import kanban_db_path
        from hermes_cli.kanban_db_connect import connect as _kb_connect
        db_path = kanban_db_path()
        conn = _kb_connect(db_path)
        try:
            with conn:
                cur = conn.execute(
                    "SELECT default_assignee FROM kanban_boards WHERE slug = 'default'"
                )
                row = cur.fetchone()
                fallback = (row[0] if row else "kanban-worker") or "kanban-worker"
        except Exception:
            fallback = "kanban-worker"
        finally:
            conn.close()
    except Exception:
        fallback = "kanban-worker"

    try:
        conn = _connect()
        if conn is None:
            return
        with conn:
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                (fallback, task_id),
            )
        logger.warning(
            "kanban-complexity-routing rewrote invalid assignee %r -> %r for task=%s",
            bad_assignee,
            fallback,
            task_id,
        )
    except Exception as exc:
        logger.debug("kanban-complexity-routing assignee rewrite failed: %s", exc)


def _env_db_path() -> Optional[str]:
    path = os.environ.get("HERMES_KANBAN_DB")
    return path if path else None


def _env_board() -> Optional[str]:
    board = os.environ.get("HERMES_KANBAN_BOARD")
    return board if board else None


def _connect():
    try:
        from hermes_cli.kanban_db_connect import connect as _kb_connect  # type: ignore
        db_path = _env_db_path()
        board = _env_board()
        if db_path:
            return _kb_connect(Path(db_path))
        return _kb_connect(board=board)
    except Exception as exc:
        logger.debug("_connect failed: %s", exc)
        raise


def _load_task(conn, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        cur = conn.execute(
            "SELECT id, title, body, model_override FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "body": row["body"],
            "model_override": row["model_override"],
        }
    except Exception:
        return None


def _write_model_override(conn, task_id: str, value: str) -> bool:
    try:
        with conn:
            conn.execute(
                "UPDATE tasks SET model_override = ? WHERE id = ?",
                (value, task_id),
            )
        return True
    except Exception:
        return False


def _write_laya_meta(conn, kwargs: Dict[str, Any], meta: Dict[str, Any]) -> None:
    """Persist the Laya typed verdict on task_runs.metadata (observability).

    JSON-merge under ``$.laya_route`` so other plugins' keys survive. Swallows
    all failures — the claim hook must never break the spawn path. The WAL is
    checkpointed so the dispatcher (separate process) sees the write.
    """
    import json as _json

    run_id = kwargs.get("run_id")
    if not run_id or not isinstance(meta, dict):
        return
    try:
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (str(run_id),)
        ).fetchone()
        md = {}
        if row and row[0]:
            try:
                md = _json.loads(row[0])
            except Exception:
                md = {}
        md["laya_route"] = {k: v for k, v in meta.items()}
        with conn:
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (_json.dumps(md), str(run_id)),
            )
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
    except Exception as exc:
        logger.debug("kanban-complexity-routing laya meta write failed: %s", exc)


# ---------------------------------------------------------------------------
# Complexity routing config/resolver
# ---------------------------------------------------------------------------

def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config  # type: ignore
        return load_config()
    except Exception:
        return {}


def _routing_block() -> Dict[str, Any]:
    try:
        cfg = _load_config()
        return get_complexity_routing(cfg)
    except Exception:
        return {"enabled": False}


def _resolve_routing(block: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    try:
        return select_target(goal, config=block)
    except Exception as exc:
        logger.debug("kanban-complexity-routing select_target failed: %s", exc)
        return None


def _encode_target(target: Dict[str, Any]) -> Optional[str]:
    strategy = str(target.get("strategy") or "model").strip().lower()
    provider = target.get("provider")
    model = target.get("model")
    preset = target.get("preset")

    if strategy == "model":
        if not model:
            return None
        value = str(model)
        if provider:
            value = f"{provider}/{value}"
        return value
    if strategy == "moa":
        if not preset:
            return None
        value = f"moa:{preset}"
        if provider and model:
            value = f"{provider}/{model}:{preset}"
        elif provider:
            value = f"{provider}:{preset}"
        return value
    return None


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def handle_kanban_task_claimed(**kwargs: Any) -> None:
    task_id = kwargs.get("task_id")
    if not task_id:
        return

    # ------------------------------------------------------------------
    # Assignee validation — prevent invalid profile assignments from
    # reaching the worker pool.
    # Known good: kanban-worker, plus any explicitly configured profile
    # that has a valid model config. Anything else is rewritten to
    # default_assignee with a warning.
    # ------------------------------------------------------------------
    _known_assignees = _load_known_assignees()
    assignee = kwargs.get("assignee") or ""
    if assignee and assignee not in _known_assignees:
        _rewrite_assignee(task_id, assignee)

    block = _routing_block()
    if not isinstance(block, dict) or not block.get("enabled"):
        return

    conn = None
    try:
        conn = _connect()
        if conn is None:
            return
        task = _load_task(conn, str(task_id))
        if not task or task.get("model_override"):
            return

        goal = " ".join(str(task.get(k) or "") for k in ("title", "body")).strip()
        if not goal:
            return

        classifier = str((block or {}).get("classifier") or "laya").lower()
        if classifier in ("laya", "laya-route", "laya-choice"):
            # Typed Laya router: lane choice + decompose-rule noul, fail-to-REVIEW.
            try:
                res = laya_route(goal, config=block)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("kanban-complexity-routing laya_route failed: %s", exc)
                res = None
            if not isinstance(res, dict):
                return
            meta = res.get("meta") or {}
            _write_laya_meta(conn, kwargs, meta)
            target = res.get("target")
            if target is None:
                # Confident local: keep default (local) lane; verdict is logged.
                return
        else:
            target = _resolve_routing(block, goal)
            if not target:
                return

        value = _encode_target(target)
        if not value:
            return

        ok = _write_model_override(conn, str(task_id), value)
        if ok:
            logger.info(
                "kanban-complexity-routing set model_override=%r for task=%s assignee=%s run_id=%s",
                value,
                task_id,
                kwargs.get("assignee"),
                kwargs.get("run_id"),
            )
    except Exception as exc:
        logger.debug("kanban-complexity-routing hook failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def handle_delegate_task_routing(**kwargs: Any) -> Dict[str, Any]:
    goal = kwargs.get("goal") or ""
    try:
        block = _routing_block()
        if not isinstance(block, dict) or not block.get("enabled"):
            return {}
        target = _resolve_routing(block, goal)
        if not target:
            return {}
        return {
            "model": target.get("model"),
            "provider": target.get("provider"),
            "preset": target.get("preset"),
            "scope": target.get("scope"),
        }
    except Exception as exc:
        logger.debug("kanban-complexity-routing delegate_task_routing failed: %s", exc)
        return {}
