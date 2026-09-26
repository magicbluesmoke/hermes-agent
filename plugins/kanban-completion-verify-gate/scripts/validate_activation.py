"""Standalone validation for kanban-completion-verify-gate.

Runs inside an agent session with Hermes tools available via `hermes_tools`,
so plugin discovery/hook registration can actually be exercised. No board
mutation happens unless `apply_mutations` is passed explicitly.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Resolve plugin root from this file's location.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "kanban-completion-verify-gate"
PLUGIN_NAME = "kanban-completion-verify-gate"


def _plugin_discovery_paths():
    return [
        PLUGIN_ROOT,
        Path.home() / ".hermes" / "plugins" / PLUGIN_NAME,
        Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "plugins" / PLUGIN_NAME,
    ]


def _print(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    from hermes_tools import terminal  # noqa: F401 - path probe only
    _print(f"validate plugin root: {PLUGIN_ROOT}")
    for path in _plugin_discovery_paths():
        _print(f"checking: {path} exists={path.exists()}")

    manifest = PLUGIN_ROOT / "plugin.yaml"
    module = PLUGIN_ROOT / "__init__.py"
    _print(f"plugin.yaml exists={manifest.exists()}")
    _print(f"__init__.py exists={module.exists()}")
    if not manifest.exists() or not module.exists():
        return 2

    text = manifest.read_text(encoding="utf-8")
    for token in ["name: kanban-completion-verify-gate", "kanban_task_completed"]:
        _print(f"manifest contains {token!r}: {token in text}")

    spec = importlib.util.spec_from_file_location(PLUGIN_NAME, module)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        _print(f"module import failed: {exc}")
        return 3

    _print(f"register symbol present: {hasattr(mod, 'register')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
