from .src.handler import handle_kanban_task_claimed  # noqa: F401


def register(ctx):  # noqa: ANN001, ANN202
    ctx.register_hook("kanban_task_claimed", handle_kanban_task_claimed)
