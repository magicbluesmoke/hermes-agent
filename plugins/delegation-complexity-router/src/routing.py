"""Shared complexity routing logic for kanban and delegation plugins.

This module provides the core classification and routing functions
shared between kanban-complexity-router and delegation-complexity-router.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Embedded classifier (keyword heuristic)
# ---------------------------------------------------------------------------
_KEYWORDS: Dict[str, list] = {
    "classification": [
        "classif", "categoriz", "label", "tag", "type of", "identify",
        "determine whether", "belongs to", "bucket",
    ],
    "extraction": [
        "extract", "pull out", "gather", "collect", "parse",
        "scrape", "harvest", "mine", "retrieve", "get all",
    ],
    "tool_selection": [
        "which tool", "choose tool", "pick tool", "select tool",
        "tool call", "function call", "what to call",
    ],
    "state_tracking": [
        "track", "monitor", "state", "status", "progress",
        "where are we", "what changed", "diff", "delta", "change since",
    ],
    "coding": [
        "code", "implement", "refactor", "bug", "debug", "function",
        "class", "api", "fix", "patch", "module", "type annot",
    ],
    "analysis": [
        "analyze", "synthesize", "compare", "evaluate", "review",
        "critique", "research", "study", "investigate",
    ],
    "architecture": [
        "architecture", "system design", "design", "roadmap",
        "event-driven", "async", "tech plan",
    ],
    "planning": [
        "plan", "roadmap", "milestone", "strategy",
    ],
    "tool_use": [
        "run", "execute", "search", "find", "grep", "list",
        "read", "write", "patch", "shell", "command", "script",
    ],
    "summarization": [
        "summarize", "tl;dr", "condense", "digest", "brief",
        "abstract", "overview", "key point",
    ],
    "general": [
        "what is", "how to", "explain", "define", "describe",
        "tell me", "help", "question",
    ],
}

_PRIORITY: list = [
    "architecture",
    "state_tracking",
    "coding",
    "analysis",
    "planning",
    "classification",
    "extraction",
    "tool_selection",
    "tool_use",
    "summarization",
    "rag_chunk_filtering",
    "general",
]

_COMPLEXITY_INDICATORS: Dict[str, int] = {
    "architecture": 3,
    "from scratch": 3,
    "multi-file": 2,
    "system design": 3,
    "full": 2,
    "complex": 2,
    "design": 1,
    "framework": 1,
    "event-driven": 2,
    "asynchronous": 1,
    "fix typo": -2,
    "rename": -2,
    "simple": -1,
}


def classify_task(goal: str) -> str:
    """Classify a task goal string into a task type (lowercase str)."""
    goal_lower = (goal or "").lower()
    for task_type in _PRIORITY:
        for kw in _KEYWORDS.get(task_type, []):
            if kw and (kw[0].isalpha() or kw[0] == "_"):
                if len(kw) < 6:
                    pattern = r"\b" + re.escape(kw) + r"\b"
                else:
                    pattern = r"\b" + re.escape(kw)
            else:
                pattern = re.escape(kw)
            if re.search(pattern, goal_lower):
                return task_type
    return "general"


def estimate_complexity(goal: str) -> int:
    """Estimate task complexity 1-10 based on keywords."""
    goal_lower = (goal or "").lower()
    score = 5  # baseline
    for indicator, delta in _COMPLEXITY_INDICATORS.items():
        if indicator in goal_lower:
            score += delta
    return max(1, min(10, score))


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------
_BLOCK_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "default_scope": "turn",
    "strategy": "model",
    "thresholds": {"escalate": 7, "max_complexity": 10},
    "routes": [],
    "routes_moa": [],
    "fallback": {},
    "safeguards": {
        "max_escalations_per_session": 10,
        "max_moa_escalations_per_session": 3,
        "require_confirmation": False,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def get_complexity_routing(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the complexity_routing block with safe defaults."""
    if config is None:
        config = {}
    raw_block = config.get("complexity_routing")
    if not isinstance(raw_block, dict):
        raw_block = config if isinstance(config, dict) and "enabled" in config else {}
    return _deep_merge(_BLOCK_DEFAULTS, raw_block)


# ---------------------------------------------------------------------------
# Route matching + target selection
# ---------------------------------------------------------------------------
def _match_route(routes: Any, task_type: str, complexity: int) -> Optional[Dict[str, Any]]:
    if not isinstance(routes, list):
        return None
    for route in routes:
        if not isinstance(route, dict):
            continue
        when = route.get("when") or {}
        min_c = int(when.get("min_complexity", 0) or 0)
        types = when.get("types") or []
        if complexity >= min_c and (not types or task_type in types):
            return route
    return None


def _fallback_target(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fb = block.get("fallback") or {}
    if not fb.get("model"):
        return None
    return {
        "strategy": "model",
        "provider": fb.get("provider"),
        "model": fb.get("model"),
        "preset": None,
        "scope": block.get("default_scope", "turn"),
    }


def select_target(goal: str, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Choose escalation target based on goal complexity.

    Returns a dict with strategy/provider/model/preset/scope or None
    when routing is disabled, no route matches, or a session cap is hit.
    """
    block = get_complexity_routing(config)
    if not block.get("enabled"):
        return None

    goal = (goal or "").strip()
    if not goal:
        return None

    classifier = str(block.get("classifier") or "laya").lower()
    if classifier in ("laya", "laya-route", "laya-choice"):
        # Laya router (legacy - currently not used)
        from . import laya_route
        result = laya_route(goal, config=block)
        return result.get("target") if result else None

    task_type = classify_task(goal)
    complexity = estimate_complexity(goal)

    thresholds = block.get("thresholds") or {}
    escalate_min = int(thresholds.get("escalate", 7) or 7)

    if complexity < escalate_min:
        return None

    safeguards = block.get("safeguards") or {}
    max_esc = int(safeguards.get("max_escalations_per_session", 10) or 10)
    max_moa = int(safeguards.get("max_moa_escalations_per_session", 3) or 3)
    strategy = str(block.get("strategy") or "model").lower()

    # MoA route: architecture/research/planning at high complexity.
    moa_route = _match_route(block.get("routes_moa"), task_type, complexity)
    if moa_route:
        if not hasattr(_fallback_target, '_session_moa_escalations'):
            _fallback_target._session_moa_escalations = 0
        if _fallback_target._session_moa_escalations >= max_moa:
            return _fallback_target(block)
        _fallback_target._session_moa_escalations += 1
        preset = moa_route.get("preset") or block.get("preset")
        model_route = _match_route(block.get("routes"), task_type, complexity)
        target = model_route.get("target") or {} if model_route else {}
        provider = moa_route.get("provider") or target.get("provider") or (block.get("fallback") or {}).get("provider")
        model = moa_route.get("model") or target.get("model") or (block.get("fallback") or {}).get("model")
        return {
            "strategy": "moa",
            "provider": provider,
            "model": model,
            "preset": preset,
            "scope": moa_route.get("scope") or block.get("default_scope", "turn"),
        }

    route = _match_route(block.get("routes"), task_type, complexity)
    if not route:
        return None
    if not hasattr(_fallback_target, '_session_escalations'):
        _fallback_target._session_escalations = 0
    if _fallback_target._session_escalations >= max_esc:
        return _fallback_target(block)
    _fallback_target._session_escalations += 1
    target = route.get("target") or {}
    return {
        "strategy": "model",
        "provider": target.get("provider"),
        "model": target.get("model"),
        "preset": None,
        "scope": target.get("scope") or block.get("default_scope", "turn"),
    }
