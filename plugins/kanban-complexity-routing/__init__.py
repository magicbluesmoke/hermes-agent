from .src.handler import handle_kanban_task_claimed, handle_delegate_task_routing  # noqa: F401

_VALID_HOOKS = None


def _delegate_hook_supported() -> bool:
    """True when the current Hermes core still exposes ``delegate_task_routing``.

    Upstream dropped the hook from VALID_HOOKS (v0.20.0, 2026-08-03); the
    kanban path is unaffected. Guard keeps the registration future-proof:
    if Nous re-adds the hook, delegation routing lights up again without a
    plugin edit.
    """
    global _VALID_HOOKS
    if _VALID_HOOKS is None:
        try:
            from hermes_cli.plugins import VALID_HOOKS as _vh
            _VALID_HOOKS = _vh
        except Exception:
            _VALID_HOOKS = frozenset()
    return "delegate_task_routing" in _VALID_HOOKS


def register(ctx):  # noqa: ANN001, ANN202
    ctx.register_hook("kanban_task_claimed", handle_kanban_task_claimed)
    if _delegate_hook_supported():
        ctx.register_hook("delegate_task_routing", handle_delegate_task_routing)
