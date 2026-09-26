"""Self-contained complexity routing for kanban + delegation.

Replaces the wiped ``hermes_cli.complexity_routing`` integration module.
This module is intentionally dependency-light: it reads the
``complexity_routing`` config block, classifies goals with an embedded
keyword heuristic, applies thresholds/routes, and returns escalation
targets. It does NOT import from ``hermes_cli.*`` (except optionally
``hermes_cli.config.load_config`` for the caller), so it survives
``hermes update`` which wipes in-tree local edits.

Contract (kept compatible with the original integration layer):
- ``select_target(goal, config=None)`` -> dict with keys
  ``strategy/provider/model/preset/scope`` or ``None``.
  Accepts EITHER a full config dict containing a ``complexity_routing``
  key OR an already-resolved block dict (unwrapped-block shortcut).
- ``get_complexity_routing(config)`` -> default-safe block loader.
- ``reset_session_counters()`` -> clear module-global session caps.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session-scoped safeguard counters
# ---------------------------------------------------------------------------
_session_escalations: int = 0
_session_moa_escalations: int = 0


def reset_session_counters() -> None:
    """Reset escalation counters (used between isolated runs/tests)."""
    global _session_escalations, _session_moa_escalations
    _session_escalations = 0
    _session_moa_escalations = 0


# ---------------------------------------------------------------------------
# Embedded classifier (subset of the delegation-routing skill vocabulary)
# ---------------------------------------------------------------------------
_KEYWORDS: Dict[str, List[str]] = {
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
    "rag_chunk_filtering": [
        "filter chunk", "rerank", "retrieve relevant", "search chunk",
        "similarity", "embedding search", "vector search",
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
        "plan", "roadmap", "milestone", "strategy", "roadmap",
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

_PRIORITY: List[str] = [
    "architecture",      # explicit design/architecture -> route_moa candidates
    "state_tracking",    # needs reasoning -> cloud first
    "coding",            # complex generation -> cloud first
    "analysis",          # reasoning-heavy -> cloud first
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
    # +complexity
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
    # -complexity
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
    """Return the ``complexity_routing`` block with safe defaults.

    Accepts a full config dict (contains a ``complexity_routing`` key) or a
    bare block. Returns a merged dict that always has the default keys.
    """
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

    Returns a dict with ``strategy/provider/model/preset/scope`` or ``None``
    when routing is disabled, no route matches, or a session cap is hit
    (in which case the configured fallback target is returned).

    Classifier dispatch (2026-09-23; laya RETIRED 2026-09-26):
    the ``complexity_routing.classifier`` field selects the engine:
      - ``laya`` (RETIRED 2026-09-26 — do not re-enable): typed
        Choice(local/deepseek/delegate) + confidence from the Laya RLCD head.
        Kept only as legacy code; classifier reverted to delegation-routing.
      - ``delegation-routing`` (active default): keyword heuristic below.

    Route precedence (legacy path, matches the config design):
    - ``routes_moa`` first: high-complexity architecture/research/planning
      tasks escalate to an MoA preset (strategy=moa). Provider/model are
      sourced from the matching ``routes`` entry (or fallback) when the MoA
      route only declares a preset.
    - ``routes`` second: strategy=model escalation target.
    """
    global _session_escalations, _session_moa_escalations

    block = get_complexity_routing(config)
    if not block.get("enabled"):
        return None

    goal = (goal or "").strip()
    if not goal:
        return None

    classifier = str(block.get("classifier") or "laya").lower()
    if classifier in ("laya", "laya-route", "laya-choice"):
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
    # _match_route already enforces min_complexity + type membership, so a
    # match here means the task qualifies for MoA escalation.
    moa_route = _match_route(block.get("routes_moa"), task_type, complexity)
    if moa_route:
        if _session_moa_escalations >= max_moa:
            return _fallback_target(block)
        _session_moa_escalations += 1
        preset = moa_route.get("preset") or block.get("preset")
        # Source provider/model from the model route (or fallback) so the
        # encoded override is ``provider/model:preset`` per the test contract.
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
    if _session_escalations >= max_esc:
        return _fallback_target(block)
    _session_escalations += 1
    target = route.get("target") or {}
    return {
        "strategy": "model",
        "provider": target.get("provider"),
        "model": target.get("model"),
        "preset": None,
        "scope": target.get("scope") or block.get("default_scope", "turn"),
    }


# ---------------------------------------------------------------------------
# Laya typed router (2026-09-23)
# ---------------------------------------------------------------------------
# Replaces the keyword heuristic when ``complexity_routing.classifier: laya``.
# Calls the shared typed client (laya_route_client.py) which talks to the Laya
# head shim at :8091 (backbone at :8082). ONE attempt per task:
#   - Schema violation or head unreachable -> escalate to main lane (REVIEW),
#     never retry-forever.
#   - lane_conf (P_max) < DECIDE_LANE_CONF (0.55) -> low confidence -> main lane.
#   - rule noul >= VIOLATE_CONF (0.60) -> decompose-rule violation -> main lane.
#   - local + confident -> no override (worker's default local lane).
#   - deepseek/delegate + confident -> main paid lane (deepseek flash).

_LATENT_IMPORT_CACHE = None


def _load_laya_client():
    """Lazy-load the shared client from ~/.hermes/scripts (not on sys.path)."""
    global _LATENT_IMPORT_CACHE
    if _LATENT_IMPORT_CACHE is not None:
        return _LATENT_IMPORT_CACHE
    import importlib.util
    path = Path(os.path.expanduser("~/.hermes/scripts/laya_route_client.py"))
    if not path.exists():
        logger.warning("laya-route: client missing at %s", path)
        return None
    try:
        spec = importlib.util.spec_from_file_location("laya_route_client", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LATENT_IMPORT_CACHE = mod
        return mod
    except Exception as exc:
        logger.warning("laya-route: client import failed: %s", exc)
        return None


def laya_route(goal: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Typed lane + rule gate for one task. Returns {"target", "meta"}.

    ``target`` is the route-shape dict (or None = keep default local lane);
    ``meta`` carries the Laya verdict for observability:
      {lane, lane_conf, rule_noul, rule_violated, review, review_reason, source}
    On schema violation or unavailable head the task is escalated to the main
    lane (fallback target) with review_reason set — never retried.
    """
    block = get_complexity_routing(config)
    meta = {
        "lane": "unknown", "lane_conf": 0.0, "rule_noul": 0.0,
        "rule_violated": False, "review": False, "review_reason": None,
        "source": "laya",
    }
    if not block.get("enabled"):
        return {"target": None, "meta": meta}

    goal = (goal or "").strip()
    if not goal:
        return {"target": None, "meta": meta}

    client = _load_laya_client()
    if client is None:
        meta.update(review=True, review_reason="laya client unavailable -> main lane")
        return {"target": _fallback_target(block), "meta": meta}

    try:
        d = client.ask(goal)
    except Exception as exc:  # client raises LayaUnavailable / LayaSchemaError
        meta.update(
            review=True,
            review_reason=f"{type(exc).__name__}: {exc} -> main lane (fail-to-REVIEW, no retry)",
        )
        return {"target": _fallback_target(block), "meta": meta}

    meta.update(
        lane=d["lane"], lane_conf=d["lane_conf"], rule_noul=d["rule_noul"],
        rule_violated=d["rule_violated"], review=d["review"],
        review_reason=d.get("review_reason"), source=d.get("source", "laya"),
    )

    if d["review"]:
        # Low confidence OR rule violation OR explicit review -> main lane.
        return {"target": _fallback_target(block), "meta": meta}
    if d["lane"] == "local":
        # Confident local -> no override (worker default lane is local).
        return {"target": None, "meta": meta}
    # Confident deepseek / delegate -> main paid lane.
    return {"target": _fallback_target(block), "meta": meta}
