"""kanban-worktree-rebase plugin.

Why this exists
---------------
Cards run in a linked worktree (``<repo>/.worktrees/<task-id>``, branch
``wt/<task-id>``) cut from the base branch at CLAIM time. A card that runs for a
while finishes on a branch dozens of commits behind. ``scripts/land_worktrees.py``
is deliberately ff-only and refuses to force anything, so such a card is reported
DIVERGED and its work strands — by 2026-10-18 that was 40 branches and ~9k lines
sitting in no long-lived branch while the board said "done".

Rebasing the card's OWN branch onto the base tip at completion makes it a
fast-forward again, so the next lander pass carries it forward without any
force-merge.

Guarantees
----------
* Never merges anything into the base branch.
* Never blocks or re-opens a card: conflicts are reported in a card comment and
  the branch is left exactly as the worker committed it (``rebase --abort``).
* Never touches a branch that is already a descendant of the base, an orphan
  branch with no managed worktree, or a worktree with uncommitted changes.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

_REGISTRY_IGNORE = ("_known_issues.json",)


def _git(repo: str, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_EDITOR", "true")
    env.setdefault("GIT_SEQUENCE_EDITOR", "true")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _rev(repo: str, ref: str) -> str:
    r = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return r.stdout.strip() if r.returncode == 0 else ""


def _current_branch(repo: str) -> str:
    r = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    name = r.stdout.strip()
    return "" if r.returncode != 0 or name == "HEAD" else name


def _is_ancestor(repo: str, maybe_ancestor: str, descendant: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", maybe_ancestor, descendant).returncode == 0


def _worktree_for_branch(repo: str, branch: str) -> Optional[str]:
    """Return the worktree path that has ``branch`` checked out, or None."""
    r = _git(repo, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return None
    path = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.strip() == f"branch refs/heads/{branch}" and path:
            return path
    return None


def _resolve_repo(workspace_path: Optional[str]) -> Optional[str]:
    """Accept either ``<repo>`` or ``<repo>/.worktrees/<task-id>``."""
    if not workspace_path:
        return None
    p = os.path.abspath(workspace_path)
    if os.path.basename(os.path.dirname(p)) == ".worktrees":
        p = os.path.dirname(os.path.dirname(p))
    if not os.path.isdir(p):
        return None
    if _git(p, "rev-parse", "--git-dir").returncode != 0:
        return None
    return p


def rebase_completed_worktree(
    repo: str,
    task_id: str,
    base_branch: Optional[str] = None,
) -> dict:
    """Rebase ``wt/<task_id>`` onto the base branch tip. Pure git, no DB access.

    Returns ``{"status": ..., "detail": ...}`` with status one of
    ``rebased`` / ``conflict`` / ``skipped`` / ``error``.
    """
    branch = f"wt/{task_id}"
    if not _rev(repo, branch):
        return {"status": "skipped", "detail": f"no branch {branch}"}

    base = base_branch or _current_branch(repo)
    if not base:
        return {"status": "skipped", "detail": "base checkout is on a detached HEAD"}
    if base == branch:
        return {"status": "skipped", "detail": "base branch is the card branch"}
    base_sha = _rev(repo, base)
    if not base_sha:
        return {"status": "skipped", "detail": f"cannot resolve base {base}"}

    if _is_ancestor(repo, base_sha, branch):
        return {"status": "skipped", "detail": f"{branch} already contains {base}"}

    worktree = _worktree_for_branch(repo, branch)
    if not worktree:
        return {"status": "skipped", "detail": f"{branch} has no managed worktree"}

    dirty = _git(worktree, "status", "--porcelain").stdout.strip()
    if dirty:
        return {"status": "skipped", "detail": f"worktree has uncommitted changes ({worktree})"}

    ahead_before = len(_git(repo, "rev-list", f"{base_sha}..{branch}").stdout.split())
    before_tip = _rev(repo, branch)

    r = _git(worktree, "rebase", base_sha)
    if r.returncode == 0:
        after_tip = _rev(repo, branch)
        ahead_after = len(_git(repo, "rev-list", f"{base_sha}..{branch}").stdout.split())
        return {
            "status": "rebased",
            "detail": (
                f"{branch}: {ahead_before} commit(s) replayed onto {base}@{base_sha[:9]} "
                f"({before_tip[:9]} -> {after_tip[:9]}, {ahead_after} ahead); now ff-landable"
            ),
        }

    conflicted = [
        line.strip() for line in
        _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
        if line.strip()
    ]
    _git(worktree, "rebase", "--abort")
    # Never leave a rebase in progress: abort restores the pre-rebase branch.
    restored = _rev(repo, branch) == before_tip
    return {
        "status": "conflict",
        "detail": (
            f"{branch} could not rebase onto {base}@{base_sha[:9]} "
            f"(conflicts: {', '.join(conflicted) or 'unknown'}"
            f"{'' if restored else '; WARNING: branch not restored to ' + before_tip[:9]}). "
            f"Branch left as the worker committed it — resolve by hand."
        ),
    }


def _task_workspace_path(task_id: str, board: str) -> Optional[str]:
    try:
        from hermes_cli.kanban_db import get_task
        from hermes_cli.kanban_db_connect import connect

        conn = connect(board=board or None)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
        return getattr(row, "workspace_path", None) if row is not None else None
    except Exception:
        return None


def _task_workspace_kind(task_id: str, board: str) -> Optional[str]:
    try:
        from hermes_cli.kanban_db import get_task
        from hermes_cli.kanban_db_connect import connect

        conn = connect(board=board or None)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
        return getattr(row, "workspace_kind", None) if row is not None else None
    except Exception:
        return None


def _comment_task(task_id: str, board: str, body: str, author: str = "worktree-rebase") -> None:
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
    """Rebase the completing card's worktree branch so the lander can carry it."""
    try:
        if (_task_workspace_kind(task_id, board) or "") != "worktree":
            return
        repo = _resolve_repo(workspace_path) or _resolve_repo(_task_workspace_path(task_id, board))
        if repo is None:
            return
        result = rebase_completed_worktree(repo, task_id)
        if result["status"] in ("rebased", "conflict"):
            _comment_task(task_id, board, f"worktree-rebase: {result['detail']}")
    except Exception:
        # A rebase must never break completion. Silence is a bug, so the
        # exception is reported on the card rather than swallowed entirely.
        try:
            _comment_task(
                task_id, board,
                "worktree-rebase: internal error while preparing the branch for landing; "
                "the branch is untouched. Run scripts/land_worktrees.py --dry-run to inspect.",
            )
        except Exception:
            pass


def register(ctx: Any) -> None:
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
