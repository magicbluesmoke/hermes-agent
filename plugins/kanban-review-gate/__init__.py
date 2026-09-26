"""kanban-review-gate plugin.

Auto-requests review for qualifying kanban tasks after completion.
Fires via the kanban_task_completed hook.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
except Exception:
    _get_hermes_home = lambda: Path.home() / "AppData" / "Local" / "hermes"

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.environ.get("HERMES_HOME")) if os.environ.get("HERMES_HOME") else _get_hermes_home()
KANBAN_HOME = HERMES_HOME / "kanban"


def _board_db_path(board: Optional[str]) -> Path:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    if board and board.strip() and board.strip().lower() != "default":
        scoped = KANBAN_HOME / "boards" / board.strip() / "kanban.db"
        if scoped.exists():
            return scoped
        # Worker processes run profile-scoped (HERMES_HOME points at
        # ~/.hermes/profiles/<name>), so the profile-local boards path does
        # not exist — the real boards live under the main profile home.
        if os.environ.get("HERMES_PROFILE"):
            main_home = Path.home() / ".hermes"
            main_db = main_home / "kanban" / "boards" / board.strip() / "kanban.db"
            if main_db.exists():
                return main_db
        return scoped
    return HERMES_HOME / "kanban.db"


def _table_has_column(db_path: Path, column: str) -> bool:
    """Schema drift guard: boards created before `tags` was added to the
    tasks table lack that column; a SELECT referencing it throws and the
    plugin silently skips every card. Check column existence instead."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            return column in cols
        finally:
            conn.close()
    except Exception:
        return False


def _get_config() -> dict:
    cfg_path = HERMES_HOME / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _qualify(task_id: str, board: Optional[str]) -> Optional[dict]:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return None
    try:
        tags_col = _table_has_column(db_path, "tags")
        expires_col = _table_has_column(db_path, "claim_expires")
        select_tags = ", tags" if tags_col else ""
        select_expires = ", claim_expires" if expires_col else ""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, title, body, status, current_run_id, claim_lock, "
                "workspace_kind, workspace_path" + select_expires + select_tags
                + " FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            status = str(row["status"])
            if status == "done":
                state = "completed"
            elif status == "review":
                state = "in_review"
            elif status in {"blocked", "todo", "ready", "scheduled"}:
                state = status
            else:
                state = "unknown"
            return {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "body": str(row["body"]),
                "status": state,
                "tags": str(row["tags"] or "") if tags_col else "",
                "current_run_id": row["current_run_id"],
                "claim_lock": row["claim_lock"],
                "claim_expires": row["claim_expires"] if expires_col else None,
                "workspace_kind": row["workspace_kind"],
                "workspace_path": row["workspace_path"],
            }
        finally:
            conn.close()
    except Exception:
        return None


def _file_risk_tier(rel_path: str, tiers: dict[str, list[str]]) -> str:
    normalized = "/" + rel_path.replace("\\", "/").lstrip("/")
    for tier, tokens in sorted(tiers.items(), key=lambda kv: (kv[0] == "always", kv[0])):
        if tier == "never":
            continue
        for token in tokens:
            token = token.strip().lower()
            if not token:
                continue
            norm_token = "/" + token.strip("/")
            if normalized == norm_token or normalized.startswith(norm_token + "/"):
                return tier
    return "content"

def _changed_files(workspace_path: Optional[str], workspace_kind: Optional[str]) -> list[str]:
    if not workspace_path or workspace_kind not in {"dir", "worktree", "scratch"}:
        return []
    repo = os.path.expanduser(os.path.normpath(workspace_path))
    git_dir = os.path.join(repo, ".git")
    if not os.path.isdir(git_dir):
        return []
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=repo, timeout=15,
        )
        if r.returncode != 0:
            return []
        seen = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                continue
            seen[parts[1]] = seen.get(parts[1], 0) + 1
        return list(seen.keys())
    except Exception:
        return []


def _qualification_reason(task: dict, changed_lines: Optional[int], new_files: int) -> Optional[str]:
    body = (task.get("body") or "").lower()
    tags = (task.get("tags") or "").lower()
    title = (task.get("title") or "").lower()
    text = "\n".join([title, body, tags])
    cfg = _get_config()
    gate_cfg = cfg.get("plugins", {}).get("kanban_review_gate", {})
    always = [str(x).lower() for x in gate_cfg.get("always_review", [])]
    never = [str(x).lower() for x in gate_cfg.get("never_review", ["docs", "triage"])]
    min_lines = int(gate_cfg.get("min_changed_lines", 20) or 20)
    tiers = gate_cfg.get("risk_tiers", {}) or {}
    always_tokens = [t.strip().lower() for t in (tiers.get("always") or [])]
    never_tokens = [t.strip().lower() for t in (tiers.get("never") or [])]

    if any(token in text for token in [f"no_review:{x}" for x in never] + ["no_review"]):
        return None
    if any(token in text for token in [f"review:{x}" for x in always] + ["review: required"]):
        return "flagged for required review"
    if any(token in text for token in never_tokens):
        return None

    ws_path = task.get("workspace_path")
    ws_kind = task.get("workspace_kind")
    changed_files = _changed_files(ws_path, ws_kind)
    tier_hits = []
    for rel in changed_files:
        tier = _file_risk_tier(rel, tiers)
        if tier != "never":
            tier_hits.append((tier, rel))
        if tier == "always":
            return f"risk-tiered: always-review path touched: {rel}"

    always_hits = [rel for tier, rel in tier_hits if tier == "always"]
    content_hits = [rel for tier, rel in tier_hits if tier == "content"]
    if always_hits:
        return f"risk-tiered: always-review path touched: {always_hits[0]}"
    if tier_hits and not content_hits:
        return f"risk-tiered: non-content change detected: {tier_hits[0][1]}"

    if any(token in text for token in always):
        return "flagged for required review"
    if any(token in text for token in never):
        return None
    if new_files > 0:
        return f"new files added: {new_files}"
    if changed_lines is not None and changed_lines >= min_lines:
        return f"changed lines {changed_lines} >= threshold {min_lines}"
    return None


def _changed_lines(workspace_path: Optional[str], workspace_kind: Optional[str]) -> tuple[Optional[int], int]:
    if not workspace_path or workspace_kind not in {"dir", "worktree", "scratch"}:
        return None, 0
    repo = os.path.expanduser(os.path.normpath(workspace_path))
    git_dir = os.path.join(repo, ".git")
    if not os.path.isdir(git_dir):
        return None, 0
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=repo, timeout=15,
        )
        if r.returncode != 0:
            return None, 0
        lines = r.stdout.splitlines()
        added = sum(1 for line in lines if line.startswith("A ") or line.startswith("M ") or line.startswith("?? "))
        stat = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            capture_output=True, text=True, cwd=repo, timeout=15,
        )
        changed = 0
        if stat.returncode == 0 and stat.stdout.strip():
            tail = stat.stdout.strip().splitlines()[-1]
            parts = tail.split(",")
            for part in parts:
                part = part.strip()
                if part.endswith(" +"):
                    part = part[:-2].strip()
                if part.endswith(" deletions(-)"):
                    try:
                        changed += int(part.split()[0])
                    except Exception:
                        pass
                elif part.endswith(" insertion(+)"):
                    try:
                        changed += int(part.split()[0])
                    except Exception:
                        pass
                elif part.endswith(" insertions(+)"):
                    try:
                        changed += int(part.split()[0])
                    except Exception:
                        pass
                elif part.endswith(" deletion(-)"):
                    try:
                        changed += int(part.split()[0])
                    except Exception:
                        pass
        return changed if changed > 0 else None, added
    except Exception:
        return None, 0


def _request(task_id: str, board: Optional[str], summary: str, metadata: dict, reviewer: Optional[str]) -> bool:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row  # core request_review indexes by column name
        try:
            # NOTE: no explicit BEGIN here — request_review()/request_changes()
            # wrap their writes in write_txn() themselves, and a manual BEGIN
            # IMMEDIATE causes the core's nested-transaction guard to raise
            # RuntimeError, which the outer except would swallow into False.
            import importlib.util
            import sys as _sys
            candidate = HERMES_HOME / "hermes-agent" / "hermes_cli" / "kanban_db.py"
            if not candidate.exists() and os.environ.get("HERMES_PROFILE"):
                # Worker processes run profile-scoped; the Hermes core lives
                # under the main profile home.
                candidate = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "kanban_db.py"
            spec = importlib.util.spec_from_file_location("hermes_cli.kanban_db", candidate)
            if spec is None or spec.loader is None:
                return False
            kb = importlib.util.module_from_spec(spec)
            # Register under its real module name before exec: kanban_db.py
            # contains @dataclass classes that resolve their own module via
            # sys.modules — loading under a fake name ("kb") leaves that
            # lookup as None and the import dies with AttributeError, which
            # the outer except then swallows into a silent False.
            _sys.modules.setdefault("hermes_cli.kanban_db", kb)
            spec.loader.exec_module(kb)
            ok = kb.request_review(
                conn,
                task_id,
                summary=summary,
                metadata=metadata,
                reviewer=reviewer,
                force=True,
                with_reason=True,
            )
            if ok and isinstance(ok, tuple):
                ok, reason = ok
                if not ok:
                    logger.warning("request_review refused for %s: %s", task_id, reason)
            if ok:
                conn.commit()
                return True
            conn.rollback()
            return False
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
    except Exception:
        return False


def _comment(task_id: str, board: Optional[str], author: str, body: str) -> bool:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author, body, int(time.time())),
            )
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return False
        finally:
            conn.close()
    except Exception:
        return False


def _on_complete(task_id: str, board: Optional[str], workspace_path: Optional[str], workspace_kind: Optional[str]) -> None:
    if not task_id:
        return
    task = _qualify(task_id, board)
    if not task:
        return
    if task.get("current_run_id"):
        return
    claim_lock = task.get("claim_lock")
    claim_expires = task.get("claim_expires")
    now = int(time.time())
    if claim_lock:
        if claim_expires and now > int(claim_expires):
            _comment(
                task_id,
                board,
                "kanban-review-gate",
                f"Review lane claim expired for owner={claim_lock}; re-requesting review.",
            )
        else:
            return
    # Prefer the explicit hook kwargs (added to kanban_task_completed payload
    # in kanban_db.complete_task); fall back to the task row for callers that
    # fire without them.
    ws_path = workspace_path or task.get("workspace_path")
    ws_kind = workspace_kind or task.get("workspace_kind")
    changed, new_files = _changed_lines(ws_path, ws_kind)
    reason = _qualification_reason(task, changed, new_files)
    if not reason:
        return
    cfg = _get_config()
    gate_cfg = cfg.get("plugins", {}).get("kanban_review_gate", {})
    reviewer = gate_cfg.get("reviewer_default") or os.environ.get("HERMES_PROFILE", "default")
    summary = f"Auto-review requested: {reason}"
    metadata = {
        "plugin": "kanban-review-gate",
        "qualification_reason": reason,
        "changed_lines": changed,
        "new_files": new_files,
    }
    ok = _request(task_id, board, summary=summary, metadata=metadata, reviewer=reviewer)
    if ok:
        logger.info("Auto-requested review for task %s: %s", task_id, reason)
    else:
        logger.debug("Auto-review request failed for task %s", task_id)


def register(ctx: Any) -> None:
    def on_kanban_complete(**kwargs: Any) -> None:
        task_id = kwargs.get("task_id")
        board = kwargs.get("board")
        workspace_path = kwargs.get("workspace_path")
        workspace_kind = kwargs.get("workspace_kind")
        _on_complete(
            str(task_id) if task_id else None,
            str(board) if board else None,
            str(workspace_path) if workspace_path else None,
            str(workspace_kind) if workspace_kind else None,
        )

    ctx.register_hook("kanban_task_completed", on_kanban_complete)
