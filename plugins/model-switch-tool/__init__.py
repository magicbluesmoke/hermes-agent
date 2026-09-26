"""Model switch tool plugin — exposes model_switch as an agent-callable tool.

Registers into the 'delegation' toolset (always enabled, conceptually fitting
alongside delegate_task for routing decisions).
"""

from __future__ import annotations

from .tool import SCHEMA, check_fn, handle_model_switch


def register(ctx) -> None:
    """Register the model_switch tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="model_switch",
        toolset="delegation",
        schema=SCHEMA,
        handler=handle_model_switch,
        check_fn=check_fn,
        description="Switch the active model/provider mid-session — turn-scoped, session, or global.",
        emoji="🔀",
    )