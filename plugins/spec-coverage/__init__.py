"""spec-coverage plugin.

Pre-verify hook that nudges agents when source code changes touch areas
covered by SPEC.md without also updating the spec file.

The hook checks:
- If source code files changed and SPEC.md was NOT changed, it emits a
  targeted nudge listing which SPEC.md sections may need review.
- If SPEC.md changed without code changes, it's a pure spec update — passes.
- If nothing relevant changed, passes silently.

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

# ── File-to-section mapping ────────────────────────────────────────────────
# Maps filename patterns to SPEC.md section references.
# The patterns are matched against the basename of changed files.

_FILE_SECTION_MAP: dict[str, list[str]] = {
    # §1 Architecture Invariants
    "backend.py": ["§1.1 Backend Abstraction", "§1.2 Input Routing"],
    "pygame_backend.py": [
        "§1.1 Backend Abstraction",
        "§1.2 Input Routing",
        "§1.6 Sound Architecture",
        "§2 Rendering Spec",
    ],
    "game.py": ["§1.3 Game Loop Ownership"],
    "state.py": ["§1.4 Save Path"],
    "saveload.py": ["§1.4 Save Path"],
    "invariants.py": ["§1.5 Invariant Validation"],
    "sound.py": ["§1.6 Sound Architecture"],
    "music_plugin.py": ["§1.6 Sound Architecture"],
    "audio_asset": ["§1.6 Sound Architecture"],

    # §2 Rendering Spec
    "renderer.py": ["§2.1 Color Tag Parser", "§2.2 Semantic Color Conventions"],
    "character_select.py": ["§2.3 Font Policy"],
    "npc_display": ["§2.2 Semantic Color Conventions"],

    # §3 Behavioral Contracts
    "combat.py": ["§3.1 Combat Model"],
    "parser.py": ["§3.2 Parser Contract"],
    "events.py": ["§3.3 Event System"],
    "event": ["§3.3 Event System"],
    "npc_ai.py": ["§3.4 NPC AI Integration"],
    "llm_contract.py": ["§3.4 NPC AI Integration"],
    "command_interpreter.py": ["§3.4 NPC AI Integration"],
    "movement.py": ["§3.3 Event System", "§1.4 Save Path"],
    "interaction.py": ["§3.3 Event System"],

    # §4 Test Contracts
    "test_sound.py": ["§4.1 Headless Test Mode", "§1.6 Sound Architecture"],
    "conftest.py": ["§4.1 Headless Test Mode"],
    "test_": ["§4.2 Regression Guard Files", "§4.4 Testing Invariant"],

    # §5 Dependency Pins
    "pyproject.toml": ["§5 Dependency Pins"],
    "requirements": ["§5 Dependency Pins"],
}

# Non-code file extensions that shouldn't trigger a spec nudge
_NON_CODE_EXTENSIONS = frozenset({
    ".md", ".markdown",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".wav", ".ogg", ".mp3",
    ".gitignore", ".gitkeep", ".editorconfig",
})

# Files that are spec-level themselves — changing these IS a spec change
_SPEC_FILES = frozenset({"SPEC.md", "AGENTS.md"})


def _get_project_root(changed_paths: list[str]) -> Optional[str]:
    """Find the project root from changed paths or env vars."""
    ws = os.environ.get("HERMES_KANBAN_WORKSPACE", "")
    if ws and os.path.isdir(ws):
        if os.path.isdir(os.path.join(ws, ".git")):
            return ws

    for cp in changed_paths:
        p = Path(cp).resolve()
        for parent in [p] + list(p.parents):
            if (parent / ".git").is_dir():
                return str(parent)

    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, ".git")):
        return cwd
    return None


def _is_code_file(path: str) -> bool:
    """Return True if the path is a code file that SPEC.md might cover."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _NON_CODE_EXTENSIONS:
        return False
    # Python, config, and data files
    return ext in {".py", ".toml", ".yaml", ".yml", ".json"}


def _find_matching_sections(
    changed_paths: list[str],
    project_root: str,
) -> tuple[list[tuple[str, str]], bool]:
    """Find SPEC.md sections that match the changed files.

    Returns (matched_sections, spec_changed) where matched_sections is
    a list of (filename, [sections]) tuples and spec_changed indicates
    whether SPEC.md itself was among the changed files.
    """
    matched: list[tuple[str, str]] = []
    spec_changed = False
    project = Path(project_root)

    for cp in changed_paths:
        name = Path(cp).name

        # Check if SPEC.md/AGENTS.md was changed
        if name in _SPEC_FILES:
            spec_changed = True
            continue

        # Skip non-code files
        if not _is_code_file(cp):
            continue

        # Check filename prefix/pattern matching
        matched_sections: list[str] = []
        for pattern, sections in _FILE_SECTION_MAP.items():
            if pattern.startswith("test_") and name.startswith("test_"):
                # Test file pattern — match any test_*.py
                matched_sections.extend(sections)
            elif pattern == name or pattern in cp:
                matched_sections.extend(sections)
                break

        if matched_sections:
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique: list[str] = []
            for s in matched_sections:
                if s not in seen:
                    seen.add(s)
                    unique.append(s)
            # Get a short relative path for the message
            try:
                rel = Path(cp).resolve().relative_to(project)
                matched.append((str(rel), ", ".join(unique)))
            except (ValueError, OSError):
                matched.append((cp, ", ".join(unique)))

    return matched, spec_changed


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
    """Pre-verify hook: nudge agent to update SPEC.md when code touches spec areas.

    If source code changed but SPEC.md did not, and the changed files map
    to known SPEC.md sections, emit a targeted nudge.
    """
    # Accept future hook kwargs (eg telemetry_schema_version) silently
    if not coding or not changed_paths:
        return None

    # Throttle: only nudge on early attempts
    if attempt >= 2:
        return None

    # Filter to files that matter
    code_files = [p for p in changed_paths if _is_code_file(p)]
    if not code_files:
        return None

    # Find project root for SPEC.md
    project_root = _get_project_root(changed_paths)
    if not project_root:
        logger.debug("No project root found — skipping spec-coverage check")
        return None

    # Check for matching sections
    matched_sections, spec_changed = _find_matching_sections(
        changed_paths, project_root,
    )

    # If SPEC.md was already changed this turn, no nudge needed
    if spec_changed:
        return None

    # If no sections matched, still do a generic check:
    # any Python source change without SPEC.md change warrants a gentle nudge
    py_files = [p for p in changed_paths if p.endswith(".py")]

    if not matched_sections and not py_files:
        return None

    # Build the nudge message
    lines: list[str] = []

    if matched_sections:
        lines.append(
            "[System: You changed code covered by SPEC.md but did not update SPEC.md.]"
        )
        lines.append("")
        lines.append("The following files map to SPEC.md sections that may need review:")
        lines.append("")
        for path, sections in matched_sections:
            lines.append(f"  {path}")
            lines.append(f"    → {sections}")
    elif py_files:
        lines.append(
            "[System: You changed source code but did not update SPEC.md.]"
        )
        lines.append("")
        lines.append("If this change affects architecture, rendering, behavioral")
        lines.append("contracts, test contracts, or dependency pins, update SPEC.md")
        lines.append("before committing. See:")
        lines.append("  C:/Users/Michael Anselmi/OneDrive/Documents/04_Games/realm-forge-game/SPEC.md")

    if matched_sections or py_files:
        lines.append("")
        lines.append(
            "Per the spec-before-code rule: commit SPEC.md changes FIRST, "
            "then the implementation."
        )
        lines.append(
            "If the change does NOT affect any spec-covered area, "
            "acknowledge explicitly and continue."
        )

        message = "\n".join(lines)
        logger.info(
            "Spec-coverage nudge fired: %d file-section matches, %d total py files (attempt %d)",
            len(matched_sections), len(py_files), attempt,
        )
        return {"action": "continue", "message": message}

    return None


def register(ctx: Any) -> None:
    """Register the pre_verify hook."""
    ctx.register_hook("pre_verify", on_pre_verify)
