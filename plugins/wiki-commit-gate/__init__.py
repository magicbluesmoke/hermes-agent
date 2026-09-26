"""wiki-commit-gate plugin.

Hooks ``kanban_task_completed`` on the wiki-research board and requires that a
wiki commit in the recent window references the card (task id) or a page path
named in the card body (``Wiki:`` line / ``.md`` token / ``entities/..`` token).
Missing reference -> the card is re-blocked with guidance. This closes the gap
where wiki-research cards (t_46936418 Cactus Needle 3, t_4121124a JEV mode,
2026-09-25) were closed with no wiki page committed at all.

Contract conventions (mirror the verify-gate plugin):
- Card bodies should carry an explicit scope line, e.g.
  ``Wiki: entities/needle-3.md concepts/system-one-aux-integration.md``
  (analogue of the verify-gate ``Verify:`` line). Without it the gate falls
  back to any ``.md`` tokens / ``entities|concepts|comparisons|operations``
  paths / the task id found in the body.
- The wiki commit must exist in the git log within the window (default 96h,
  ``WIKI_GATE_COMMIT_WINDOW_HOURS``) and its message must mention the task id
  OR one of the derived page paths (with or without ``.md``).
- Operator override: operator (author default/operator/local-only) posts
  ``wiki-commit-gate: override <reason>`` (legacy ``verify-gate: override``
  markers are also honored) -> the gate records evidence and passes instead of
  re-blocking. kanban-worker can never self-exempt.

The veto mirrors core and the verify-gate plugin: ``kanban_task_completed``
fires AFTER the completion txn commits and hooks are observer-only, so a done
card is flipped back to ``blocked`` via ``reopen_task`` (if merged) or the raw
scoped CAS (``status='done'`` -> ``blocked``) plus a sticky ``blocked`` event
and a ``kanban_task_blocked`` fire.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WIKI_PATH = Path(
    os.environ.get("WIKI_GATE_WIKI", "").strip()
    or Path.home() / "wiki"
).expanduser()
_BOARDS = {
    b.strip()
    for b in os.environ.get("WIKI_GATE_BOARDS", "wiki-research").split(",")
    if b.strip()
}
_COMMIT_WINDOW_HOURS = int(os.environ.get("WIKI_GATE_COMMIT_WINDOW_HOURS", "96"))

_OPERATOR_AUTHORS = {"default", "operator", "local-only"}
_OVERRIDE_MARKER = re.compile(
    r"(?:wiki-commit-gate|verify-gate):\s*override\b", re.IGNORECASE
)
_WIKI_LINE = re.compile(r"(?:wiki|pages)\s*:\s*(?P<rest>[^\n\r|]+)", re.IGNORECASE)
_MD_TOKEN = re.compile(r"([A-Za-z0-9_./\[\]#-]+\.md)\b", re.IGNORECASE)
_SECTION_PATH = re.compile(
    r"\b(?:entities|concepts|comparisons|operations|raw|research|queries|assets)"
    r"/([A-Za-z0-9_-]+)(?:\.md)?\b",
    re.IGNORECASE,
)
_TASK_ID = re.compile(r"\bt_[0-9a-f]{6,}\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Pure logic (no hermes_cli imports -> unit-testable standalone)
# --------------------------------------------------------------------------- #

def _normalize(token: str) -> str:
    return token.strip().strip("[]").strip().lower()


def _plausible_ref(tok: str) -> bool:
    """A token is a plausible wiki reference only if it is a task id, a
    section-relative path, or a .md filename — never bare prose words
    ('update', 'new', '(new);') that would false-match common commit text."""
    if not tok or tok == ".md":
        return False
    if re.fullmatch(r"t_[0-9a-f]{6,}", tok):
        return True
    if "/" in tok or tok.endswith(".md"):
        return True
    return False


def _expand_ref(tok: str, refs: dict[str, str]) -> None:
    """Register canonical variants (path, stem, basename, stem-of-basename)."""
    if tok.endswith(".md"):
        refs.setdefault(tok, tok)
        stem = tok[:-3]
        refs.setdefault(stem, stem)
        base = tok.rsplit("/", 1)[-1]
        refs.setdefault(base, base)
        if base.endswith(".md"):
            refs.setdefault(base[:-3], base[:-3])
    else:
        refs.setdefault(tok, tok)


def _derive_refs(body: str) -> list[str]:
    """Derive candidate wiki references from a card body.

    Sources: an explicit ``Wiki:``/``Pages:`` line, any ``*.md`` token,
    section-relative paths (entities/..., concepts/...), and the task id.
    """
    body = body or ""
    raw: set[str] = set()

    for m in _WIKI_LINE.finditer(body):
        for tok in m.group("rest").split():
            t = _normalize(tok.lstrip("-"))
            if _plausible_ref(t):
                raw.add(t)
    for m in _MD_TOKEN.finditer(body):
        t = _normalize(m.group(1))
        if _plausible_ref(t):
            raw.add(t)
    for m in _SECTION_PATH.finditer(body):
        t = _normalize(m.group(0))
        if _plausible_ref(t):
            raw.add(t)
        if not t.endswith(".md"):
            raw.add(t + ".md")
    for m in _TASK_ID.finditer(body):
        raw.add(_normalize(m.group(0)))

    refs: dict[str, str] = {}
    for t in sorted(raw):
        _expand_ref(t, refs)
    return sorted(refs)


def _commit_matches(refs: list[str], window_hours: int = _COMMIT_WINDOW_HOURS
                    ) -> Optional[dict[str, str]]:
    """Return the newest wiki commit in the window matching any ref.

    Returns a dict {ref, hash, subject} or None when no commit matches. Raises
    RuntimeError when the wiki repo cannot be inspected (fail-closed).
    """
    if not refs:
        return None
    if not (_WIKI_PATH / ".git").is_dir():
        raise RuntimeError(f"wiki repo missing: {_WIKI_PATH}")
    proc = subprocess.run(
        ["git", "-C", str(_WIKI_PATH), "log",
         f"--since={window_hours} hours ago",
         "--format=%H%x1f%s%x1f%b%x1e"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {_WIKI_PATH}: {proc.stderr.strip()[:300]}")
    lowered = [r.lower() for r in refs]
    records = [rec for rec in proc.stdout.split("\x1e") if rec.strip()]
    for record in records:
        parts = record.split("\x1f")
        if len(parts) < 3:
            continue
        commit_hash, subject, rest = parts[0].strip(), parts[1].strip(), parts[2]
        haystack = f"{subject} {rest}".lower()
        for ref in lowered:
            if ref and ref in haystack:
                return {"ref": ref, "hash": commit_hash,
                        "subject": subject[:180]}
    return None


def _last_commit_summary() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(_WIKI_PATH), "log", "-1",
             "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return "(git unavailable)"


# --------------------------------------------------------------------------- #
# Board DB + lifecycle helpers (hermes_cli imports inside functions)
# --------------------------------------------------------------------------- #

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home as _ghh
    return _ghh()


def _board_db_path(board: Optional[str]) -> Path:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    home = _hermes_home()
    if board and board.strip() and board.strip().lower() != "default":
        return home / "kanban" / "boards" / board.strip() / "kanban.db"
    return home / "kanban.db"


def _connect(board: Optional[str]):
    from hermes_cli.kanban_db_connect import connect
    return connect(board=board)


def _card_body(task_id: str, board: Optional[str]) -> str:
    try:
        from hermes_cli.kanban_db import get_task
        conn = _connect(board)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
        if row is not None:
            return getattr(row, "body", "") or getattr(row, "title", "") or ""
    except Exception:
        logger.debug("wiki-commit-gate: _card_body failed", exc_info=True)
    return ""


def _has_operator_override(task_id: str, board: Optional[str]) -> bool:
    try:
        from hermes_cli.kanban_db import list_comments
        conn = _connect(board)
        try:
            comments = list_comments(conn, task_id)
        finally:
            conn.close()
    except Exception:
        return False
    for c in comments or []:
        author = getattr(c, "author", "") or ""
        body = getattr(c, "body", "") or ""
        if author in _OPERATOR_AUTHORS and _OVERRIDE_MARKER.search(body):
            return True
    return False


def _comment_task(task_id: str, board: Optional[str], body: str,
                  author: str = "wiki-commit-gate") -> None:
    try:
        from hermes_cli.kanban_db import add_comment
        conn = _connect(board)
        try:
            add_comment(conn, task_id, author, body)
        finally:
            conn.close()
    except Exception:
        logger.debug("wiki-commit-gate: comment failed", exc_info=True)


def _veto_done_task(task_id: str, board: Optional[str], reason: str,
                    kind: str = "needs_input") -> bool:
    """Flip a just-completed task back to ``blocked`` — the gate veto."""
    try:
        from hermes_cli.kanban_db import reopen_task as _reopen_core
        from hermes_cli.kanban_db_connect import connect as _connect_core
    except Exception:
        _reopen_core = None
    if _reopen_core is not None:
        try:
            conn = _connect_core(board=board or None)
            try:
                return bool(_reopen_core(
                    conn, task_id, reason=reason, landing="blocked",
                    kind=kind, author="wiki-commit-gate",
                ))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return False
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
                    (task_id, json.dumps(
                        {"reason": reason, "kind": kind, "recurrences": 1,
                         "source_status": "done"}),
                     int(time.time())),
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


# --------------------------------------------------------------------------- #
# Hook entry
# --------------------------------------------------------------------------- #

def on_kanban_task_completed(
    task_id: str,
    board: str = "",
    assignee: str = "",
    run_id: Optional[str] = None,
    summary: Optional[str] = None,
    task_body: Optional[str] = None,
    **kwargs: Any,
) -> None:
    if board != "wiki-research" and board not in _BOARDS:
        return

    body = (task_body or "").strip() or _card_body(task_id, board)
    override = _has_operator_override(task_id, board)

    try:
        refs = _derive_refs(body)
        if not refs:
            reason = (
                "wiki_commit_gate: no wiki reference derivable from the card "
                "body. Cards on this board close only with a committed wiki "
                "page; add a scope line to the body, e.g. "
                "`Wiki: entities/<page>.md`, state any .md page path the "
                "deliverable lands in, or cite the task id "
                "(t_<hex>) in the wiki commit message. If the closing is "
                "legitimate without a page (research-only), an operator must "
                "post `wiki-commit-gate: override <reason>`."
            )
            if override:
                _comment_task(
                    task_id, board,
                    "wiki-commit-gate: operator override honored — no wiki "
                    "reference derivable, skipped. " + reason,
                )
                return
            _comment_task(task_id, board, reason)
            _veto_done_task(task_id, board, reason)
            return

        match = _commit_matches(refs)
        if match:
            _comment_task(
                task_id, board,
                "wiki-commit-gate: evidence — commit "
                f"{match['hash'][:8]}: {match['subject']!r} matches "
                f"reference {match['ref']!r} within the "
                f"{_COMMIT_WINDOW_HOURS}h window.",
            )
            return

        reason = (
            "wiki_commit_gate: no wiki commit in the last "
            f"{_COMMIT_WINDOW_HOURS}h references "
            + ", ".join(f"'{r}'" for r in refs)
            + ". Expected the wiki page deliverable to be committed and its "
            "log entry or commit message to cite the card (task id or page "
            f"path). Last wiki commit: {_last_commit_summary()}. If the "
            "closing is legitimate without a matching commit, post "
            "`wiki-commit-gate: override <reason>` as operator."
        )
        if override:
            _comment_task(
                task_id, board,
                "wiki-commit-gate: operator override honored — no matching "
                "wiki commit, skipped. " + reason,
            )
            return
        _comment_task(task_id, board, reason)
        _veto_done_task(task_id, board, reason)
    except Exception as exc:  # fail-closed on infra errors, never raise
        reason = (
            f"wiki_commit_gate: gate could not verify a wiki commit "
            f"({type(exc).__name__}: {exc}). Fail-closed: re-blocked for "
            "operator review."
        )
        try:
            _comment_task(task_id, board, reason)
        except Exception:
            pass
        _veto_done_task(task_id, board, reason)


def register(ctx: Any) -> None:
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)