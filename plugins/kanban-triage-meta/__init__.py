"""kanban-triage-meta plugin (AWO meta-tool pilot).

Collapses the recurring kanban triage chain into a single deterministic
invocation: board status -> blocked cards -> runs/comments -> triage report.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

SCHEMA = {
    "name": "kanban_triage",
    "description": "Triage a kanban board: return blocked cards with evidence (runs, comments) and a recommended action. Deterministic, no LLM.",
    "parameters": {
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Board slug (e.g. realm-forge, hermes-self-improvement). Omit for default board."}
        },
        "required": [],
    },
}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home as _ghh
        return _ghh()
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _board_db_path(board: Optional[str]) -> Path:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    home = _hermes_home()
    if not board or board in ("default", "main"):
        return home / "kanban.db"
    return home / "kanban" / "boards" / board / "kanban.db"


def _triage(board: Optional[str]) -> dict:
    db = _board_db_path(board)
    if not db.exists():
        return {"error": f"board db not found: {db}", "board": board}
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        blocked = [dict(r) for r in conn.execute(
            "SELECT id AS task_id, title, status, block_kind FROM tasks WHERE status='blocked' ORDER BY id"
        ).fetchall()]
        for t in blocked:
            tid = t["task_id"]
            t["runs"] = [dict(r) for r in conn.execute(
                "SELECT id, status, outcome, worker_pid, started_at, ended_at FROM task_runs WHERE task_id=? ORDER BY id", (tid,)
            ).fetchall()]
            t["comments"] = [dict(r) for r in conn.execute(
                "SELECT id, substr(content,1,300) AS content FROM task_comments WHERE task_id=? ORDER BY id", (tid,)
            ).fetchall()]
            # recommended action: if any run succeeded recently -> close; else leave blocked
            ok = any(r.get("status") == "done" or r.get("outcome") == "done" for r in t["runs"])
            t["recommended"] = "close" if ok else "leave-blocked"
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
    finally:
        conn.close()
    return {"board": board or "default", "db": str(db), "total": total, "done": done,
            "blocked_count": len(blocked), "blocked": blocked}


def handle_kanban_triage(args: dict, **kwargs) -> str:
    board = args.get("board") if isinstance(args, dict) else None
    result = _triage(board)
    return json.dumps(result, default=str)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="kanban_triage",
        toolset="kanban-triage-meta",
        schema=SCHEMA,
        handler=handle_kanban_triage,
    )

