"""Regression tests for kanban-worktree-rebase.

The plugin exists because a card branch cut at claim time falls behind while the
card runs, and the ff-only lander then refuses it (40 branches / ~9k lines
stranded on 2026-10-18). These tests pin the three decisions that matter:

* a diverged branch becomes fast-forwardable, with its commit content intact;
* a CONTENT conflict aborts and leaves the branch exactly as committed;
* nothing happens to a branch that is already current, dirty, or orphaned.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_INIT = Path(__file__).resolve().parents[1] / "__init__.py"
_MODULE_NAME = "kanban_worktree_rebase_under_test"


def _load():
    loader = importlib.machinery.SourceFileLoader(_MODULE_NAME, str(PLUGIN_INIT))
    spec = importlib.util.spec_from_loader(_MODULE_NAME, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _git(repo, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def _init_repo(tmp_path, name="repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "rebase@example.invalid")
    _git(repo, "config", "user.name", "Rebase Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worktree_card(repo, task_id="t_test01") -> Path:
    wt = repo / ".worktrees" / task_id
    _git(repo, "worktree", "add", "-q", "-b", f"wt/{task_id}", str(wt))
    return wt


def _advance_base(repo, name="later.txt", content="later\n") -> None:
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"advance {name}")


def test_diverged_branch_becomes_ff_landable(tmp_path):
    repo = _init_repo(tmp_path)
    wt = _worktree_card(repo)
    (wt / "card.txt").write_text("card work\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "fix: card work")
    _advance_base(repo)

    # Precondition: the branch is NOT a descendant of the base -> ff-only lander refuses.
    assert mod._git(repo, "merge-base", "--is-ancestor", "main", "wt/t_test01").returncode == 1

    result = mod.rebase_completed_worktree(str(repo), "t_test01")

    assert result["status"] == "rebased", result
    assert mod._git(repo, "merge-base", "--is-ancestor", "main", "wt/t_test01").returncode == 0, (
        "branch is still not fast-forwardable: the lander would report DIVERGED again"
    )
    # Content survives the replay.
    assert (wt / "card.txt").read_text() == "card work\n"
    assert "advance" in _git(repo, "log", "--format=%s", "wt/t_test01")
    assert "card work" in _git(repo, "log", "--format=%s", "wt/t_test01")


def test_content_conflict_aborts_and_restores_the_branch(tmp_path):
    repo = _init_repo(tmp_path)
    wt = _worktree_card(repo)
    (wt / "shared.txt").write_text("card version\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "card edits shared.txt")
    (repo / "shared.txt").write_text("base version\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base edits shared.txt")

    tip_before = mod._rev(str(repo), "wt/t_test01")
    result = mod.rebase_completed_worktree(str(repo), "t_test01")

    assert result["status"] == "conflict", result
    assert "shared.txt" in result["detail"]
    assert mod._rev(str(repo), "wt/t_test01") == tip_before, (
        "a conflicted rebase must leave the branch exactly as the worker committed it"
    )
    # No rebase left in progress in the worktree.
    assert mod._git(wt, "status", "--porcelain").stdout.strip() == ""
    assert (repo / ".git" / "rebase-merge").exists() is False


def test_already_current_branch_is_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    wt = _worktree_card(repo)
    (wt / "card.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "card work")

    result = mod.rebase_completed_worktree(str(repo), "t_test01")

    assert result["status"] == "skipped", result
    assert "already contains" in result["detail"]


def test_dirty_worktree_is_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    wt = _worktree_card(repo)
    (wt / "card.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "card work")
    _advance_base(repo)
    (wt / "scratch.txt").write_text("uncommitted\n")

    result = mod.rebase_completed_worktree(str(repo), "t_test01")

    assert result["status"] == "skipped", result
    assert "uncommitted" in result["detail"]


def test_orphan_branch_without_worktree_is_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    # An orphan branch must still be AHEAD of the base, otherwise the
    # "already contains" check short-circuits before the worktree lookup.
    _git(repo, "checkout", "-q", "-b", "wt/t_orphan")
    (repo / "orphan.txt").write_text("orphan work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan commit")
    _git(repo, "checkout", "-q", "main")
    _advance_base(repo)

    result = mod.rebase_completed_worktree(str(repo), "t_orphan")

    assert result["status"] == "skipped", result
    assert "no managed worktree" in result["detail"]


def test_resolve_repo_accepts_worktree_and_repo_paths(tmp_path):
    repo = _init_repo(tmp_path)
    wt = _worktree_card(repo)

    assert mod._resolve_repo(str(repo)) == str(repo)
    assert mod._resolve_repo(str(wt)) == str(repo)
    assert mod._resolve_repo(str(tmp_path / "nope")) is None
