"""kanban-auto-commit plugin.

Auto-commit uncommitted changes in dir: workspace git repos after
kanban task completion. Fires via the kanban_task_completed hook
which runs in the worker process after kanban_complete() succeeds.

Survives hermes-agent updates because user plugins live under
~/.hermes/plugins/, not in the agent checkout.

Two behaviours were fixed after critique t_4b7e63b0 (report
audits/t_4b7e63b0_critique_dispatch.md):

1. Spec citation. The commit message now carries the completing task's
   registry/spec ID (``I{NNN}``) when one can be resolved, e.g.
   ``auto: kanban task t_x completed (I140)``. This keeps auto-commits
   from tripping the realm-forge hard spec-citation gate
   (``project-quality-gates/scripts/check_spec_citation.py``,
   ``REALM_FORGE_GATE_SPEC_WARN_ONLY=0``). The ID is resolved from, in
   order: ``KANBAN_AUTO_COMMIT_SPEC_ID`` env override; the completion
   summary; the task title/body; the latest run's metadata; the task's
   comments. When the staged diff touches ``src/`` and no ID is known the
   auto-commit is **blocked** by default (changes are left uncommitted and
   an ERROR is logged) rather than creating a commit the gate would reject.
   Set ``KANBAN_AUTO_COMMIT_SPEC_ID_POLICY=commit`` to opt into committing
   without an ID anyway (explicit, logged, and gate-visible).

2. Staging scope. Staging is scoped to the completing task's own declared
   files (``changed_files`` in its run metadata, or
   ``KANBAN_AUTO_COMMIT_FILES``), so a finishing worker no longer sweeps a
   concurrent worker's untracked files (observed in commit bfd1f45, which
   mis-attributed ~40 sibling guard files to t_2372ed64). When no declared
   file list is available it falls back to tracked modifications only
   (``git add -u``) and never sweeps untracked files.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

# Spec/registry ID citation: exactly ``I`` followed by three digits
# (I140, I153, I479 — SPEC.md §3.15). Mirrors check_spec_citation.py.
SPEC_ID_PATTERN = re.compile(r"\bI\d{3}\b")
SPEC_ID_EXACT = re.compile(r"^I\d{3}$")

# Paths whose staged changes make a commit a "coding commit" for the
# spec-citation gate.
CODE_PREFIX = "src/"


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


# --------------------------------------------------------------------------- #
# task metadata resolution (spec ID + declared file scope)
# --------------------------------------------------------------------------- #

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if ``name`` is a table in this DB (older/minimal DBs may lack it)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _extract_spec_id(*texts: Any) -> str | None:
    """Return the first ``I{NNN}`` token found across ``texts``, else None."""
    for text in texts:
        if not text:
            continue
        match = SPEC_ID_PATTERN.search(str(text))
        if match:
            return match.group(0)
    return None


def _resolve_spec_id(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int | None = None,
    summary: str | None = None,
) -> str | None:
    """Resolve the spec/registry ID to cite in the auto-commit message.

    Sources, in priority order:

    1. ``KANBAN_AUTO_COMMIT_SPEC_ID`` env override (validated as I{NNN});
    2. the completion ``summary`` handed to the hook;
    3. the task's title + body;
    4. the latest run's ``summary`` / ``metadata``;
    5. the task's comments (newest first).

    Returns ``None`` when no ID is known — the caller decides the fallback
    policy (it never invents an ID).
    """
    override = os.environ.get("KANBAN_AUTO_COMMIT_SPEC_ID", "").strip()
    if override:
        if SPEC_ID_EXACT.match(override):
            return override
        match = SPEC_ID_PATTERN.search(override)
        if match:
            return match.group(0)

    found = _extract_spec_id(summary)
    if found:
        return found

    try:
        row = conn.execute(
            "SELECT title, body FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        found = _extract_spec_id(row["title"], row["body"])
        if found:
            return found

    if _table_exists(conn, "task_runs"):
        try:
            if run_id is not None:
                run = conn.execute(
                    "SELECT summary, metadata FROM task_runs WHERE id = ?", (run_id,)
                ).fetchone()
            else:
                run = None
            if run is None:
                run = conn.execute(
                    "SELECT summary, metadata FROM task_runs WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error:
            run = None
        if run is not None:
            found = _extract_spec_id(run["summary"], run["metadata"])
            if found:
                return found

    if _table_exists(conn, "task_comments"):
        try:
            for comment in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? "
                "ORDER BY id DESC",
                (task_id,),
            ):
                found = _extract_spec_id(comment["body"])
                if found:
                    return found
        except sqlite3.Error:
            pass

    return None


def _resolve_declared_files(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int | None = None,
) -> list[str] | None:
    """Return the completing task's declared changed files, or ``None``.

    Sources, in priority order:

    1. ``KANBAN_AUTO_COMMIT_FILES`` env override (newline/comma separated);
    2. the run's ``metadata.changed_files`` (the canonical worker-declared
       list written by kanban_complete).

    ``None`` means "no declaration available" and triggers the conservative
    tracked-only fallback in the stager.
    """
    override = os.environ.get("KANBAN_AUTO_COMMIT_FILES", "").strip()
    if override:
        parts = [p.strip() for p in re.split(r"[\n,]+", override)]
        return [p for p in parts if p]

    if not _table_exists(conn, "task_runs"):
        return None

    run = None
    try:
        if run_id is not None:
            run = conn.execute(
                "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if run is None:
            run = conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
    except sqlite3.Error:
        run = None
    if run is None or not run["metadata"]:
        return None

    try:
        meta = json.loads(run["metadata"])
    except (ValueError, TypeError):
        return None
    changed = meta.get("changed_files") if isinstance(meta, dict) else None
    if not isinstance(changed, list):
        return None
    files = [str(f).strip() for f in changed if str(f).strip()]
    return files or None


def _normalize_repo_path(repo: str, rel: str) -> str | None:
    """Normalize a declared path to a repo-relative POSIX path.

    Returns ``None`` for empty values, the repo root itself, or anything
    that escapes the repo (``..`` traversal / absolute path outside).
    """
    value = str(rel or "").strip().replace("\\", "/")
    if not value:
        return None
    if os.path.isabs(value):
        try:
            value = os.path.relpath(value, repo)
        except ValueError:
            return None
    value = os.path.normpath(value).replace("\\", "/")
    if value in (".", ""):
        return None
    if value == ".." or value.startswith("../"):
        return None
    if os.path.isabs(value):
        return None
    return value


def _tracked_changed_paths(repo: str) -> list[str] | None:
    """Return tracked files modified/deleted vs HEAD (never untracked adds).

    Used for the no-declaration fallback so a finishing worker still cannot
    sweep a sibling's untracked (or index-staged-but-new) files. Returns
    ``None`` when HEAD cannot be resolved (e.g. an empty repo), in which case
    the caller uses the legacy ``git add -u`` path.
    """
    head = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, cwd=repo, timeout=15,
    )
    if head.returncode != 0:
        return None
    head_files = {line for line in head.stdout.splitlines() if line}
    diff = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True, text=True, cwd=repo, timeout=15,
    )
    if diff.returncode != 0:
        return None
    # Intersect with the HEAD tree: keeps modifications + deletions of
    # already-tracked files and drops newly-added (staged or not) files.
    return [f for f in diff.stdout.splitlines() if f in head_files]


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


def _auto_commit(
    task_id: str,
    board: str | None,
    run_id: int | None = None,
    summary: str | None = None,
) -> None:
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

            # Scope staging to the completing task's own declared files so a
            # finishing worker never sweeps a concurrent worker's untracked
            # files (critique t_4b7e63b0 defect C / commit bfd1f45).
            declared = _resolve_declared_files(conn, task_id, run_id)
            scoped_paths: list[str] | None = None
            if declared is not None:
                scoped_paths = []
                for rel in declared:
                    normalized = _normalize_repo_path(repo, rel)
                    if normalized and normalized not in scoped_paths:
                        scoped_paths.append(normalized)
                if not scoped_paths:
                    return
                for rel in scoped_paths:
                    subprocess.run(
                        ["git", "add", "-A", "--", rel],
                        capture_output=True, cwd=repo, timeout=30,
                    )
            else:
                # No declaration available: restrict to tracked modifications
                # and deletions of files already in HEAD — never untracked
                # files, and never a sibling's index-staged additions. This is
                # strictly narrower than the old "stage every untracked
                # non-artifact file" sweep.
                scoped_paths = _tracked_changed_paths(repo)
                if scoped_paths is None:
                    subprocess.run(
                        ["git", "add", "-u"],
                        capture_output=True, cwd=repo, timeout=30,
                    )
                elif not scoped_paths:
                    return
                else:
                    for rel in scoped_paths:
                        subprocess.run(
                            ["git", "add", "-A", "--", rel],
                            capture_output=True, cwd=repo, timeout=30,
                        )

            # Only the files this task will actually commit — never the whole
            # index, which may carry a sibling worker's staged entries.
            diff_args = ["git", "diff", "--cached", "--name-only"]
            if scoped_paths is not None:
                diff_args += ["--", *scoped_paths]
            staged = subprocess.run(
                diff_args, capture_output=True, text=True, cwd=repo, timeout=15,
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

            # Resolve the spec/registry ID so a src/-touching auto-commit
            # satisfies the hard spec-citation gate instead of reddening main.
            spec_id = _resolve_spec_id(conn, task_id, run_id, summary)
            touches_src = any(f.startswith(CODE_PREFIX) for f in staged_files)

            msg = f"auto: kanban task {task_id} completed"
            if spec_id:
                msg = f"{msg} ({spec_id})"
            elif touches_src:
                policy = os.environ.get(
                    "KANBAN_AUTO_COMMIT_SPEC_ID_POLICY", "block"
                ).strip().lower()
                if policy != "commit":
                    logger.error(
                        "Auto-commit BLOCKED for task %s: staged changes touch "
                        "src/ but no spec ID (I{NNN}) is known for this task. "
                        "Cite an ID in the task card/comment/metadata or set "
                        "KANBAN_AUTO_COMMIT_SPEC_ID; changes are left "
                        "uncommitted. Set KANBAN_AUTO_COMMIT_SPEC_ID_POLICY="
                        "commit to override (new commit will be flagged by "
                        "the spec-citation gate).",
                        task_id,
                    )
                    return
                logger.warning(
                    "Auto-committing src/ changes for task %s WITHOUT a spec "
                    "ID (KANBAN_AUTO_COMMIT_SPEC_ID_POLICY=commit). The "
                    "spec-citation gate will flag this commit.",
                    task_id,
                )

            commit_cmd = ["git", "commit", "--no-verify", "-m", msg]
            if scoped_paths is not None:
                # Pathspec commit: record only the task's own files, ignoring
                # any other staged entries in the shared index.
                commit_cmd += ["--", *scoped_paths]
            subprocess.run(
                commit_cmd, capture_output=True, cwd=repo, timeout=30,
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
        run_id = kwargs.get("run_id")
        summary = kwargs.get("summary")
        if task_id:
            _auto_commit(task_id, board, run_id, summary)

    ctx.register_hook("kanban_task_completed", on_kanban_complete)
