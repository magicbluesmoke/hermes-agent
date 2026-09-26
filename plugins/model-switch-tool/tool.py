"""Agent-callable model_switch tool — wraps hermes_cli.model_switch.switch_model().

Lets the agent autonomously switch models/providers mid-session with
configurable scope: turn (revert next turn), session, or global.

Imports the Hermes core module directly so it survives upstream changes —
switch_model() lives in the main codebase and is updated by Hermes releases.
"""

from __future__ import annotations

import logging

from tools.registry import tool_result, tool_error

logger = logging.getLogger(__name__)


def _current_default_model() -> str:
    """Resolve the configured default model for use as a live schema example.

    Reads config once at plugin load; any failure yields "" so the schema omits
    the default example rather than pinning a possibly-stale slug."""
    try:
        from hermes_cli.config import load_config, cfg_get

        return cfg_get(load_config(), "model", "default", default="") or ""
    except Exception:
        return ""


_DEFAULT_EXAMPLE = _current_default_model()

SCHEMA = {
    "name": "model_switch",
    "description": (
        "Switch the active model/provider for the current session or globally. "
        "Use this when a task exceeds the current model's capability — e.g. "
        "code review, refactoring, multi-file changes, architecture analysis. "
        "Pass 'scope': 'turn' to auto-revert after this turn, "
        "'scope': 'session' to keep the switch for the rest of the session, or "
        "'scope': 'global' to persist in config.yaml."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "model": {
                "type": "string",
                "description": (
                    "Model name or alias to switch to. Examples: "
                    "'claude-sonnet-4', 'sonnet' (alias), 'step' (alias for stepfun)"
                    + (
                        f", '{_DEFAULT_EXAMPLE}' (your configured default)"
                        if _DEFAULT_EXAMPLE
                        else ""
                    )
                    + ". To see current model: pass empty string."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Optional explicit provider slug. E.g. 'nous', 'deepseek', "
                    "'anthropic', 'custom'. Omit to let the resolver auto-detect."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["turn", "session", "global"],
                "description": (
                    "'turn' — switch for this turn only, auto-revert next input. "
                    "'session' — keep for the rest of the session. "
                    "'global' — persist to config.yaml (survives restart). "
                    "Default: 'turn'."
                ),
            },
        },
        "required": ["model"],
    },
}


def _read_current_config() -> dict:
    """Read the active model/provider/creds from config.

    Returns a dict with keys: provider, model, base_url, api_key,
    user_providers, custom_providers.
    """
    try:
        from hermes_cli.config import load_config, cfg_get

        cfg = load_config()

        current_provider = cfg_get(cfg, "model", "provider", default="")
        current_model = cfg_get(cfg, "model", "default", default="")
        current_base_url = cfg_get(cfg, "model", "base_url", default="")
        current_api_key = cfg_get(cfg, "model", "api_key", default="")
        user_providers = cfg_get(cfg, "providers", default={})
        custom_providers = cfg_get(cfg, "custom_providers", default=[])

        return {
            "provider": current_provider,
            "model": current_model,
            "base_url": current_base_url,
            "api_key": current_api_key,
            "user_providers": user_providers,
            "custom_providers": custom_providers,
        }
    except Exception as exc:
        logger.warning("model_switch: could not read config: %s", exc)
        # Honest empty state: fabrication here would mislead alias resolution and
        # the "current model" readout. Callers treat empty provider/model as
        # "unknown" and let the resolver auto-detect from the raw model input.
        return {
            "provider": "",
            "model": "",
            "base_url": "",
            "api_key": "",
            "user_providers": {},
            "custom_providers": [],
        }


def check_fn() -> bool:
    """Gate: tool is always available when the model_switch module exists."""
    try:
        from hermes_cli.model_switch import switch_model  # noqa: F401

        return True
    except ImportError:
        return False


def handle_model_switch(args: dict, task_id=None, session_id=None, **kwargs) -> str:
    """Handle a model_switch tool call from the agent.

    Args from tool schema:
        model: str — model name or alias (required)
        provider: str — explicit provider slug (optional)
        scope: str — "turn", "session", or "global" (optional, default "turn")

    Note:
        `task_id`, `session_id`, and `**kwargs` are accepted for
        runtime dispatch compatibility across Hermes versions, but are
        intentionally unused for standalone model switching.
    """
    model_input = (args.get("model") or "").strip()
    explicit_provider = (args.get("provider") or "").strip()
    scope = (args.get("scope") or "turn").strip().lower()

    # Validate scope
    if scope not in ("turn", "session", "global"):
        return tool_error(f"Invalid scope '{scope}'. Must be 'turn', 'session', or 'global'.")

    # Map scope to is_global flag
    # turn → not global (model.persist_switch_by_default controls persistence)
    # session → explicitly not global
    # global → explicitly global
    is_global = scope == "global"

    if not model_input:
        # No model specified — show current state
        cur = _read_current_config()
        return tool_result({
            "notice": "No model specified. Current state shown below.",
            "current_model": cur["model"],
            "current_provider": cur["provider"],
            "current_base_url": cur["base_url"],
            "hint": "Call model_switch with a model name or alias.",
        })

    try:
        from hermes_cli.model_switch import switch_model, ModelSwitchResult

        cur = _read_current_config()

        result: ModelSwitchResult = switch_model(
            raw_input=model_input,
            current_provider=cur["provider"],
            current_model=cur["model"],
            current_base_url=cur["base_url"],
            current_api_key=cur["api_key"],
            is_global=is_global,
            explicit_provider=explicit_provider,
            user_providers=cur["user_providers"],
            custom_providers=cur["custom_providers"],
        )

        if not result.success:
            return tool_error(
                f"Model switch failed: {result.error_message}",
                provider=result.target_provider,
                model=result.new_model,
            )

        # Build a clean response
        response = {
            "success": True,
            "new_model": result.new_model,
            "target_provider": result.target_provider,
            "provider_changed": result.provider_changed,
            "scope": scope,
            "provider_label": result.provider_label or result.target_provider,
        }

        if result.warning_message:
            response["warning"] = result.warning_message

        if result.resolved_via_alias:
            response["resolved_via_alias"] = result.resolved_via_alias

        if scope == "turn":
            response["note"] = (
                "This switch is turn-scoped. It will revert to the previous model "
                "on your next input."
            )
        elif scope == "session":
            response["note"] = (
                "This switch lasts for the session. Use /model to change back, or "
                "call model_switch again with your original model."
            )
        else:  # global
            response["note"] = "This switch is persisted to config.yaml."

        return tool_result(response)

    except ImportError as exc:
        return tool_error(
            f"model_switch module not available in this Hermes build: {exc}. "
            "This plugin requires the hermes_cli.model_switch module."
        )
    except Exception as exc:
        logger.exception("model_switch tool failed")
        return tool_error(f"model_switch tool failed: {type(exc).__name__}: {exc}")