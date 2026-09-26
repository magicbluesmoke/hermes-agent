"""kanban-auto-commit plugin.

Auto-commit uncommitted changes in dir: workspace git repos after
kanban task completion. Fires via the kanban_task_completed hook
which runs in the worker process after kanban_complete() succeeds.

Survives hermes-agent updates because user plugins live under
~/.hermes/plugins/, not in the agent checkout.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
except Exception:
    _get_hermes_home = lambda: Path.home() / "AppData" / "Local" / "hermes"

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.environ.get("HERMES_HOME")) if os.environ.get("HERMES_HOME") else _get_hermes_home()
KANBAN_HOME = HERMES_HOME / "kanban"


def _board_db_path(board: str | None) -> Path:
    """Resolve a board name to its kanban.db path.

    Priority order mirrors the core's ``kanban_db_path`` so the plugin
    works correctly inside worker subprocesses (profile-scoped
    HERMES_HOME) as well as the main agent process (root HERMES_HOME):

    1. ``HERMES_KANBAN_DB`` env var — the dispatcher injects this into
       worker envs explicitly so the board is immune to path-resolution
       disagreements between root and profile HERMES_HOME.
    2. ``HERMES_HOME/kanban/boards/<slug>/kanban.db`` for named boards,
       ``HERMES_HOME/kanban.db`` for the default board.

    Mirrors kanban_db.kanban_db_path() / kanban_home() logic without
    importing the Hermes internal API.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    if board and board.strip() and board.strip().lower() != "default":
        return KANBAN_HOME / "boards" / board.strip() / "kanban.db"
    return HERMES_HOME / "kanban.db"


# A real git conflict-marker line begins with a 7-char prefix.
_MARKER_LEN = 7


def _conflict_marker_findings(content: str) -> list[str]:
    """Find git conflict-marker lines in staged file ``content``.

    Returns a list of ``"lineno:marker"`` findings (1-indexed). An empty list
    means the file is clean.

    False-positive policy (rate kept near zero):
    * ``<<<<<<<`` (the 7-char prefix) at line start is *always* a conflict
      start marker.
    * ``>>>>>>>`` (the 7-char prefix) at line start is *always* a conflict
      end marker.
    * ``=======`` is git's separator between the two sides. It is flagged
      **only** when it occurs inside a ``<<<<<<<``...``>>>>>>>`` block (i.e.
      paired with a start/end marker). A bare ``=======`` — a Markdown/RST
      section underline, a Markdown table separator, or a long ``=====``
      divider — is *not* flagged, which is what keeps the false-positive rate
      at zero.
    """
    findings: list[str] = []
    in_conflict = False
    for lineno, line in enumerate(content.splitlines(), 1):
        if line.startswith("<<<<<<<"):
            in_conflict = True
            findings.append(f"{lineno}:<<<<<<<")
        elif line.startswith(">>>>>>>"):
            findings.append(f"{lineno}:>>>>>>>")
            in_conflict = False
        elif line == "=======" and in_conflict:
            findings.append(f"{lineno}:=======")
    return findings


def _read_staged_text(repo: str, rel: str) -> str | None:
    """Return the text of a staged file, or ``None`` if it is absent / binary.

    ``repo`` is an absolute repo path; ``rel`` is a path relative to the repo
    root (as produced by ``git diff --cached --name-only``). Reads the working
    tree copy, which at this point equals the staged blob because staging
    (``git add``) has already run immediately upstream and no edits happen
    between staging and this check.
    """
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return None  # staged deletion: nothing to inspect
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None  # binary file cannot carry a text conflict marker
    # errors="replace" preserves ASCII markers regardless of the file's
    # encoding, so a latin-1/ascii file containing <<<<<<< is still caught.
    return raw.decode("utf-8", errors="replace")


def _auto_commit(task_id: str, board: str | None) -> None:
    """Look up the task's workspace info and auto-commit if applicable."""
    db_path = _board_db_path(board)
    if not db_path.exists():
        return

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return

            kind = str(row["workspace_kind"])
            path = row["workspace_path"]
            # Accept both plain "dir" (majority of tasks) and the older
            # "dir:<path>" colon-prefixed form. The previous startswith("dir:")
            # guard matched only 1 task on the realm-forge board, silently
            # no-op'ing every worker completion (773 tasks stored "dir").
            if not (kind == "dir" or kind.startswith("dir:")) or not path:
                return

            repo = os.path.normpath(os.path.expanduser(str(path)))
            git_dir = os.path.join(repo, ".git")
            if not os.path.isdir(git_dir):
                return

            # Check for uncommitted changes
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=repo, timeout=15,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return

            # Stage scoped: always stage tracked modifications/deletions
            # (git add -u), and stage new untracked files only when they are
            # not build/artifact noise. Avoids sweeping unrelated untracked
            # files (critique notes, build output, golden artifacts) into the
            # worker's auto-commit.
            subprocess.run(
                ["git", "add", "-u"],
                capture_output=True, cwd=repo, timeout=30,
            )
            _ARTIFACT_DIRS = ("build/", "target/", "dist/", "__pycache__/")
            _ARTIFACT_SUFFIXES = (".pyc", "_current.png", "_diff.png")

            def _is_artifact(rel: str) -> bool:
                if any(rel.startswith(d) or "/" + d in rel for d in _ARTIFACT_DIRS):
                    return True
                return any(rel.endswith(s) for s in _ARTIFACT_SUFFIXES)

            for line in r.stdout.splitlines():
                if not line.startswith("?? "):
                    continue
                rel = line[3:].strip().strip('"')
                if _is_artifact(rel):
                    continue
                subprocess.run(
                    ["git", "add", "--", rel],
                    capture_output=True, cwd=repo, timeout=30,
                )

            # Verify something is actually staged before committing so a
            # worker that only produced artifact noise does not create an
            # empty commit.
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True, text=True, cwd=repo, timeout=15,
            )
            if not staged.stdout.strip():
                return

            # Guard: refuse to auto-commit staged files that still contain git
            # conflict markers (<<<<<<< / ======= / >>>>>>>). These leak in when
            # concurrent workers clobber the same file (see t_d53f8ef7). The
            # commit below uses --no-verify, so a git pre-commit hook would NOT
            # fire — the guard therefore must live in this pipeline, and it blocks
            # hard (no commit, ERROR logged with file + task) rather than
            # silently skipping.
            staged_files = [f for f in staged.stdout.splitlines() if f.strip()]
            blocked: list[str] = []
            for rel in staged_files:
                text = _read_staged_text(repo, rel)
                if text is None:
                    continue
                if _conflict_marker_findings(text):
                    blocked.append(rel)
            if blocked:
                logger.error(
                    "Auto-commit BLOCKED for task %s: git conflict markers "
                    "found in staged file(s): %s. Resolve the conflicts and "
                    "re-stage before the auto-commit can proceed.",
                    task_id, ", ".join(blocked),
                )
                return

            msg = f"auto: kanban task {task_id} completed"
            subprocess.run(
                ["git", "commit", "--no-verify", "-m", msg],
                capture_output=True, cwd=repo, timeout=30,
            )
            logger.info("Auto-committed changes for task %s in %s", task_id, repo)
        finally:
            conn.close()
    except Exception:
        logger.debug(
            "auto-commit failed for task %s (board=%s)", task_id, board, exc_info=True,
        )


def register(ctx: Any) -> None:
    """Register the kanban_task_completed hook."""

    def on_kanban_complete(**kwargs: Any) -> None:
        task_id = kwargs.get("task_id")
        board = kwargs.get("board")
        if task_id:
            _auto_commit(task_id, board)

    ctx.register_hook("kanban_task_completed", on_kanban_complete)
