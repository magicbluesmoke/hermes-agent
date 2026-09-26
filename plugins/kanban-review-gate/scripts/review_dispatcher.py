"""kanban-review-dispatcher script.

Finds review cards, dispatches reviewer subagents via delegate_task, aggregates
verdicts, and either approves or requests changes. Iteration-cap and dispatcher
config are enforced from config.yaml.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
    from hermes_constants import get_default_hermes_root as _get_hermes_root
except Exception:
    _get_hermes_home = lambda: Path.home() / "AppData" / "Local" / "hermes"
    _get_hermes_root = lambda: Path.home() / ".hermes"

HERMES_HOME = Path(os.environ.get("HERMES_HOME")) if os.environ.get("HERMES_HOME") else _get_hermes_home()


def _kanban_home() -> Path:
    """Shared kanban root (profile-independent), mirroring hermes_cli.kanban_db.kanban_home.

    Resolving through the active profile's HERMES_HOME would fork the board per
    profile and silently break dispatch in profile-mode cron runs (see
    hermes_constants.get_default_hermes_root). HERMES_HOME itself is still
    correct for config.yaml, so it is retained for that purpose.
    """
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return _get_hermes_root()


KANBAN_ROOT = _kanban_home()
KANBAN_HOME = KANBAN_ROOT / "kanban"
WORKSPACE_ROOT = KANBAN_HOME / "boards" / "workflow-improvements" / "workspaces"


def _board_db_path(board: Optional[str]) -> Path:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    if board and board.strip() and board.strip().lower() != "default":
        return KANBAN_HOME / "boards" / board.strip() / "kanban.db"
    return KANBAN_ROOT / "kanban.db"


def _load_config() -> dict:
    cfg_path = HERMES_HOME / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _review_cards(conn: sqlite3.Connection) -> list[dict]:
    # NOTE: the `tags` column was removed from dispatcher queries — it existed in
    # no board schema (checked all boards + root kanban.db), and nothing downstream
    # consumed the dict key. Kept the static column list aligned with the SELECT.
    rows = conn.execute(
        "SELECT id, title, body, assignee, current_run_id, claim_lock, workspace_kind, workspace_path FROM tasks WHERE status = 'review'"
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "title": r[1],
                "body": r[2],
                "assignee": r[3],
                "current_run_id": r[4],
                "claim_lock": r[5],
                "workspace_kind": r[6],
                "workspace_path": r[7],
            }
        )
    return out


def _event_iterations(conn: sqlite3.Connection, task_id: str) -> int:
    rows = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'review_reopened'",
        (task_id,),
    ).fetchone()
    return int(rows[0]) if rows else 0


def _load_core_module():
    import importlib.util
    import sys as _sys
    candidate = HERMES_HOME / "hermes-agent" / "hermes_cli" / "kanban_db.py"
    spec = importlib.util.spec_from_file_location("hermes_cli.kanban_db", candidate)
    if spec is None or spec.loader is None:
        raise RuntimeError("kanban_db.py not loadable")
    kb = importlib.util.module_from_spec(spec)
    # Register under the real module name: kanban_db.py has @dataclass
    # classes that resolve sys.modules["hermes_cli.kanban_db"]; loading
    # under a fake name dies with AttributeError.
    _sys.modules.setdefault("hermes_cli.kanban_db", kb)
    spec.loader.exec_module(kb)
    return kb


def _ensure_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    try:
        return int(conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()[0])
    except Exception:
        return None


def _approve(task_id: str, board: Optional[str], evidence: str) -> bool:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            kb = _load_core_module()
            run_id = _ensure_run_id(conn, task_id)
            ok = kb.complete_task(
                conn,
                task_id,
                summary=evidence,
                metadata={"plugin": "kanban-review-gate", "dispatcher": "review_dispatcher", "run_id": run_id},
                expected_run_id=run_id,
                with_reason=False,
            )
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


def _request_changes(task_id: str, board: Optional[str], reason: str) -> bool:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            kb = _load_core_module()
            ok = kb.request_changes(
                conn,
                task_id,
                reason=reason,
                expected_run_id=None,
                with_reason=False,
            )
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


def _escalate(task_id: str, board: Optional[str], iterations: int, reason: str) -> bool:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            kb = _load_core_module()
            ok = kb.block_task(
                conn,
                task_id,
                reason=f"review escalation after {iterations} iterations: {reason}",
                kind="needs_input",
                expected_run_id=None,
                with_reason=False,
            )
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


_SUBAGENT_TEMPLATE = """# Subagent Behavioral Guidelines

You are a subagent working for Hermes, serving Michael Anselmi. Follow these rules.

## Output Style
- Be concise. Prefer bullets, numbered steps, inline code.
- No wall-of-text paragraphs unless summarizing findings.
- Default to plain text, not markdown (terminal-renderable).

## Don't
- Narrate your tool calls or internal reasoning — report only results.
- Fabricate output (data, file contents, API responses). Report blockers truthfully.
- Ask questions — you cannot receive answers mid-task. Make the best call with available info.
- Apologize or speculate about tool behavior.

## On Failure
- Retry once with a meaningfully different approach if a command/tool fails.
- If still failing, say so clearly in your summary and propose the next step.
- Return verifiable handles for any work done: absolute file paths, URLs, test output.

## Safety
- Confirm before destructive operations (delete, overwrite, system config changes).
- If a path contains "Michael Anselmi" (with space), quote it in shell commands.

## Scope
- Stay within the task. Don't refactor adjacent code or add features you weren't asked for.
- If something is ambiguous, pick the most likely interpretation and flag it in your summary.
"""


def _reviewer_context(task: dict, role: str) -> str:
    board = os.environ.get("HERMES_KANBAN_BOARD") or "default"
    workspace = task.get("workspace_path") or ""
    workspace_kind = task.get("workspace_kind") or ""
    canonical_root = WORKSPACE_ROOT / str(task.get("id", ""))
    canonical_note = ""
    if canonical_root.exists():
        canonical_note = f"\nCanonical checkout context: {canonical_root}\nUse this path for any evidence collection."
    return "\n".join([
        _SUBAGENT_TEMPLATE,
        "## Reviewer Identity",
        f"- Role: {role} reviewer",
        "- You are a kanban review agent. Output a structured verdict only.",
        "## Task Under Review",
        f"- task_id: " + str(task.get("id")),
        f"- title: " + str(task.get("title")),
        f"- board: " + str(board),
        f"- workspace_kind: {workspace_kind}",
        f"- workspace_path: {workspace}" + canonical_note,
        "## Verdict Contract",
        "Respond with JSON only:",
        '{ "verdict": "pass" | "fail", "gaps": [...], "evidence": "..." }',
        "If verdict is fail, gaps must be a list of concrete blockers. If pass, evidence must summarize the review.",
    ])


def _parse_verdict(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("verdict") in {"pass", "fail"}:
            return {
                "verdict": str(data.get("verdict")),
                "gaps": data.get("gaps") if isinstance(data.get("gaps"), list) else [],
                "evidence": str(data.get("evidence") or ""),
            }
    except Exception:
        pass
    lower = text.lower()
    if "pass" in lower:
        return {"verdict": "pass", "gaps": [], "evidence": text[:500]}
    return {"verdict": "fail", "gaps": [text[:500]], "evidence": text[:500]}


def _delegate_review(task: dict, role: str, model: Optional[str]) -> dict[str, Any]:
    try:
        from hermes_tools import delegate_task
    except Exception as exc:
        # hermes_tools only exists inside a live agent session. This module is
        # sometimes imported by tests or cron subprocesses; fail loudly with a
        # clear diagnostic instead of silently emitting a fake "fail" verdict.
        return {
            "verdict": "fail",
            "gaps": [
                "delegate_task unavailable: hermes_tools is only importable "
                f"inside a live Hermes agent session ({exc}). Run the "
                "kanban-review-dispatch cron (inline dispatch) instead of this "
                "script as a subprocess."
            ],
            "evidence": "",
        }
    try:
        result = delegate_task(
            context=_reviewer_context(task, role),
            goal=f"Review kanban task {task.get('id')} as a {role} reviewer.",
            output_schema={
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["pass", "fail"]},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "string"},
                },
                "required": ["verdict"],
            },
            model=model,
        )
        return _parse_verdict(result)
    except Exception as exc:
        return {"verdict": "fail", "gaps": [f"delegate failure: {exc}"], "evidence": ""}


def _iterations_since_last_review(conn: sqlite3.Connection, task_id: str) -> int:
    rows = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind IN ('review_reopened','review_dispatched')",
        (task_id,),
    ).fetchone()
    return int(rows[0]) if rows else 0


def dispatch(board: Optional[str], max_iterations: int = 3) -> dict:
    db_path = _board_db_path(board)
    if not db_path.exists():
        return {"status": "no_db", "board": board}
    cfg = _load_config()
    dispatcher_cfg = cfg.get("kanban_review_dispatcher", {}) or {}
    max_iterations = int(dispatcher_cfg.get("max_review_iterations", max_iterations) or max_iterations)
    interval_hours = int(dispatcher_cfg.get("interval_hours", 0) or 0)
    spec_model = dispatcher_cfg.get("reviewer_models", {}).get("spec")
    quality_model = dispatcher_cfg.get("reviewer_models", {}).get("quality")

    conn = sqlite3.connect(str(db_path))
    try:
        cards = _review_cards(conn)
        results = []
        for card in cards:
            if card.get("current_run_id") or card.get("claim_lock"):
                results.append({"id": card["id"], "action": "skipped", "reason": "in_progress"})
                continue
            iterations = _iterations_since_last_review(conn, card["id"])
            if iterations >= max_iterations:
                _escalate(card["id"], board, iterations, "iteration cap reached")
                results.append({"id": card["id"], "action": "escalated", "iterations": iterations})
                continue
            spec = _delegate_review(card, "spec", spec_model)
            quality = _delegate_review(card, "quality", quality_model)
            verdict = "pass" if spec["verdict"] == "pass" and quality["verdict"] == "pass" else "fail"
            gaps = (spec.get("gaps") or []) + (quality.get("gaps") or [])
            evidence = (spec.get("evidence") or "").strip() + "\n" + (quality.get("evidence") or "").strip()
            if verdict == "pass":
                ok = _approve(card["id"], board, evidence.strip() or "Subagent review pass")
                results.append({"id": card["id"], "action": "completed" if ok else "approve_failed", "verdict": "pass"})
            else:
                ok = _request_changes(card["id"], board, "\n".join(gaps) if gaps else "Subagent review fail")
                results.append({"id": card["id"], "action": "request_changes" if ok else "changes_failed", "verdict": "fail", "gaps": gaps})
        return {"status": "ok", "reviewed": len(results), "interval_hours": interval_hours, "results": results}
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    cfg = _load_config()
    board = os.environ.get("HERMES_KANBAN_BOARD") or "realm-forge"
    dispatcher_cfg = cfg.get("kanban_review_dispatcher", {}) or {}
    max_iterations = int(dispatcher_cfg.get("max_review_iterations", 3) or 3)
    result = dispatch(board=board, max_iterations=max_iterations)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
