"""kanban-scratch-dir-guard plugin.

Prevents canonical-repo content/code tasks from completing with a ``scratch``
workspace. At task completion we inspect the task's persisted workspace:
- repo-targeting tasks that are still scratch -> block completion with a clear
  reason requiring dir:C:/src/realm-forge-game.
- non-repo or already-dir tasks -> allow.

Why completion-time guard instead of create-time rewrite:
- create happens via direct SQL insert in the core; plugins only reliably
  observe lifecycle events.
- Completion is the safety boundary we can enforce from a plugin hook.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
except Exception:
    _get_hermes_home = lambda: Path.home() / "AppData" / "Local" / "hermes"

logger = logging.getLogger(__name__)

HERMES_HOME = (
    Path(os.environ.get("HERMES_HOME")) if os.environ.get("HERMES_HOME") else _get_hermes_home()
)
KANBAN_HOME = HERMES_HOME / "kanban"

_CANONICAL_REPO = os.environ.get("REALM_FORGE_REPO", "C:/src/realm-forge-game")
_REPO_SLUGS = (
    "realm-forge",
    "realm_forge",
    "realmforge",
    "realm-forge-game",
    "realm_forge_game",
    "realmforgegame",
)


def _board_db_path(board: Optional[str]) -> Optional[Path]:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    if board and board.strip() and board.strip().lower() != "default":
        return KANBAN_HOME / "boards" / board.strip() / "kanban.db"
    return HERMES_HOME / "kanban.db"


def _is_repo_task(title: Optional[str], body: Optional[str]) -> bool:
    blob = " ".join(filter(None, [title or "", body or ""])).lower()
    return any(slug in blob for slug in _REPO_SLUGS)


def _veto_done_task(task_id: str, board: str, reason: str, kind: str = "needs_input") -> bool:
    """Flip a task that just completed back to ``blocked`` (completion veto).

    The ``kanban_task_completed`` hook fires AFTER the completion txn commits
    (hooks are observer-only; ``block_task`` transitions running/ready only),
    so a done card cannot be re-blocked through the public API. Preferred path:
    the first-class core primitive ``hermes_cli.kanban_db.reopen_task`` (fork
    PR). Until that lands upstream we mirror the core's inline CAS pattern
    (``kanban_swarm._activate_root_inline``): a scoped raw UPDATE guarded by
    ``status = 'done'`` plus a ``blocked`` event in the same txn. The
    ``blocked`` event makes the card sticky to ``recompute_ready`` (no
    auto-promote) and feeds notify subs. Best-effort; returns True on a real
    done->blocked transition.
    """
    try:
        from hermes_cli.kanban_db import reopen_task as _reopen_core
        from hermes_cli.kanban_db_connect import connect as _connect_core
    except Exception:
        _reopen_core = None  # pre-merge core: fall through to the raw flip
    if _reopen_core is not None:
        try:
            conn = _connect_core(board=board or None)
            try:
                return bool(_reopen_core(
                    conn, task_id, reason=reason, landing="blocked",
                    kind=kind, author="scratch-dir-guard",
                ))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return False
    import json as _json
    import time as _time
    try:
        from hermes_cli.kanban_db import VALID_BLOCK_KINDS, get_task
        from hermes_cli.kanban_db_connect import connect
    except Exception:
        return False
    if kind not in VALID_BLOCK_KINDS:
        kind = "needs_input"
    try:
        conn = connect(board=board or None)
        try:
            row = get_task(conn, task_id)
            if row is None or getattr(row, "status", "") != "done":
                return False
            assignee = getattr(row, "assignee", None)
            with conn:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status            = 'blocked',
                           completed_at      = NULL,
                           claim_lock        = NULL,
                           claim_expires     = NULL,
                           worker_pid        = NULL,
                           block_kind        = ?,
                           block_recurrences = 1
                     WHERE id = ?
                       AND status = 'done'
                    """,
                    (kind, task_id),
                )
                if cur.rowcount != 1:
                    return False
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'blocked', ?, ?)",
                    (task_id, _json.dumps(
                        {"reason": reason, "kind": kind, "recurrences": 1,
                         "source_status": "done"}),
                     int(_time.time())),
                )
            try:
                from hermes_cli.lifecycle import invoke_hook
                invoke_hook(
                    "kanban_task_blocked", task_id=task_id, board=board or None,
                    assignee=assignee, run_id=None, reason=reason,
                )
            except Exception:
                pass
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


def _block_task(task_id: str, board: str, reason: str) -> None:
    _veto_done_task(task_id, board, reason)


def _comment_task(task_id: str, board: str, body: str, author: str = "scratch-dir-guard") -> None:
    try:
        from hermes_cli.kanban_db import add_comment
        from hermes_cli.kanban_db_connect import connect
        conn = connect(board=board or None)
        try:
            add_comment(conn, task_id, author, body)
        finally:
            conn.close()
    except Exception:
        pass


def on_kanban_task_completed(
    task_id: str,
    board: str = "",
    assignee: str = "",
    run_id: Optional[str] = None,
    summary: Optional[str] = None,
    workspace_path: Optional[str] = None,
    task_body: Optional[str] = None,
    **kwargs: Any,
) -> None:
    is_repo = False
    board_slug = board if board else ""
    row = None
    try:
        from hermes_cli.kanban_db import get_task
        from hermes_cli.kanban_db_connect import connect
        conn = connect(board=board_slug or None)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
    except Exception:
        row = None

    if row is not None:
        is_repo = _is_repo_task(getattr(row, "title", None), getattr(row, "body", None))
        effective_workspace = getattr(row, "workspace_kind", None)
    else:
        is_repo = _is_repo_task(summary, task_body)
        effective_workspace = None

    if is_repo and effective_workspace == "scratch":
        canonical = _CANONICAL_REPO.replace("\\", "/")
        reason = f"scratch-dir-guard: canonical-repo task must use dir:{canonical}"
        _block_task(task_id, board_slug, reason)
        _comment_task(
            task_id,
            board_slug,
            "Completion blocked by kanban-scratch-dir-guard.\n"
            f"- Requirement: dir:{canonical}\n"
            "- Current: scratch\n"
            f"- Action: recreate the task with --workspace dir:{canonical}",
        )
        logger.info("Blocked scratch repo task %s on board %s", task_id, board_slug)


def register(ctx: Any) -> None:
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
