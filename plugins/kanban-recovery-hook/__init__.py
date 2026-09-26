"""kanban-recovery-hook plugin.

Cron-free recovery: runs the kanban recovery scan (kanban_recovery.py) on
every dispatcher tick via the on_kanban_dispatch_tick hook. Replaces the
retired kanban-recovery-watchdog cron (every 15m) with an event-driven
scan that fires whenever the gateway dispatcher ticks (~60s) — no cron
needed, no LLM needed.

Design notes:
- on_kanban_dispatch_tick fires strictly AFTER the dispatch lock is
  released (per the #64231 disposition), so a slow scan never extends the
  writer critical section.
- The scan is skipped for idle ticks (outcome == "idle") to avoid needless
  full-board scans when nothing dispatched. When the dispatcher is active
  (outcome "ok" or "skipped_locked"), run the scan.
- Kanban recovery itself is idempotent and safe to run repeatedly: it
  fingerprints failures, hard-gates escalation spam, and artifact-first
  completes recoverable tasks. Running it more often than the old 15m cron
  only makes recovery faster.
- Hermes-home resolution mirrors kanban_recovery.py: env override wins,
  else ~/.hermes on Linux, %LOCALAPPDATA%/hermes on Windows.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Cooldown: never run the scan more than once per N seconds per process.
# The dispatcher ticks ~every 60s; this guards against a tight-loop gateway
# that could otherwise hammer the scan. 45s < 60s tick so we never skip a
# real tick, but a 15s tick loop won't double-run.
MIN_SCAN_INTERVAL = 45.0


def _resolve_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
    return Path.home() / ".hermes"


HERMES_HOME = _resolve_hermes_home()
RECOVERY_SCRIPT = HERMES_HOME / "scripts" / "kanban_recovery.py"

_last_scan_at: float = 0.0


def on_kanban_dispatch_tick(**kwargs: dict) -> None:
    """Run the recovery scan after a dispatcher tick that did work."""
    global _last_scan_at

    outcome = kwargs.get("outcome", "idle")
    # Idle ticks = nothing dispatched = no reason to scan (board state
    # unchanged by this tick). Still scan on "skipped_locked" — the lock
    # may have hidden blocked/stalled work that the next tick should see.
    if outcome == "idle":
        return

    now = time.monotonic()
    if now - _last_scan_at < MIN_SCAN_INTERVAL:
        return
    _last_scan_at = now

    if not RECOVERY_SCRIPT.exists():
        logger.warning(
            "kanban-recovery-hook: recovery script missing at %s — skipping", RECOVERY_SCRIPT
        )
        return

    try:
        result = subprocess.run(
            [sys.executable, str(RECOVERY_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (result.stdout or "").strip()
        if out and out != "OK":
            logger.info("kanban-recovery-hook: scan output:\n%s", out)
    except subprocess.TimeoutExpired:
        logger.warning("kanban-recovery-hook: recovery scan timed out (300s) — skipping")


def register(ctx) -> None:
    """Register the dispatch-tick hook. Idempotent (plugin re-registration)."""
    ctx.register_hook("on_kanban_dispatch_tick", on_kanban_dispatch_tick)
