"""kanban-completion-verify-gate plugin.

Hooks ``kanban_task_completed`` and, for dir-workspace tasks that point at a
git repo, re-runs the task's acceptance tests in the CANONICAL checkout. If
tests fail the card is re-blocked; if they pass an evidence comment is added.

The gate verifies against the canonical checkout (C:/src/realm-forge-game by
default), never the worker's scratch/workspace copy, so a worker's self-report
that doesn't match the tree is caught before the card closes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

# Canonical checkout + its venv python. The historical default is a Windows
# path (C:/src/...) that is unreachable on this Linux host; when the env var
# is absent, fall back to the Linux canonical checkout under $HOME/src and
# the POSIX .venv/bin/python layout so CLI invocations outside the gateway
# (which injects REALM_FORGE_*) still resolve.
_CANONICAL_REPO = os.environ.get("REALM_FORGE_REPO", "C:/src/realm-forge-game")
if not os.path.isdir(_CANONICAL_REPO):
    _CANONICAL_REPO = str(Path.home() / "src" / "realm-forge-game")
_candidate_python = os.path.join(_CANONICAL_REPO, ".venv", "Scripts", "python.exe")
if not os.path.isfile(_candidate_python):
    _candidate_python = os.path.join(_CANONICAL_REPO, ".venv", "bin", "python")
_CANONICAL_PYTHON = os.environ.get("REALM_FORGE_VENV_PYTHON", _candidate_python)


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home as _ghh
    return _ghh()


def _board_db_path(board: Optional[str]) -> Path:
    """Resolve a board name to its kanban.db path.

    Mirrors kanban-auto-commit's resolution (which mirrors the core's
    ``kanban_db_path``) so the plugin works in worker subprocesses
    (profile-scoped HERMES_HOME) and the main agent process alike:

    1. ``HERMES_KANBAN_DB`` env var -- injected by the dispatcher into worker
       envs so the board is immune to path disagreements.
    2. ``HERMES_HOME/kanban/boards/<slug>/kanban.db`` for named boards,
       ``HERMES_HOME/kanban.db`` for the default board.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    home = _hermes_home()
    if board and board.strip() and board.strip().lower() != "default":
        return home / "kanban" / "boards" / board.strip() / "kanban.db"
    return home / "kanban.db"


def _detect_repo_root(workspace_path: Optional[str]) -> Optional[str]:
    """Return the git repo the completion actually changed, else canonical.

    Prefer the workspace's OWN repo (walk up to the enclosing .git) so cards
    on non-realm-forge repos (hermes-agent under ~/.hermes, plugins/, wiki/)
    are verified against the tree they actually committed to. Fall back to the
    canonical Realm Forge checkout only when the workspace is not itself inside
    a git repo (scratch cards on realm-forge work).
    """
    if workspace_path:
        p = os.path.abspath(os.path.expanduser(workspace_path))
        while True:
            if os.path.isdir(os.path.join(p, ".git")):
                return p
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
    if os.path.isdir(os.path.join(_CANONICAL_REPO, ".git")):
        return _CANONICAL_REPO
    return None


def _target_resolves(target: str, repo_root: str) -> bool:
    """True if at least one token of `target` is an existing path/glob in repo_root.

    Prevents running pytest on prose ('next 2 tier2 ticks show...'), template
    placeholders ('test_i<n>_*.py'), or bare shell commands ('python3
    scripts/...') that were picked up from a card body's Verify: line — those
    yield pytest rc=4 'no tests ran' and false-block a completed card.
    """
    import shlex

    try:
        tokens = shlex.split(target)
    except Exception:
        return False
    for tok in tokens:
        if tok.startswith("-"):
            continue
        cand = tok if os.path.isabs(tok) else os.path.join(repo_root, tok)
        if os.path.exists(cand):
            return True
        # glob (may contain * ? [ ])
        try:
            import glob

            if glob.glob(cand):
                return True
        except Exception:
            continue
    return False


def _detect_test_target(task_body: str) -> str:
    patterns = [
        r"pytest\s+(?P<target>[^\s]+)",
        r"run_qc\s+(?P<target>[^\s]+)",
        r"verify:\s*(?P<target>.+)",
    ]
    for pat in patterns:
        m = re.search(pat, task_body, re.IGNORECASE)
        if m:
            target = m.group("target").strip()
            # A token captured from prose is often wrapped in markdown backticks
            # or quotes ("pytest tests/`"). Strip the wrapping so the
            # bare-whole-suite guard below recognises the real token instead of
            # a backtick-suffixed one it fails to match.
            if not pat.startswith("verify"):
                target = target.strip("`'\"").strip()
            # A bare whole-suite target ("tests", "tests/", ".", possibly
            # followed by a status word like "pass"/"passes") means "run
            # everything". Never treat that as a scoped gate target: the board
            # carries many unrelated pre-existing failures that would re-block
            # a green card indefinitely. Fall through so the gate scopes to the
            # card's own regression test / TDAD impact instead.
            core = re.sub(r"\s+(?:pass(?:es|ed)?|all|suite)$", "", target, flags=re.I).strip()
            if re.fullmatch(r"(?:tests/?|\\.)", core):
                continue
            # Verify: lines on meta/IMPROVE cards are often prose acceptance
            # criteria ("Verify: python3 scripts/... emits <1KB", "Verify: next
            # 2 tier2 ticks show rotated roles", "Verify: plugin tests + a
            # low-risk card skips review"). Running pytest on prose yields rc=4
            # 'no tests ran' and false-blocks a completed card. Only honor a
            # Verify: line that LOOKS like a pytest/run_qc invocation or a
            # test-path token; otherwise treat it as no explicit target so the
            # gate falls through to scoped discovery / comment-and-pass.
            if pat.startswith("verify"):
                looks_testy = bool(
                    re.match(r"^(?:pytest|run_qc|\.?/?(?:tests?/|src/)?[\w./-]+\.py)\b", core, re.IGNORECASE)
                ) or bool(re.match(r"^pytest\b", core, re.IGNORECASE))
                if not looks_testy:
                    continue
            return target
    return ""   # NOT "tests": no explicit target means we scope below, never whole-suite


def _impact_affected_tests(task_body: str, repo_root: str) -> list[str]:
    """Return TDAD-style affected test files for the task's changed files.

    Uses the Graphify graph via scripts/test_impact.py. Falls back to [] on
    any failure (graph stale/missing) so the gate never blocks on tooling.
    """
    files = re.findall(r"(?:[Ff][Ii][Ll][Ee])[:：]\s*([\w./]+\.py)", task_body or "")
    # A single FILE: token can introduce several files ('+ '/', '-separated);
    # reuse _claimed_files semantics for .py-only impact discovery.
    if len(files) <= 1:
        claimed_all = _claimed_files(task_body or "")
        files = [f for f in claimed_all if f.endswith(".py")]
    if not files:
        return []
    impact = os.path.join(repo_root, "scripts", "test_impact.py")
    if not os.path.isfile(impact):
        return []
    try:
        proc = subprocess.run(
            [_CANONICAL_PYTHON, impact] + files,
            cwd=repo_root, capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout or ""
        return [l.strip() for l in out.splitlines()
                if l.strip().startswith("tests/") and l.strip().endswith(".py")]
    except Exception:
        return []


def _discover_regression_tests(task_body: str, repo_root: str) -> list[str]:
    """Find the worker's regression test(s) for an issue card.

    Bodies from create_known_issue_tasks.py carry 'Issue: I<n>' with no
    explicit pytest/verify line. Workers add tests/regressions/test_i<n>_*.py
    (or tests/test_i<n>_*.py); discover those by issue id so the gate verifies
    the card's own change instead of the whole suite.
    """
    m = re.search(r"[iI]ssue[:：#]?\s*[iI](\d+)", task_body or "")
    if not m:
        return []
    iid = m.group(1)
    test_root = os.path.join(repo_root, "tests")
    if not os.path.isdir(test_root):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(test_root):
        for f in files:
            # Match on the issue-id BOUNDARY, not a raw prefix: `test_i36_*.py`
            # or `test_i36.py` belong to issue 36; `test_i360_*.py` /
            # `test_i369_*.py` are separate 3-digit issues and must NOT be
            # swept in (observed 2026-09-04: I35 gate globbed I357, I36 globbed
            # I360-369, I56 globbed I560-569, I57 globbed I577, I59 globbed
            # I596 — 7 cards re-blocked on out-of-scope failures).
            if f.endswith(".py") and (
                f == f"test_i{iid}.py" or f.startswith(f"test_i{iid}_")
            ):
                rel = os.path.relpath(os.path.join(dirpath, f), repo_root)
                out.append(rel.replace("\\", "/"))
    return out


def _claimed_files(task_body: str) -> list[str]:
    """Extract FILE:/File: paths the card body claims the worker changed.

    Same regex family as _impact_affected_tests but accepts ANY file extension
    (.py, .json, data files): a FILE:/CHANGE: card must actually modify the
    named file(s), or the completion is a test-only/empty commit. Handles
    multi-file bodies where a single FILE: token is followed by '+' / ','
    separated paths.
    """
    out: list[str] = []
    for m in re.finditer(r"(?:[Ff][Ii][Ll][Ee])[:：]?\s*([\w./]+\.[a-zA-Z0-9]+)", task_body or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
        # Continuation: other bare paths may follow the first token on the SAME
        # line (e.g. 'FILE: a.py:310 (...) + b.py:258-266 (...)',
        # 'File: x.json (tag), y.py handler'). Bound to the same line so prose
        # mentions of test files on later lines (e.g. a Verify: pytest line)
        # are never misread as claimed source changes.
        rest = task_body[m.end():]
        cut = re.split(r"\n", rest, maxsplit=1)[0]
        cut = re.split(r"CHANGE:|Change:|Verify:", cut, maxsplit=1)[0]
        for c in re.findall(r"[\w./]+\.[a-zA-Z0-9]+", cut):
            if c not in out:
                out.append(c)
    return out


def _shellcheck_claimed(claimed: list[str], repo_root: str) -> tuple[list[str], str]:
    """Run shellcheck on claimed .sh files; return (problem_files, combined_output).

    Any card whose FILE: list includes a shell script gets it linted with
    `shellcheck -S warning` against the canonical checkout. Warning+ findings
    block the card (the same bar as the realm-forge pre-commit hook); info
    style nits are surfaced in the evidence comment but do not block. Missing
    shellcheck binary or missing files are treated as pass (no false block).
    """
    sh_claimed = [c for c in claimed if c.endswith(".sh")]
    if not sh_claimed:
        return [], ""
    import shutil

    if shutil.which("shellcheck") is None:
        return [], ""
    problems: list[str] = []
    output_parts: list[str] = []
    for rel in sh_claimed:
        nf = rel.replace("\\", "/")
        cand = nf if os.path.isabs(nf) else os.path.join(repo_root, nf)
        if not os.path.isfile(cand):
            # Claimed .sh that does not exist in the tree: let the existing
            # claimed-file guard handle it, not this linter.
            continue
        try:
            proc = subprocess.run(
                ["shellcheck", "-S", "warning", cand],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:
            output_parts.append(f"{nf}: shellcheck error: {exc}")
            continue
        if proc.returncode != 0:
            problems.append(nf)
            output_parts.append(proc.stdout or "")
        else:
            output_parts.append(f"{nf}: shellcheck clean (warning+ severity)")
    return problems, "\n".join(output_parts)


def _ruff_claimed(claimed: list[str], repo_root: str) -> tuple[list[str], str]:
    """Run ruff on claimed .py files; return (problem_files, combined_output).

    Any card whose FILE: list includes a Python file gets it linted with
    `ruff check` (bug-only rules F/E4/E7 per the repo pyproject.toml — the
    same bar as the realm-forge pre-commit hook). Findings block the card.
    Missing ruff binary or missing files are treated as pass (no false block).
    """
    py_claimed = [c for c in claimed if c.endswith(".py")]
    if not py_claimed:
        return [], ""
    import shutil

    if shutil.which("ruff") is None:
        return [], ""
    problems: list[str] = []
    output_parts: list[str] = []
    for rel in py_claimed:
        nf = rel.replace("\\", "/")
        cand = nf if os.path.isabs(nf) else os.path.join(repo_root, nf)
        if not os.path.isfile(cand):
            # Claimed .py that does not exist in the tree: let the existing
            # claimed-file guard handle it, not this linter.
            continue
        try:
            proc = subprocess.run(
                ["ruff", "check", cand],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:
            output_parts.append(f"{nf}: ruff error: {exc}")
            continue
        if proc.returncode != 0:
            problems.append(nf)
            output_parts.append(proc.stdout or "")
        else:
            output_parts.append(f"{nf}: ruff clean (F/E4/E7)")
    return problems, "\n".join(output_parts)


def _git_raw(repo_root: str, args: list[str], timeout: int = 30) -> str:
    """Run git in ``repo_root`` and return stdout ('' on any failure)."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.stdout or ""
    except Exception:
        return ""


def _git_path_lines(repo_root: str, args: list[str]) -> list[str]:
    """Run git and return trimmed, slash-normalised output lines."""
    return [
        ln.strip().replace("\\", "/")
        for ln in _git_raw(repo_root, args).splitlines()
        if ln.strip()
    ]


# Record separator (start of a commit block) and field separator inside the
# header, so the message can contain newlines without breaking the parse.
_LOG_BLOCK = "\x01"
_LOG_END = "\x02"
_LOG_FIELD = "\x1f"
# How far back the task-scoped commit scan looks. Generous enough for a busy
# board (hundreds of sibling auto-commits) while keeping the log call cheap.
_TASK_COMMIT_SCAN_LIMIT = 400


def _task_commit_changes(
    repo_root: str,
    task_id: Optional[str],
    since_epoch: Optional[int],
) -> list[str]:
    """Files touched by commits that belong to THIS task.

    A commit belongs to the task when its message contains ``task_id`` or when
    it was committed at/after the task was claimed (``since_epoch``). Uses one
    ``git log --name-only`` call rather than a subprocess per commit.
    """
    if not task_id and not since_epoch:
        return []
    fmt = f"{_LOG_BLOCK}%H{_LOG_FIELD}%ct{_LOG_FIELD}%B{_LOG_END}"
    raw = _git_raw(
        repo_root,
        ["log", f"--max-count={_TASK_COMMIT_SCAN_LIMIT}", "--no-color", "-m", "--name-only", f"--format={fmt}"],
    )
    out: list[str] = []
    for block in raw.split(_LOG_BLOCK)[1:]:
        header, _, body = block.partition(_LOG_END)
        fields = header.split(_LOG_FIELD, 2)
        if len(fields) < 2:
            continue
        try:
            committed_at = int(fields[1].strip())
        except (TypeError, ValueError):
            committed_at = 0
        message = fields[2] if len(fields) > 2 else ""
        relevant = bool(task_id and task_id in message) or bool(
            since_epoch and committed_at and committed_at >= int(since_epoch)
        )
        if not relevant:
            continue
        for line in body.splitlines():
            s = line.strip().replace("\\", "/")
            if s and s.endswith(".py") and s not in out:
                out.append(s)
    return out


def _task_claim_epoch(task_id: Optional[str], board: str) -> Optional[int]:
    """Epoch seconds when ``task_id`` was claimed, read from the board DB.

    Uses the EARLIEST ``claimed`` event for the task rather than
    ``tasks.started_at`` alone: a re-claim (crash retry) pushes ``started_at``
    forward past the card's own earlier commit, which would re-open the very
    false-block window this widened evidence set exists to close. Best effort —
    None when the board cannot be read, in which case the caller still matches
    on the task id appearing in the commit message.
    """
    if not task_id:
        return None
    try:
        conn = _connect(board or None)
    except Exception:
        return None
    try:
        epoch: Optional[int] = None
        try:
            row = conn.execute(
                "SELECT MIN(created_at) FROM task_events "
                "WHERE task_id = ? AND kind = 'claimed'",
                (task_id,),
            ).fetchone()
            if row and row[0]:
                epoch = int(row[0])
        except Exception:
            pass
        try:
            row = conn.execute(
                "SELECT started_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and row[0]:
                started = int(row[0])
                if epoch is None or started < epoch:
                    epoch = started
        except Exception:
            pass
        return epoch
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _git_changed_files(
    repo_root: str,
    task_id: Optional[str] = None,
    since_epoch: Optional[int] = None,
) -> list[str]:
    """Return .py files changed by the completion, scoped to the task's own work.

    Evidence is the union of:

    * the working tree (``git diff --name-only HEAD``) — the uncommitted case,
      retained verbatim from the original implementation;
    * every commit relevant to this task — its message contains ``task_id``,
      or it was committed at/after the task was claimed (``since_epoch``);
    * the immediately preceding commit (``HEAD~1..HEAD``) as the historical
      floor, so a caller that passes no task context (or a task whose commit
      predates the claim timestamp) keeps the old behaviour.

    The original implementation checked the working tree plus exactly ONE
    commit (``HEAD~1..HEAD``). On a busy board sibling cards' auto-commits
    land on top of the completing card's own commit within minutes, so the
    claimed FILE: path rolled out of that one-commit window and the gate
    re-blocked a finished, green card with "claimed source file(s) not
    modified by this completion" (2026-09-10: 16 realm-forge cards; e.g.
    t_1c7075bf's commit 28670e5 was buried by sibling auto-commits).
    Widening to the task's own commits fixes that without loosening the
    guard: an unrelated sibling commit still cannot satisfy a claimed path
    unless it also happens to touch that same file.
    """
    out: list[str] = []

    def _add(paths: list[str]) -> None:
        for spec in paths:
            if spec.endswith(".py") and spec not in out:
                out.append(spec)

    _add(_git_path_lines(repo_root, ["diff", "--name-only", "HEAD"]))
    _add(_git_path_lines(repo_root, ["diff", "--name-only", "HEAD~1", "HEAD"]))
    _add(_task_commit_changes(repo_root, task_id, since_epoch))
    return out


def _changed_test_files(repo_root: str) -> list[str]:
    """Tests the worker actually touched this run: uncommitted tree + last 4 commits.

    More specific than issue-id prefix discovery — scopes to the worker's own
    added/modified test files, so a same-id collision (two distinct issues
    both numbered I79, observed 2026-09-04) can't sweep in the other issue's
    failing test. Window HEAD~4..HEAD covers multi-commit workers (registry
    commit + regression-test commit + auto-commit).
    """
    out: list[str] = []
    for spec in (
        ["diff", "--name-only", "HEAD"],
        ["diff", "--name-only", "HEAD~4", "HEAD"],
    ):
        try:
            proc = subprocess.run(
                ["git", "-C", repo_root] + spec,
                capture_output=True, text=True, timeout=30,
            )
            for line in (proc.stdout or "").splitlines():
                s = line.strip().replace("\\", "/")
                # conftest.py/__init__.py are pytest plumbing, not collectible
                # test modules. Running pytest on them collects 0 items and
                # returns rc=5, which the gate then reports as a failure and
                # false-blocks a completed card (observed t_d29d056d 2026-09-09:
                # an auto-commit swept a parallel card's conftest.py into the
                # worker's commit, making it the derived target).
                if os.path.basename(s) in ("conftest.py", "__init__.py"):
                    continue
                if (
                    s.startswith("tests/")
                    and s.endswith(".py")
                    and s not in out
                ):
                    out.append(s)
        except Exception:
            continue
    return out


def _summary_test_targets(summary: Optional[str]) -> list[str]:
    """Extract explicit pytest targets / test-file paths from a completion summary.

    Workers routinely name their verification in the completion summary (e.g.
    'pytest tests/regressions/test_i87_...py PASSES'). When the card body has
    no scoped target, re-run exactly what the worker claims instead of giving
    up. Only tokens on a line that actually invokes ``pytest`` count: a bare
    prose mention of another test's path ("the unrelated tests/foo.py stays
    red") must NOT be swept into the target (I79 2026-09-04 — 7 passed / 1
    unrelated failure re-blocked a green card).
    """
    if not summary:
        return []
    out: list[str] = []
    for line in summary.splitlines():
        if not re.search(r"\bpytest\b", line, re.IGNORECASE):
            continue
        # pytest <file> tokens (one or more targets after the word pytest).
        for m in re.finditer(r"pytest\s+([^\s`;]+)", line, re.IGNORECASE):
            t = m.group(1).strip().strip('`"\'')
            if t and t not in out:
                out.append(t)
        # All tests/...py path tokens on a pytest line (multi-file targets).
        for m in re.finditer(r"(?:^|[\s`])(tests?/[\w./\-]+\.py)", line):
            t = m.group(1).strip()
            if t and t not in out:
                out.append(t)
    return out


def _expand_glob_args(args: list[str], repo_root: str) -> list[str]:
    """Shell-expand glob tokens in a pytest argv list.

    ``_run_pytest`` builds argv directly (no shell), so a card whose Verify
    line is ``pytest <repo>/tests/regressions/test_i614_*.py`` handed the
    UNEXPANDED pattern to pytest, which reports
    ``ERROR: file or directory not found: .../*.py`` and exits rc=4 — a
    false-block on a green card. ``_target_resolves`` already accepts a glob
    (glob.glob match) as a resolvable target, so honour it here the same way.

    A token without glob metacharacters is passed through untouched. A token
    whose expansion is empty is left as-is, so a genuinely missing target
    still fails rc=4 exactly as before.
    """
    import glob as _glob

    out: list[str] = []
    for arg in args:
        if arg.startswith("-") or not any(ch in arg for ch in "*?["):
            out.append(arg)
            continue
        pattern = arg if os.path.isabs(arg) else os.path.join(repo_root, arg)
        matches = sorted(_glob.glob(pattern))
        out.extend(matches if matches else [arg])
    return out


def _run_pytest(target: str, repo_root: str) -> dict:
    # target may be a space-joined list (e.g. two regression files joined by
    # `" ".join(...)` in the gate caller). Passing the joined string as ONE
    # argv makes pytest report "file or directory not found" (rc=4) and
    # false-blocks the card. Split into argv entries.
    import shlex

    target_args = shlex.split(target) if target else []
    target_args = _expand_glob_args(target_args, repo_root)
    cmd = [
        _CANONICAL_PYTHON,
        "-m",
        "pytest",
        *target_args,
        "-q",
        "--tb=line",
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=900,
        )
        out = proc.stdout or ""
        passed = failed = 0
        m = re.search(r"(\d+)\s+passed", out)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+)\s+failed", out)
        if m:
            failed = int(m.group(1))
        return {
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "output": out[-8000:],
        }
    except Exception as exc:
        return {
            "returncode": 2,
            "passed": 0,
            "failed": 0,
            "output": f"verify-gate error: {exc}",
        }


def _connect(board: Optional[str]):
    """Open a connection to the task's board DB (threads board through)."""
    from hermes_cli.kanban_db_connect import connect
    return connect(board=board)


def _veto_done_task(task_id: str, board: str, reason: str, kind: str = "needs_input") -> bool:
    """Flip a task that just completed back to ``blocked`` — the verify veto.

    ``kanban_task_completed`` fires AFTER the completion txn commits (hooks are
    observer-only, and ``block_task`` transitions running/ready only), so a
    done card cannot be re-blocked through the public API. Preferred path: the
    first-class core primitive ``hermes_cli.kanban_db.reopen_task`` (fork PR,
    done -> blocked with sticky event + descendant invalidation). Until that
    lands upstream we mirror the core's own inline CAS pattern
    (``kanban_swarm._activate_root_inline``): a scoped raw UPDATE guarded by
    ``status = 'done'`` plus a ``blocked`` event in the same txn. The
    ``blocked`` event is what makes the card sticky to ``recompute_ready`` (no
    auto-promote back to ready) and feeds notify subs. Best-effort; returns
    True on a real done->blocked transition.
    """
    try:
        from hermes_cli.kanban_db import reopen_task as _reopen_core
        from hermes_cli.kanban_db_connect import connect as _connect_core
    except Exception:
        _reopen_core = None  # pre-merge core: fall through to the raw flip
    if _reopen_core is not None:
        try:
            conn = _connect_core(board=board or None)
            try:
                return bool(_reopen_core(
                    conn, task_id, reason=reason, landing="blocked",
                    kind=kind, author="verify-gate",
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
                # One 'blocked' event mirrors block_task/_route_block: it makes
                # the block sticky (newest blocked/unblocked event) so
                # recompute_ready will NOT auto-promote, and it notifies
                # watcher subs.
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'blocked', ?, ?)",
                    (task_id, json.dumps(
                        {"reason": reason, "kind": kind, "recurrences": 1,
                         "source_status": "done"}),
                     int(time.time())),
                )
            # Mirrors block_task's post-commit lifecycle fire so plugin
            # observers (model-tracking, notify) see the veto.
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


def _block_task(task_id: str, board: str, reason: str) -> bool:
    """Veto a just-completed card by re-blocking it (see :func:`_veto_done_task`)."""
    return _veto_done_task(task_id, board, reason)


def _comment_task(task_id: str, board: str, body: str, author: str = "verify-gate") -> None:
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


_OVERRIDE_MARKER = re.compile(r"verify-gate:\s*override\b", re.IGNORECASE)
# Authors allowed to post the override marker. kanban-worker is deliberately
# excluded: a worker must never self-exempt its own closing.
_OPERATOR_AUTHORS = {"default", "operator", "local-only"}


def _has_operator_override(task_id: str, board: str) -> bool:
    """True if an operator posted 'verify-gate: override <reason>' on the card.

    The gate is fail-closed by design, but reconciliation closings
    (registry-only updates, already-fixed-at-HEAD, spec-superseded, or a
    green-at-HEAD card whose earlier block was a stale/out-of-scope gate run)
    legitimately ship with no scoped code/test change to re-run. An operator
    who has manually verified the closing posts the marker comment; the gate
    then records evidence and passes instead of re-blocking the card.
    """
    try:
        from hermes_cli.kanban_db import list_comments
        from hermes_cli.kanban_db_connect import connect
        conn = connect(board=board or None)
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


def _task_workspace_kind(task_id: str, board: str) -> Optional[str]:
    """Return the task's workspace_kind from the board DB, or None.

    The gate must run only for repo-backed tasks. Scratch (and other
    ephemeral) workspaces carry no canonical-tree handoff to verify, and
    the full acceptance suite costs ~70s on every closure — so we read the
    task's workspace kind from the DB rather than trusting the (often
    absent) ``workspace_path`` hook arg, which falls back to the canonical
    checkout regardless of task type.
    """
    try:
        from hermes_cli.kanban_db import get_task
        from hermes_cli.kanban_db_connect import connect
        conn = connect(board=board or None)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
        if row is None:
            return None
        return getattr(row, "workspace_kind", None)
    except Exception:
        return None


def _task_body(task_id: str, board: str) -> str:
    """Return the task's body from the board DB ('' on any failure).

    The kanban_task_completed hook does not always receive the card body
    (CLI completions and some worker paths omit it). Discovery and the
    implementation-card fail-closed rule need the real body, so read it
    from the DB when the hook arg is missing.
    """
    try:
        from hermes_cli.kanban_db import get_task
        from hermes_cli.kanban_db_connect import connect
        conn = connect(board=board or None)
        try:
            row = get_task(conn, task_id)
        finally:
            conn.close()
        if row is None:
            return ""
        return getattr(row, "body", "") or ""
    except Exception:
        return ""


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
    # Gate: only repo-backed tasks (dir:/worktree) are verified against the
    # canonical tree. Scratch tasks have no canonical handoff to check, and
    # running the full suite on them adds ~70s latency to every completion
    # with nothing to verify. Non-repo workspaces are skipped entirely.
    # Accept both plain 'dir' and legacy 'dir:<path>' stored forms.
    kind = _task_workspace_kind(task_id, board) or ""
    if not (kind == "worktree" or kind == "dir" or kind.startswith("dir:")):
        return
    # Only consult the hook arg when it was actually supplied. _detect_repo_root
    # falls through to the canonical checkout when workspace_path is falsy, so
    # calling it with an ABSENT hook arg would return the canonical repo and make
    # the stored-workspace_path fallback below dead code — that false-blocks
    # every CLI completion whose card points at a non-canonical git repo.
    repo_root = _detect_repo_root(workspace_path) if workspace_path else None
    if not repo_root and (kind == "worktree" or kind == "dir" or kind.startswith("dir:")):
        # Hook workspace_path is often absent (CLI completions omit it). Fall
        # back to the task's stored workspace_path so non-realm-forge repos
        # (hermes-agent, plugins/, wiki/) are verified against the tree they
        # actually committed to, not hijacked by the realm-forge canonical.
        try:
            from hermes_cli.kanban_db import get_task
            from hermes_cli.kanban_db_connect import connect

            conn = connect(board=board or None)
            try:
                row = get_task(conn, task_id)
            finally:
                conn.close()
            if row is not None:
                stored = getattr(row, "workspace_path", None)
                repo_root = _detect_repo_root(stored)
        except Exception:
            repo_root = None
    if not repo_root:
        # Neither the hook arg nor the stored workspace pointed at a git repo.
        # Preserve the historical canonical-checkout fallback for dir cards that
        # live outside version control (realm-forge scratch dirs).
        repo_root = _detect_repo_root("")
    if not repo_root:
        return

    # The hook may fire without the card body (CLI completions and some
    # worker paths omit it). Read the real body from the board DB so the
    # claimed-file guard, discovery, and the implementation-card fail-closed
    # rule all see the actual card.
    body = (task_body or "").strip() or _task_body(task_id, board)

    # Operator override: an operator who has manually verified the closing
    # (reconciliation: registry-only, already-fixed-at-HEAD, spec-superseded,
    # or verified-green-at-HEAD after a stale gate run) posts
    # 'verify-gate: override <reason>'. The gate then records evidence and
    # passes instead of re-blocking the card.
    override = _has_operator_override(task_id, board)

    # Claimed-file guard: a FILE:/CHANGE: card must actually modify the named
    # source file(s). Workers that complete with test-only commits (or empty
    # ones) get blocked here instead of closing — this is what let I87/I91
    # close with zero source changes.
    claimed = _claimed_files(body)
    changed = _git_changed_files(
        repo_root,
        task_id=task_id,
        since_epoch=_task_claim_epoch(task_id, board),
    )
    if claimed:
        missing = []
        for f in claimed:
            nf = f.replace("\\", "/")
            base = nf.rsplit("/", 1)[-1]
            # Skip references that are not real files in this repo: dotfile
            # dirs (~/.hermes, ~/.config) get misparsed by the extension regex,
            # and hallucinated nested paths (agent/agent/x.py) don't exist.
            # Only enforce that claimed files which ACTUALLY EXIST were changed.
            exists_anywhere = False
            for root, _dirs, files in os.walk(repo_root):
                if base in files:
                    exists_anywhere = True
                    break
            if not exists_anywhere:
                continue
            if nf in changed or nf in [c.replace("\\", "/") for c in changed]:
                continue
            if any(c.rsplit("/", 1)[-1] == base for c in changed):
                continue
            missing.append(f)
        if missing:
            reason = (
                "verify_gate: claimed source file(s) not modified by this "
                f"completion: {', '.join(missing)}. Card body says FILE: these "
                "files, but the commit/working tree did not touch them. "
                "Test-only or empty commits cannot close a FILE:/CHANGE: card."
            )
            if override:
                _comment_task(
                    task_id, board,
                    "verify-gate: operator override honored — claimed-file "
                    "guard skipped for no-code-change closing. " + reason,
                )
                return
            _comment_task(task_id, board, reason)
            _block_task(task_id, board, reason)
            return

    # Shellcheck stage: any claimed .sh file must lint clean at warning+
    # severity (same bar as the realm-forge pre-commit hook). Warning+ findings
    # block the card; info style nits surface in the evidence comment only.
    if claimed:
        sc_problems, sc_output = _shellcheck_claimed(claimed, repo_root)
        if sc_problems:
            reason = (
                "verify_gate: shellcheck failed on claimed shell script(s): "
                f"{', '.join(sc_problems)}. Fix findings or add targeted "
                "# shellcheck disable= with a reason; see the evidence comment."
            )
            if override:
                _comment_task(
                    task_id, board,
                    "verify-gate: operator override honored — shellcheck "
                    "findings waived. " + reason + "\n\n" + sc_output,
                )
                return
            _comment_task(task_id, board, reason + "\n\n" + sc_output)
            _block_task(task_id, board, reason)
            return
    else:
        sc_output = ""

    # Ruff stage: any claimed .py file must lint clean (bug-only rules
    # F/E4/E7 per repo pyproject.toml — same bar as the pre-commit hook).
    # Findings block the card; the evidence comment carries the output.
    if claimed:
        rf_problems, rf_output = _ruff_claimed(claimed, repo_root)
        if rf_problems:
            reason = (
                "verify_gate: ruff check failed on claimed Python file(s): "
                f"{', '.join(rf_problems)}. Fix findings (ruff check --fix or "
                "manually) before closing; see the evidence comment."
            )
            if override:
                _comment_task(
                    task_id, board,
                    "verify-gate: operator override honored — ruff findings "
                    "waived. " + reason + "\n\n" + rf_output,
                )
                return
            _comment_task(task_id, board, reason + "\n\n" + rf_output)
            _block_task(task_id, board, reason)
            return
    else:
        rf_output = ""

    target = _detect_test_target(body)
    if target and not _target_resolves(target, repo_root):
        # Verify: line is prose, a template placeholder (<n>), a bare shell
        # command, or a nonexistent path — running pytest on it yields rc=4
        # 'no tests ran' and false-blocks a completed card. Drop it and let the
        # scoped-discovery path below decide (comment-and-pass when nothing
        # testable is derivable).
        target = ""
    if not target:
        # No explicit pytest/verify line in the body. Scope to the card's own
        # work in order of specificity: the worker's summary pytest claim,
        # then the test files the worker actually touched this run, then the
        # issue-id regression test (test_i<issue>_*.py), then TDAD impact.
        # Never default to the whole suite: the board carries many unrelated
        # pre-existing failures that would re-block a green card indefinitely.
        scoped = _summary_test_targets(summary)
        if scoped:
            scoped = [t for t in scoped if _target_resolves(t, repo_root)]
        if not scoped:
            scoped = _changed_test_files(repo_root)
        if scoped:
            scoped = [t for t in scoped if _target_resolves(t, repo_root)]
        if not scoped:
            scoped = _discover_regression_tests(body, repo_root)
        if scoped:
            scoped = [t for t in scoped if _target_resolves(t, repo_root)]
        if not scoped:
            scoped = _impact_affected_tests(body, repo_root)
        if scoped:
            scoped = [t for t in scoped if _target_resolves(t, repo_root)]
        if scoped:
            target = " ".join(dict.fromkeys(scoped))
        else:
            # Fail closed: if the card names source files (FILE:/CHANGE: body)
            # but we still can't derive a scoped test target, block for
            # operator attention instead of silently passing.
            if claimed:
                if override:
                    _comment_task(
                        task_id, board,
                        "verify-gate: operator override honored — no scoped "
                        "test target; closing per operator verification.",
                    )
                    return
                reason = (
                    "verify_gate: no scoped test target derivable for a "
                    "FILE:/CHANGE: card (no pytest/verify line, no "
                    "test_i<issue>_*.py, no summary pytest line). Blocking for "
                    "operator verification instead of closing on self-report."
                )
                _comment_task(task_id, board, reason)
                _block_task(task_id, board, reason)
                return
            # Implementation-issue cards (create_known_issue_tasks.py body:
            # 'Fix expected: implement and verify.') must also fail closed.
            # A worker can close on RED-only self-report before its regression
            # test is discoverable, letting a red test slip through as done
            # (observed I69 2026-09-03: closed done with
            # test_i69_category_fallback_buyback_loop.py still failing).
            # Research/decision/test-only cards without 'Fix expected' keep
            # the old comment-and-pass path.
            if re.search(r"Fix expected", body, re.IGNORECASE):
                if override:
                    _comment_task(
                        task_id, board,
                        "verify-gate: operator override honored — "
                        "implementation card closed per operator verification.",
                    )
                    return
                reason = (
                    "verify_gate: implementation card with no scoped test "
                    "target derivable at gate time (no pytest/verify line, no "
                    "test_i<issue>_*.py found in tree, no summary pytest "
                    "line). The worker may have closed on RED-only self-report "
                    "before committing its regression test. Blocking for "
                    "operator verification instead of closing on self-report."
                )
                _comment_task(task_id, board, reason)
                _block_task(task_id, board, reason)
                return
            _comment_task(
                task_id, board,
                "verify-gate: no scoped test target derivable (no pytest/verify "
                "line, no test_i<issue>_*.py regression test, no File: lines for "
                "TDAD impact, no summary pytest line). Skipping full-suite gate "
                "to avoid re-blocking on unrelated pre-existing failures; "
                "operator verifies the scoped change directly.",
            )
            return
    result = _run_pytest(target, repo_root)

    # TDAD impact pass: also run tests affected by the changed files so a
    # "fixes one thing, breaks the neighborhood" regression is caught here,
    # not after the card closes. Only when the stated target passed.
    impact_tests = []
    impact_result = None
    if result["failed"] == 0 and result["returncode"] == 0:
        impact_tests = _impact_affected_tests(task_body or "", repo_root)
        if impact_tests:
            impact_target = " ".join(impact_tests)
            impact_result = _run_pytest(impact_target, repo_root)

    evidence = (
        f"verify-gate evidence\n"
        f"- repo: {repo_root}\n"
        f"- target: {target}\n"
        f"- passed: {result['passed']}\n"
        f"- failed: {result['failed']}\n"
        f"- returncode: {result['returncode']}\n"
        + (f"- impact-affected tests ({len(impact_tests)}): {' '.join(impact_tests)}\n"
           f"- impact passed: {impact_result['passed']}\n"
           f"- impact failed: {impact_result['failed']}\n"
           f"- impact returncode: {impact_result['returncode']}\n"
           if impact_result else "")
        + f"```\n{result['output']}\n```"
        + (f"\n\nImpact test output:\n```\n{impact_result['output']}\n```"
           if impact_result else "")
        + (f"\n\nShellcheck (claimed .sh):\n```\n{sc_output}\n```"
           if sc_output else "")
        + (f"\n\nRuff (claimed .py):\n```\n{rf_output}\n```"
           if rf_output else "")
    )
    failed_any = (result["failed"] > 0 or result["returncode"] != 0
                  or (impact_result and (impact_result["failed"] > 0 or impact_result["returncode"] != 0)))
    if failed_any:
        if override:
            _comment_task(
                task_id, board,
                evidence
                + "\n\nverify-gate: operator override honored — pytest "
                "failure verified by operator as out-of-scope/pre-existing; "
                "card closed.",
            )
            return
        _block_task(
            task_id,
            board,
            f"verify_gate: pytest failed target={target} failed={result['failed']} rc={result['returncode']}"
            + (f" impact_failed={impact_result['failed']}" if impact_result else ""),
        )
        _comment_task(
            task_id, board, evidence + "\n\nCard re-blocked by verify-gate."
        )
        return
    _comment_task(task_id, board, evidence + "\n\nClosure verified.")


def register(ctx: Any) -> None:
    ctx.register_hook("kanban_task_completed", on_kanban_task_completed)
