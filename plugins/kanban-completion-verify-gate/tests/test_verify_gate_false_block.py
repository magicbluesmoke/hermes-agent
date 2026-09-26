"""Regression test: verify-gate false-block on a buried own-commit.

Bug (2026-09-10, 15 realm-forge cards): ``_git_changed_files()`` returned the
working tree union exactly ONE commit (``HEAD~1..HEAD``). On a busy board a
sibling card's auto-commit lands on top of the completing card's own commit
within minutes, so the claimed ``FILE:`` path rolled out of that window and the
claimed-file guard re-blocked a finished, green card with::

    verify_gate: claimed source file(s) not modified by this completion

The fix (t_acc22718) widened the evidence to every commit belonging to the task
— message contains the task id, or committed at/after the task's claim epoch.

This test builds an isolated temp git repo, buries the task's own commit under
two unrelated commits, and asserts the claimed-file guard still passes.

RED/GREEN control: set ``VERIFY_GATE_PLUGIN_PATH`` to a pre-fix ``__init__.py``
(e.g. ``__init__.py.bak_20260909_190453_repo-resolve-fix``) and
``test_claimed_file_guard_passes_with_two_unrelated_commits_on_top`` fails,
because the pre-fix helper only takes ``repo_root`` and its one-commit window
misses the buried file. Against the current implementation all tests pass.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Overridable so the same test can run against the pre-fix implementation as a
# negative control (see module docstring).
PLUGIN_INIT = Path(
    os.environ.get(
        "VERIFY_GATE_PLUGIN_PATH",
        Path(__file__).resolve().parents[1] / "__init__.py",
    )
)
_MODULE_NAME = "kanban_completion_verify_gate_under_test"

TASK_ID = "t_regress01"
TASK_FILE = "src/claimed_module.py"
UNTOUCHED_FILE = "src/pre_existing_module.py"

BODY = f"FILE: {TASK_FILE}\nVerify: {TASK_FILE.replace('.py', '_test.py')}\n"


def _load_plugin():
    # SourceFileLoader (not spec_from_file_location) so a pre-fix backup with a
    # non-.py suffix also loads — spec_from_file_location returns None there.
    loader = importlib.machinery.SourceFileLoader(_MODULE_NAME, str(PLUGIN_INIT))
    spec = importlib.util.spec_from_loader(_MODULE_NAME, loader)
    assert spec is not None and spec.loader is not None, PLUGIN_INIT
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_plugin()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _git(repo, *args, env=None) -> str:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=full_env,
    )
    return proc.stdout or ""


def _commit(repo, rel: str, content: str, message: str, when: int) -> int:
    """Commit ``rel`` with a fixed committer/author time; return that epoch."""
    fp = Path(repo) / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    stamp = f"{int(when)} +0000"
    env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message, env=env)
    committed_at = int(_git(repo, "show", "-s", "--format=%ct", "HEAD").strip())
    assert committed_at == int(when), (committed_at, when)
    return committed_at


def _init_repo(tmp_path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "Verify Gate Test")
    return repo


def _changed_files_for_task(repo, task_id=None, since_epoch=None) -> list[str]:
    """Call the gate's evidence helper with whatever signature is present.

    Post-fix the helper takes ``(repo_root, task_id, since_epoch)``. The pre-fix
    implementation takes only ``repo_root``; falling back to the bare call keeps
    this test runnable against the old code so it demonstrates the bug (RED)
    instead of erroring on an unexpected keyword argument.
    """
    params = inspect.signature(mod._git_changed_files).parameters
    if "task_id" in params:
        return list(mod._git_changed_files(repo, task_id=task_id, since_epoch=since_epoch))
    return list(mod._git_changed_files(repo))


def _pre_fix_window(repo) -> set[str]:
    """The historical pre-fix evidence set: working tree + exactly ONE commit.

    Computed straight from git (not from plugin helpers) so it is identical
    under both the pre-fix and fixed plugin implementations.
    """
    window: set[str] = set()
    for spec in (
        ["diff", "--name-only", "HEAD"],
        ["diff", "--name-only", "HEAD~1", "HEAD"],
    ):
        proc = subprocess.run(
            ["git", "-C", str(repo), *spec], capture_output=True, text=True,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            s = line.strip().replace("\\", "/")
            if s:
                window.add(s)
    return window


def _guard_missing(repo, body: str, changed) -> list[str]:
    """Mirror the claimed-file guard's decision for one completion.

    Same shape as the ``FILE:``/``CHANGE:`` guard block in
    ``on_kanban_task_completed``: a claimed path that exists in the tree must
    appear in the completion's changed-file evidence (exact path or basename),
    otherwise it lands in ``missing`` and the guard blocks the card. Returns the
    missing list — empty means the guard passes (no false block).
    """
    missing: list[str] = []
    norm = {c.replace("\\", "/") for c in changed}
    bases = {c.rsplit("/", 1)[-1] for c in norm}
    for f in mod._claimed_files(body):
        nf = f.replace("\\", "/")
        base = nf.rsplit("/", 1)[-1]
        exists = any(base in files for _root, _dirs, files in os.walk(str(repo)))
        if not exists:
            continue
        if nf in norm or base in bases:
            continue
        missing.append(f)
    return missing


@pytest.fixture()
def buried_repo(tmp_path):
    """Repo whose task commit is buried under two unrelated commits.

    Commit times are pinned seconds apart so the claim-time window is
    deterministic (git's second granularity is too coarse for rapid commits).
    """
    repo = _init_repo(tmp_path, "buried-repo")
    now = int(time.time())
    base_at = now - 400
    task_at = now - 300

    # pre-existing file nothing in this run touches (guard positive control)
    _commit(repo, UNTOUCHED_FILE, "pre = 1\n", "chore: base plumbing", base_at)
    # the task's own commit (carries the task id in its message)
    _commit(repo, TASK_FILE, "x = 1\n", f"{TASK_ID}: add claimed source file", task_at)
    # two unrelated sibling commits layered on top
    _commit(repo, "other/alpha.py", "a = 1\n", "t_sibling_alpha: unrelated commit", now - 200)
    _commit(repo, "other/beta.py", "b = 1\n", "t_sibling_beta: unrelated commit", now - 100)

    return repo, task_at


# --------------------------------------------------------------------------- #
# regression
# --------------------------------------------------------------------------- #

def test_claimed_file_guard_passes_with_two_unrelated_commits_on_top(buried_repo):
    """The buried claimed file is still evidence -> the guard does not block."""
    repo, task_at = buried_repo

    changed = _changed_files_for_task(repo, task_id=TASK_ID, since_epoch=task_at - 5)

    assert TASK_FILE in changed, (
        "claimed file missing from the completion evidence; the gate would "
        f"false-block the card. evidence={sorted(changed)}"
    )
    assert UNTOUCHED_FILE not in changed, sorted(changed)
    assert _guard_missing(repo, BODY, changed) == [], (
        "claimed-file guard still reports a missing file (false block)"
    )


def test_claim_epoch_alone_covers_an_id_less_commit_message(tmp_path):
    """A commit with no task id in its message is found via the claim window."""
    repo = _init_repo(tmp_path, "epoch-repo")
    now = int(time.time())

    _commit(repo, UNTOUCHED_FILE, "pre = 1\n", "chore: base plumbing", now - 400)
    claim_epoch = now - 300
    _commit(repo, TASK_FILE, "x = 1\n", "fix: add the claimed source file", claim_epoch)
    _commit(repo, "other/alpha.py", "a = 1\n", "fix: unrelated one", now - 200)
    _commit(repo, "other/beta.py", "b = 1\n", "fix: unrelated two", now - 100)

    changed = _changed_files_for_task(repo, task_id=TASK_ID, since_epoch=claim_epoch)

    assert TASK_FILE in changed, sorted(changed)
    assert _guard_missing(repo, BODY, changed) == []


def test_pre_fix_window_would_have_false_blocked(buried_repo):
    """Non-vacuity control: the old one-commit window really misses the file.

    If this ever stops failing, the repo fixture no longer reproduces the bug
    and the regression test above has become meaningless.
    """
    repo, _task_at = buried_repo

    window = _pre_fix_window(repo)

    assert TASK_FILE not in window, (
        "fixture no longer buries the claimed commit — the regression test is "
        f"vacuous. window={sorted(window)}"
    )
    assert _guard_missing(repo, BODY, window) == [TASK_FILE]


def test_guard_still_blocks_a_claimed_file_nothing_touched(buried_repo):
    """Widening the window must not remove the guard's teeth."""
    repo, _task_at = buried_repo
    body = f"FILE: {UNTOUCHED_FILE}\n"

    # No task id, claim epoch after every commit: nothing in this repo belongs
    # to the task, so a claimed pre-existing file must stay unmatched.
    changed = _changed_files_for_task(repo, task_id=None, since_epoch=int(time.time()))

    assert UNTOUCHED_FILE not in changed, sorted(changed)
    assert _guard_missing(repo, body, changed) == [UNTOUCHED_FILE]
