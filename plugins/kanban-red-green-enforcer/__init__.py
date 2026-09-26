"""kanban-red-green-enforcer plugin.

Pre-verify hook that enforces RED-GREEN-REFACTOR discipline by checking
that test files exist alongside changed source files before the agent
completes a coding turn.

If the agent edited source code but no corresponding test was modified
this turn, the hook injects a "write the failing test first" nudge.

Survives hermes-agent updates because user plugins live under
~/.hermes/plugins/, not in the agent checkout.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Patterns to skip: non-code files that don't need tests
_NON_CODE_EXTENSIONS = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".tsv",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".gitignore", ".gitkeep", ".editorconfig",
})

# Patterns to detect test filenames
_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*_test\.py$"),
)


def _is_source_python(path: str) -> bool:
    """Return True if path is a .py file not under a tests/ directory."""
    p = Path(path)
    if p.suffix != ".py":
        return False
    # Check if the path is itself under tests/
    parts = p.as_posix().split("/")
    if "tests" in parts and ".py" not in parts[:parts.index("tests")]:
        return False
    # Also skip __init__.py, conftest.py, setup.py
    if p.name in ("__init__.py", "conftest.py", "setup.py"):
        return False
    return True


def _is_test_file(path: str) -> bool:
    """Return True if the filename looks like a test file."""
    name = Path(path).name
    return any(p.match(name) for p in _TEST_FILE_PATTERNS)


def _infer_test_path(source_path: str, project_root: str) -> str:
    """Infer the expected test file path for a given source file.

    Heuristics in order:
    1. Mirror `src/engine/foo.py` to `tests/test_foo.py`
    2. Mirror `src/engine/foo.py` to `tests/engine/test_foo.py`
    3. Fall back to `tests/test_<stem>.py` at project root
    """
    src = Path(source_path)
    stem = src.stem
    project = Path(project_root)

    # Normalise relative path
    try:
        rel = src.relative_to(project)
        rel_parts = list(rel.parts)
    except ValueError:
        rel_parts = []

    # Candidate test paths
    candidates = []

    # Mirror src/ to tests/ preserving subdir structure
    if rel_parts and len(rel_parts) > 1:
        # Remove src/ prefix if present
        if rel_parts[0] in ("src", "source", "lib"):
            subdirs = rel_parts[1:-1]
        else:
            subdirs = rel_parts[:-1]

        # tests/ with subdir preservation: tests/<subdirs>/test_<stem>.py
        if subdirs:
            candidates.append(
                str(project / "tests" / "/".join(subdirs) / f"test_{stem}.py")
            )

        # tests/ without subdir: tests/test_<stem>.py
        candidates.append(str(project / "tests" / f"test_{stem}.py"))

    # Root-level test
    candidates.append(str(project / "tests" / f"test_{stem}.py"))

    return candidates[0]


def _find_matching_test(
    source_path: str, project_root: str
) -> Optional[dict[str, Any]]:
    """Look for existing test files matching a source path.

    Returns dict with 'path' of the first match found, or None.
    """
    project = Path(project_root)
    src = Path(source_path)
    stem = src.stem

    # Try to normalise relative path for subdir mirroring
    try:
        rel = src.relative_to(project)
        rel_parts = list(rel.parts)
    except ValueError:
        rel_parts = []

    # Candidate patterns to search
    search_globs = []

    # Mirror with subdir: tests/**/test_<stem>.py
    search_globs.append(f"tests/**/test_{stem}.py")
    search_globs.append(f"tests/**/{stem}_test.py")
    # Also check for test_<stem> anywhere
    search_globs.append(f"tests/**/test_{stem}.*")

    from glob import iglob

    for pattern in search_globs:
        full_pattern = str(project / pattern)
        for match in iglob(full_pattern, recursive=True):
            if os.path.isfile(match):
                return {"path": match}

    # Fall back to direct inferred path
    inferred = _infer_test_path(source_path, project_root)
    if os.path.isfile(inferred):
        return {"path": inferred}

    return None


def _resolve_project_root(changed_paths: list[str]) -> Optional[str]:
    """Find the project root from changed paths or env vars.

    Priority:
    1. HERMES_KANBAN_WORKSPACE env var (kanban worker context)
    2. Common ancestor of changed paths with a .git dir
    """
    # Kanban worker workspace
    ws = os.environ.get("HERMES_KANBAN_WORKSPACE", "")
    if ws and os.path.isdir(ws):
        git_dir = os.path.join(ws, ".git")
        if os.path.isdir(git_dir):
            return ws

    # Scan ancestors of changed paths for .git
    candidates: list[str] = []
    for cp in changed_paths:
        p = Path(cp).resolve()
        for parent in [p] + list(p.parents):
            git_dir = parent / ".git"
            if git_dir.is_dir():
                candidates.append(str(parent))
                break

    if candidates:
        return candidates[0]

    # Fallback: current working directory
    cwd = os.getcwd()
    git_dir = os.path.join(cwd, ".git")
    if os.path.isdir(git_dir):
        return cwd

    return None


def _get_touched_test_paths(
    changed_paths: list[str], project_root: str
) -> set[str]:
    """Return the set of test file paths that were changed this turn, normalised.

    Returns both absolute and project-relative forms so callers can
    match regardless of how the path was obtained.
    """
    result: set[str] = set()
    proj = Path(project_root)
    for p in changed_paths:
        if not (p.endswith(".py") and _is_test_file(p)):
            continue
        result.add(p)
        # Also add the absolute form
        result.add(str(Path(p).resolve()))
        # Also add the project-relative form
        try:
            rel = Path(p).resolve()
            result.add(str(rel.relative_to(proj)))
        except (ValueError, OSError):
            pass
        try:
            result.add(str(proj / p))
        except Exception:
            pass
    return result


def on_pre_verify(
    *,
    session_id: str = "",
    platform: str = "",
    model: str = "",
    coding: bool = False,
    attempt: int = 0,
    final_response: str = "",
    changed_paths: Optional[list[str]] = None,
    **kwargs: Any,
) -> Optional[dict[str, Any]]:
    """Pre-verify hook: enforce RED phase before coding turn completes.

    Checks that for every modified Python source file, a corresponding
    test file exists. If a test is missing or was not modified this turn,
    nudges the agent to write the failing test first (RED phase).
    """
    # Accept future hook kwargs (eg telemetry_schema_version) silently
    # Only fire in coding contexts where files were edited
    if not coding or not changed_paths:
        return None

    # Throttle: only nudge on early attempts (default max_verify_nudges = 3)
    if attempt >= 2:
        return None

    # Filter to source Python files
    source_files = [p for p in changed_paths if _is_source_python(p)]
    if not source_files:
        return None

    # Find the project root
    project_root = _resolve_project_root(changed_paths)
    if not project_root:
        logger.debug("No project root found — skipping RED-GREEN check")
        return None

    # What test files were touched this turn
    touched_tests = _get_touched_test_paths(changed_paths, project_root)

    # Check each source file
    missing_tests: list[str] = []
    untouched_tests: list[str] = []

    for src in source_files:
        match = _find_matching_test(src, project_root)
        if match is None:
            # No test file exists at all
            test_path = _infer_test_path(src, project_root)
            # Compute relative path for the message
            try:
                rel_test = Path(test_path).relative_to(project_root)
                missing_tests.append(str(rel_test))
            except ValueError:
                missing_tests.append(test_path)
        else:
            # Test file exists — was it modified this turn?
            test_abs = str(Path(match["path"]).resolve())
            test_rel = os.path.relpath(test_abs, project_root)
            if test_abs not in touched_tests:
                untouched_tests.append(test_rel)

    if not missing_tests and not untouched_tests:
        # All source files have tests that were modified this turn — RED
        # phase is satisfied. Let the turn finish.
        return None

    # Build the nudge message
    lines: list[str] = [
        "[System: RED-GREEN-REFACTOR enforcement — you edited source code "
        "but did not complete the RED phase for all changes.]"
    ]
    lines.append("")
    lines.append("The task body specifies:")
    lines.append("    RULE: You MUST write the failing test BEFORE writing the code. This is a hard requirement.")
    lines.append("")

    if missing_tests:
        lines.append("Missing test files (write these first — RED phase):")
        for t in missing_tests:
            lines.append(f"  - {t}")
        lines.append("")

    if untouched_tests:
        lines.append("Test files exist but were NOT modified this turn (update them for your changes):")
        for t in untouched_tests:
            lines.append(f"  - {t}")
        lines.append("")

    lines.append("Go back and write the failing test (RED) BEFORE modifying the source code (GREEN).")
    lines.append("After the test passes, you may proceed with GREEN + REFACTOR.")

    message = "\n".join(lines)

    logger.info(
        "RED-GREEN enforcer fired: %d missing tests, %d untouched tests (attempt %d)",
        len(missing_tests), len(untouched_tests), attempt,
    )

    return {"action": "continue", "message": message}


def register(ctx: Any) -> None:
    """Register the pre_verify hook."""
    ctx.register_hook("pre_verify", on_pre_verify)
