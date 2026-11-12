"""kanban-triage-workspace-resolver plugin.

After the triage specifier promotes a card (``triage -> todo``), resolve the
card's workspace *information* and apply it:

- **add** it when the card has none pinned (still ``scratch``) and the card's
  body or notes name a concrete workspace;
- **correct** it when the card's pinned workspace contradicts what the notes say.

Update-safe observation point
-----------------------------
Hooks ``on_kanban_dispatch_tick``. The core ``specify`` path does NOT fire
``on_kanban_task_updated`` — ``kanban_db.specify_triage_task`` performs a raw
``UPDATE tasks`` and never calls ``notify_task_updated`` — so the only observer
that sees a freshly specified card is the dispatcher tick. In the gateway the
watcher runs ``auto_decompose_tick`` (which specifies triage cards when
``kanban.auto_decompose`` is on) and then ``tick_once``, whose tail fires this
hook. For a manual ``hermes kanban specify`` the next gateway tick (<= interval)
picks it up.

Resolution order (first conclusive result wins)
-----------------------------------------------
1. Explicit token in body + notes: ``dir:<path>`` / ``worktree:<path>`` /
   ``scratch`` / an absolute filesystem path.
2. Repo slug (realm-forge, ...) -> canonical checkout from ``$REALM_FORGE_REPO``,
   else the board's ``default_workdir``, else ``~/src/realm-forge-game``.
3. Optional aux-model inference (``KANBAN_TRIAGE_WS_MODEL=1``, default on) for
   free-text notes that state a directory without a machine-readable token.

Every candidate is validated: absolute AND an existing directory
(``os.path.isdir``). Invalid or undetermined -> the workspace is left untouched
and the reason is recorded in a comment (when a change looked warranted).

Idempotent: a ``workspace_resolved`` task_event is written for every card the
resolver considers, so a card is never processed twice.

Known limitation (accepted design, plugin path): the tick hook fires just after
the dispatcher has already claimed ready tasks, so a card specified and claimed
in the same tick may get its first worker run on the old workspace before this
correction lands. ``kanban-scratch-dir-guard`` blocks repo-scratch completion,
so the two converge. Making the workspace correct *before* the first run needs
the core specifier (``kanban_specify``/``specify_triage_task``), not a plugin.

Env knobs
---------
``KANBAN_TRIAGE_WS``            0/false disables the resolver entirely.
``KANBAN_TRIAGE_WS_MODEL``      0/false disables only the aux-model fallback.
``KANBAN_TRIAGE_WS_MODEL_TIMEOUT``  seconds per model call (default 20).
``KANBAN_TRIAGE_WS_MAX_PER_TICK``   candidate cap per tick (default 5).
``KANBAN_TRIAGE_WS_MAX_MODEL_CALLS`` model calls per tick (default 1).
``REALM_FORGE_REPO``            canonical checkout (fallback ~/src/realm-forge-game).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_AGENT = "triage-workspace-resolver"
_EVENT_KIND = "workspace_resolved"

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))

_REPO_SLUGS = (
    "realm-forge", "realm_forge", "realmforge",
    "realm forge", "realm-forge-game", "realm_forge_game", "realmforgegame",
    "realm forge game",
)
_DEFAULT_CANONICAL_REPO = os.environ.get("REALM_FORGE_REPO") or str(
    Path.home() / "src" / "realm-forge-game"
)

# Absolute roots we are willing to accept a bare path token from. Keeps a stray
# "/etc" or "/usr" mention in prose from being taken as a workspace.
_SAFE_ROOTS = tuple(
    p for p in (
        os.environ.get("KANBAN_TRIAGE_WS_ROOTS", "").split(os.pathsep) if
        os.environ.get("KANBAN_TRIAGE_WS_ROOTS") else
        [str(Path.home()), "/srv", "/opt", "/mnt", "/media", "/data", "/work"]
    ) if p
)

_DIR_TOKEN = re.compile(r"\bdir:([^\s\"'`<>)\]]+)")
_WORKTREE_TOKEN = re.compile(r"\bworktree:([^\s\"'`<>)\]]+)")
_ABS_PATH = re.compile(r"(?<![\w:/.-])(/[A-Za-z0-9][^\s\"'`<>)\]|]*)")


def _enabled() -> bool:
    return os.environ.get("KANBAN_TRIAGE_WS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _model_enabled() -> bool:
    return os.environ.get("KANBAN_TRIAGE_WS_MODEL", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _valid_dir(path: Optional[str]) -> Optional[str]:
    """Return the resolved absolute path iff it is an existing directory."""
    if not path or not isinstance(path, str):
        return None
    p = Path(path.strip().strip("`\"'"))
    if not p.is_absolute():
        return None
    try:
        if not p.is_dir():
            return None
    except OSError:
        return None
    return str(p)


def _under_safe_root(path: str) -> bool:
    if not _SAFE_ROOTS:
        return True
    try:
        rp = Path(path).resolve()
    except OSError:
        return False
    for root in _SAFE_ROOTS:
        try:
            rp.relative_to(Path(root).expanduser().resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _board_default_workdir(board: Optional[str]) -> Optional[str]:
    try:
        from hermes_cli.kanban_db import _board_meta_for
        meta = _board_meta_for(board) or {}
        return meta.get("default_workdir") or None
    except Exception:
        return None


def _canonical_repo(board: Optional[str]) -> Optional[str]:
    for cand in (os.environ.get("REALM_FORGE_REPO"), _board_default_workdir(board),
                 _DEFAULT_CANONICAL_REPO):
        good = _valid_dir(cand)
        if good:
            return good
    return None


def _model_workspace(blob: str) -> Optional[str]:
    """Ask the aux specifier model for the directory the task belongs in."""
    try:
        from agent.auxiliary_client import call_llm
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("aux client unavailable: %s", exc)
        return None
    system = (
        "You extract the working directory a software task must run in. "
        "Reply with ONLY one absolute directory path on a single line, or the "
        "single word NONE if the task does not clearly name one. No prose."
    )
    try:
        resp = call_llm(
            task="triage_specifier",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": blob[:4000]}],
            temperature=0.0,
            max_tokens=64,
            timeout=_int_env("KANBAN_TRIAGE_WS_MODEL_TIMEOUT", 20),
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.debug("model workspace call failed: %s", exc)
        return None
    if not out:
        return None
    first = out.splitlines()[0].strip().strip("`\"'")
    if not first or first.upper().startswith("NONE"):
        return None
    return first


def _resolve_workspace(blob: str, board: Optional[str],
                       model_budget: Optional[list] = None) -> Optional[tuple[str, str, str]]:
    """``(kind, path, reason)`` or None. First conclusive rule wins."""
    low = blob.lower()

    for rx, kind in ((_DIR_TOKEN, "dir"), (_WORKTREE_TOKEN, "worktree")):
        m = rx.search(blob)
        if m:
            good = _valid_dir(m.group(1))
            if good:
                return kind, good, f"explicit {kind}: token in the card"
            logger.debug("explicit %s: token %r is not an existing dir; skipping", kind, m.group(1))

    for m in _ABS_PATH.finditer(blob):
        cand = m.group(1)
        good = _valid_dir(cand)
        if good and _under_safe_root(good):
            return "dir", good, "absolute path in body/notes"

    if any(slug in low for slug in _REPO_SLUGS):
        repo = _canonical_repo(board)
        if repo:
            return "dir", repo, "canonical repo for the realm-forge slug"

    if _model_enabled() and model_budget is not None and model_budget[0] > 0:
        model_budget[0] -= 1
        cand = _model_workspace(blob)
        good = _valid_dir(cand)
        if good and _under_safe_root(good):
            return "dir", good, "aux-model inference from body/notes"
        if cand:
            logger.debug("model suggested %r; not an absolute existing dir; ignoring", cand)

    return None


def _find_candidates(conn, limit: int) -> list:
    return conn.execute(
        """
        SELECT t.id, t.title, t.body, t.status, t.assignee,
               t.workspace_kind, t.workspace_path
          FROM tasks t
         WHERE t.status IN ('todo', 'ready', 'running')
           AND EXISTS (SELECT 1 FROM task_events e
                        WHERE e.task_id = t.id AND e.kind = 'specified')
           AND NOT EXISTS (SELECT 1 FROM task_events e
                            WHERE e.task_id = t.id AND e.kind = ?)
         ORDER BY t.created_at DESC
         LIMIT ?
        """,
        (_EVENT_KIND, limit),
    ).fetchall()


def _mark_resolved(conn, task_id: str, payload: dict) -> None:
    try:
        from hermes_cli.kanban_db import write_txn
        with write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (task_id, _EVENT_KIND, json.dumps(payload), int(time.time())),
            )
    except Exception:
        logger.debug("could not write %s event for %s", _EVENT_KIND, task_id, exc_info=True)


def _apply(conn, row, board: Optional[str], model_budget: list) -> None:
    from hermes_cli.kanban_db import add_comment, list_comments, write_txn

    tid = row["id"]
    try:
        notes = " ".join((c.body or "") for c in list_comments(conn, tid))
    except Exception:
        notes = ""
    blob = " ".join(filter(None, [row["title"] or "", row["body"] or "", notes]))

    resolved = _resolve_workspace(blob, board, model_budget)

    cur_kind = (row["workspace_kind"] or "scratch")
    cur_path = row["workspace_path"]

    if resolved is None:
        _mark_resolved(conn, tid, {"outcome": "no_target",
                                   "workspace_kind": cur_kind, "workspace_path": cur_path})
        return

    kind, path, why = resolved
    if kind == cur_kind and (path or None) == (cur_path or None):
        _mark_resolved(conn, tid, {"outcome": "already_correct",
                                   "workspace_kind": kind, "workspace_path": path})
        return

    action = "set" if cur_kind == "scratch" else "corrected"
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind = ?, workspace_path = ? WHERE id = ?",
            (kind, path, tid),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (tid, _EVENT_KIND,
             json.dumps({"outcome": action, "workspace_kind": kind,
                         "workspace_path": path, "reason": why}), int(time.time())),
        )
    try:
        add_comment(
            conn, tid, _AGENT,
            f"Workspace {action} by triage-workspace-resolver: {kind} @ {path}\n"
            f"- reason: {why}\n"
            f"- previous: {cur_kind} @ {cur_path or '(unset)'}",
        )
    except Exception:
        logger.debug("could not comment workspace %s for %s", action, tid, exc_info=True)
    logger.info("triage-workspace-resolver %s workspace for %s -> %s @ %s", action, tid, kind, path)


def on_kanban_dispatch_tick(board: Optional[str] = None, outcome: Any = None,
                            result: Any = None, **kwargs: Any) -> None:
    if not _enabled():
        return
    try:
        from hermes_cli.kanban_db_connect import connect
    except Exception:
        logger.debug("kanban_db_connect unavailable; resolver disabled")
        return
    try:
        conn = connect(board=board or None)
    except Exception:
        logger.debug("could not open board %r", board, exc_info=True)
        return
    try:
        limit = _int_env("KANBAN_TRIAGE_WS_MAX_PER_TICK", 5)
        try:
            rows = _find_candidates(conn, limit)
        except Exception:
            logger.debug("candidate query failed", exc_info=True)
            return
        model_budget = [_int_env("KANBAN_TRIAGE_WS_MAX_MODEL_CALLS", 1)]
        for row in rows:
            try:
                _apply(conn, row, board, model_budget)
            except Exception:
                logger.debug("resolve failed for %s", row["id"], exc_info=True)
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def register(ctx: Any) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", on_kanban_dispatch_tick)
    logger.info("kanban-triage-workspace-resolver registered (on_kanban_dispatch_tick)")
