"""Model switch tool plugin — exposes model_switch as an agent-callable tool.

Registers into the 'delegation' toolset (always enabled, conceptually fitting
alongside delegate_task for routing decisions).
"""

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from tool import SCHEMA, check_fn, handle_model_switch


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