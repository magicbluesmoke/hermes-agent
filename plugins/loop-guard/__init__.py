from __future__ import annotations
import json, logging, os, time
from pathlib import Path

logger = logging.getLogger(__name__)
STATE_FILE = Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes"))) / "loop-guard-state.json"

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"successful_turns": []}

def _save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass

_failures = 0
_turn_tools = 0

def _is_failure(result):
    if isinstance(result, dict):
        if result.get("error") or result.get("exit_code", 0) not in (0, None):
            return True
    return False

def _percentile(vals, p):
    if not vals:
        return 30
    s = sorted(vals)
    k = int(len(s) * p / 100)
    k = min(max(k, 0), len(s)-1)
    return s[k]

def pre_tool_call(**kw):
    global _turn_tools
    state = _load_state()
    budget = 1.25 * _percentile(state.get("successful_turns", []), 90) or 30
    if _failures >= 3 and _turn_tools >= 2.5 * budget:
        return {"action": "block", "message": "LOOP GUARD BACKSTOP: repeated failures + budget exceeded. Stop and summarize."}
    _turn_tools += 1

def post_tool_call(**kw):
    global _failures
    if _is_failure(kw.get("result")):
        _failures += 1

def pre_llm_call(**kw):
    state = _load_state()
    budget = 1.25 * _percentile(state.get("successful_turns", []), 90)
    if budget <= 0:
        budget = 30
    if _failures >= 3:
        if _turn_tools >= 2.5 * budget:
            return {"context": "LOOP GUARD BACKSTOP: turn budget exceeded with repeated failures. Stop and summarize."}
        if _turn_tools >= budget:
            return {"context": "LOOP GUARD: You appear stuck on repeated failures. Try a different approach."}

def on_session_end(**kw):
    global _failures, _turn_tools
    state = _load_state()
    turns = state.get("successful_turns", [])
    if _failures < 3:
        turns.append(_turn_tools)
        turns = turns[-50:]
        state["successful_turns"] = turns
        _save_state(state)
    _failures = 0
    _turn_tools = 0

def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
