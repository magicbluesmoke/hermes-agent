"""Dispatcher/plugin-side complexity router for delegate_task calls.

Returns model/provider/preset overrides from delegate_task_routing hook
based on goal complexity estimation.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Complexity routing config/resolver
# ---------------------------------------------------------------------------

def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        return load_config()
    except Exception:
        return {}


def _routing_block() -> Dict[str, Any]:
    try:
        cfg = _load_config()
        return get_complexity_routing(cfg)
    except Exception:
        return {"enabled": False}


def _resolve_routing(block: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    try:
        return select_target(goal, config=block)
    except Exception as exc:
        logger.debug("delegation-complexity-router select_target failed: %s", exc)
        return None


def _encode_target(target: Dict[str, Any]) -> Optional[str]:
    """Encode target to string for provider selection."""
    strategy = str(target.get("strategy") or "model").strip().lower()
    provider = target.get("provider")
    model = target.get("model")
    preset = target.get("preset")

    if strategy == "model":
        if not model:
            return None
        value = str(model)
        if provider:
            value = f"{provider}/{value}"
        return value
    if strategy == "moa":
        if not preset:
            return None
        value = f"moa:{preset}"
        if provider and model:
            value = f"{provider}/{model}:{preset}"
        elif provider:
            value = f"{provider}:{preset}"
        return value
    return None


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def handle_delegate_task_routing(**kwargs: Any) -> Dict[str, Any]:
    """Handle delegate_task_routing hook - return model overrides for delegation.

    Returns:
        Dict with keys: model, provider, preset, scope (or empty dict if no override)
    """
    goal = kwargs.get("goal") or ""
    
    block = _routing_block()
    if not isinstance(block, dict) or not block.get("enabled"):
        return {}
    
    target = _resolve_routing(block, goal)
    if not target:
        return {}
    
    # Return the full target dict for delegate_task routing
    return {
        "model": target.get("model"),
        "provider": target.get("provider"),
        "preset": target.get("preset"),
        "scope": target.get("scope"),
    }
