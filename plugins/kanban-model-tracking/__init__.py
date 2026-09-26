"""kanban-model-tracking plugin - record resolved_model into task_runs.metadata.

Hooks ``kanban_task_claimed`` to store the resolved model (profile default or
per-task ``model_override``) so model-usage statistics can be queried across
all kanban boards.

Survives ``hermes update`` because it lives in ``~/.hermes/plugins/`` - the
agent core's plugin directory is never overwritten during updates.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _maybe_write_probe_marker(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{text}\n")
    except Exception:
        pass


# Best-effort probe marker for validation of claim-time hook execution.
# Writes to a fixed file in HERMES_HOME when the plugin module is loaded,
# when register() is called, and when _on_kanban_task_claimed is entered.
_PROBE_MARKER = Path(
    os.environ.get("KANBAN_MODEL_TRACKING_PROBE_FILE")
    or str(Path.home() / ".hermes" / "kanban" / "plugins" / "kanban-model-tracking.probe")
)
_maybe_write_probe_marker(_PROBE_MARKER, "MODULE_IMPORTED")


def register(ctx) -> None:
    ctx.register_hook("kanban_task_claimed", _on_kanban_task_claimed)
    ctx.register_hook("kanban_task_blocked", _on_kanban_task_blocked)
    logger.info(
        "plugin register: kanban_task_claimed + kanban_task_blocked hooks registered in kanban-model-tracking"
    )
    _maybe_write_probe_marker(_PROBE_MARKER, "REGISTER_CALLED")


# --------------------------------------------------------------------------- #
# Profile model resolution
# --------------------------------------------------------------------------- #

def _resolve_model_from_profile(profile_name: str, hermes_home: Path) -> str | None:
    """Read the profile config and return the configured model.

    Returns a ``provider/model`` string (e.g. ``nous/stepfun/step-3.7-flash:free``)
    or a bare model name if no provider is specified.  Returns ``None`` if
    neither the per-profile config nor the global fallback has a model.
    """
    import yaml  # lazy import - PyYAML is a Hermes dependency

    # Priority 1: per-profile config (~/.hermes/profiles/<name>/config.yaml)
    # Hermes stores the profile's default model under ``model.default`` and the
    # provider under ``model.provider``.
    profile_cfg_path = hermes_home / "profiles" / profile_name / "config.yaml"
    if profile_cfg_path.exists():
        with open(profile_cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            provider = (model_cfg.get("provider") or "").strip()
            model = (model_cfg.get("default") or "").strip()
        elif isinstance(model_cfg, str):
            provider, model = "", model_cfg.strip()
        else:
            provider, model = "", ""
        if model:
            return f"{provider}/{model}" if provider else model

    # Priority 2: global config (~/.hermes/config.yaml)
    global_cfg_path = hermes_home / "config.yaml"
    if global_cfg_path.exists():
        with open(global_cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            provider = (model_cfg.get("provider") or "").strip()
            model = (model_cfg.get("default") or "").strip()
            if model:
                return f"{provider}/{model}" if provider else model

    return None


# --------------------------------------------------------------------------- #
# Hook callback
# --------------------------------------------------------------------------- #

def _on_kanban_task_claimed(
    task_id: str,
    board: str | None = None,
    assignee: str | None = None,
    run_id: int | None = None,
    profile_name: str | None = None,
    **kwargs: Any,
) -> None:
    """Record the resolved model for this task run.

    Fired by :func:`kanban_db._fire_kanban_lifecycle_hook` after the
    claim transaction has committed.  The hook payload carries::

        task_id, board, assignee, run_id, profile_name

    This callback opens the board's kanban DB, reads the task's
    ``model_override`` (if any), falls back to the profile's configured
    model, and writes ``resolved_model`` into ``task_runs.metadata``.

    All exceptions are silently swallowed by ``invoke_hook`` - a failing
    observer never breaks a board transition.
    """
    _maybe_write_probe_marker(_PROBE_MARKER, "HOOK_ENTERED")
    # Resolve HERMES_HOME (the dispatcher's home, not the worker's)
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))

    board_name = board or kwargs.get("board_name", "")
    if not board_name or run_id is None:
        logger.debug("No board or run_id in hook payload - skipping")
        return

    # Resolve the board DB path through core's kanban_db_path() so the
    # default board (which lives at <hermes_home>/kanban.db, not under
    # kanban/boards/default/) is handled correctly.
    try:
        from hermes_cli.kanban_db import kanban_db_path as _kanban_db_path
        db_path = _kanban_db_path(board_name)
    except Exception:
        # Fallback: explicit override env var, then boards layout.
        override = os.environ.get("HERMES_KANBAN_DB", "").strip()
        if override:
            db_path = Path(override).expanduser()
        else:
            db_path = hermes_home / "kanban" / "boards" / board_name / "kanban.db"
    if not db_path.exists():
        logger.debug(
            "Board DB %s does not exist yet - skipping model tracking for task=%s",
            db_path, task_id,
        )
        return
    try:
        conn = sqlite3.connect(str(db_path))

        # 1. Check for per-task model_override (highest priority)
        model_override = None
        cur = conn.execute("SELECT model_override FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if row and row[0]:
            model_override = row[0]

        # 2. Resolve the model (override > profile config > global config)
        # Use the task's assignee (the profile the worker runs under) for
        # model resolution, not the dispatcher's active profile_name.
        worker_profile = assignee or profile_name or "default"
        if model_override:
            resolved_model = model_override
        else:
            resolved_model = _resolve_model_from_profile(worker_profile, hermes_home)

        # 3. Write resolved_model into task_runs.metadata
        if resolved_model and run_id is not None:
            cur = conn.execute(
                """UPDATE task_runs
                   SET metadata = json_set(COALESCE(metadata, '{}'), '$.resolved_model', ?)
                   WHERE id = ?""",
                (resolved_model, run_id),
            )
            rowcount = cur.rowcount
            if rowcount > 0:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(FULL)")
                logger.info(
                    "Recorded resolved_model=%s for run %d (board=%s, task=%s)",
                    resolved_model,
                    run_id,
                    board_name,
                    task_id,
                )
            else:
                logger.warning("No task_runs row matched for run_id=%d", run_id)
        else:
            logger.info(
                "resolved_model is empty for task=%s board=%s run_id=%s worker_profile=%s",
                task_id,
                board_name,
                run_id,
                worker_profile,
            )
    except Exception:
        logger.exception("Failed to record model for kanban task %s", task_id)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Block taxonomy enrichment
# --------------------------------------------------------------------------- #

# Map raw block_kind / block_reason strings to a small taxonomy.
# The taxonomy is intentionally flat — more categories are added when
# the data shows a new stable pattern, not prospectively.
_BLOCK_TAXONOMY = {
    # key = lowercased substring match, value = taxonomy label
    "not alive": "infrastructure",
    "timeout": "infrastructure",
    "spawn_failed": "infrastructure",
    "missing skill": "capability_gap",
    "skill missing": "capability_gap",
    "capability": "capability_gap",
    "workspace": "config_error",
    "cwd": "config_error",
    "profile": "config_error",
    "size": "body_too_large",
    "body too large": "body_too_large",
    "too large": "body_too_large",
    "input": "needs_input",
    "dependency": "external_dependency",
    "parent": "external_dependency",
    "cron": "external_dependency",
    "manual_reclaim": "manual_reclaim",
}


def _classify_block(reason: str | None, kind: str | None) -> str:
    """Return the best taxonomy label for a block event."""
    text = " ".join(str(x or "").lower() for x in (reason, kind))
    for needle, label in _BLOCK_TAXONOMY.items():
        if needle in text:
            return label
    return "other"


def _on_kanban_task_blocked(
    task_id: str,
    board: str | None = None,
    assignee: str | None = None,
    run_id: int | None = None,
    reason: str | None = None,
    kind: str | None = None,
    **kwargs: Any,
) -> None:
    """Record block taxonomy into task_runs.metadata for reporting."""
    _maybe_write_probe_marker(_PROBE_MARKER, "BLOCK_HOOK_ENTERED")
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    board_name = board or kwargs.get("board_name", "")
    if not board_name or run_id is None:
        return

    taxonomy = _classify_block(reason, kind)
    try:
        from hermes_cli.kanban_db import kanban_db_path as _kanban_db_path
        db_path = _kanban_db_path(board_name)
    except Exception:
        override = os.environ.get("HERMES_KANBAN_DB", "").strip()
        db_path = Path(override).expanduser() if override else hermes_home / "kanban" / "boards" / board_name / "kanban.db"
    if not db_path.exists():
        return

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            """UPDATE task_runs
               SET metadata = json_set(
                     COALESCE(metadata, '{}'),
                     '$.block_taxonomy', ?,
                     '$.block_reason', ?,
                     '$.block_kind', ?
                   )
               WHERE id = ?""",
            (taxonomy, reason, kind, run_id),
        )
        if cur.rowcount > 0:
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            logger.info(
                "Recorded block_taxonomy=%s for run %d (board=%s, task=%s)",
                taxonomy, run_id, board_name, task_id,
            )
    except Exception:
        logger.debug("kanban-model-tracking block hook failed", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
