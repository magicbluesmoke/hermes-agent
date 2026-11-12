#!/usr/bin/env python3
"""Backstop-aware terminal policy (O2) harness for laya-tool-risk.

Locks in the v2.4 split: when the whole verifier chain yields no verdict,
tools a native floor also covers (FAIL_OPEN_TOOLS = terminal, execute_code)
FAIL OPEN (on_pre_tool_call returns None), and every other side-effect tool
FAILS TO REVIEW (returns the approve directive with rule_key
`laya-unverified-<tool>`). Both branches are exercised end-to-end through
on_pre_tool_call with a stubbed verifier (never a real model call).

Run:  ~/.hermes/hermes-agent/.venv/bin/python test_backstop_policy.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_LOG = tempfile.mktemp(suffix="-o2.jsonl")
os.environ["LAYA_TOOL_RISK_LOG"] = _LOG  # must be set before import (module-level)

spec = importlib.util.spec_from_file_location("laya_tool_risk_o2", HERE + "/__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod._emit_cli_warning = lambda note: None  # don't touch the real CLI renderer

PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL {name} {extra}")


def call(tool, args):
    return mod.on_pre_tool_call(tool_name=tool, args=args, tool_call_id=f"tcid-{tool}")


# Non-safe, non-catastrophic payloads so the call reaches the verifier branch.
ARGS = {
    "terminal": {"command": "git push origin main"},
    "execute_code": {"code": "print(1)"},
    "write_file": {"path": "/home/michael/tmp/x.txt", "content": "hi"},
    "patch": {"path": "/home/michael/tmp/x.txt", "old_string": "a", "new_string": "b"},
    "browser_exec": {"code": "go('https://example.com')"},
    "skill_manage": {"operations": [{"action": "patch", "name": "x"}]},
    "cronjob_manage": {"action": "list"},
    "process_manage": {"action": "poll"},
    "browser_vault_fill": {"handle": "vault_x"},
    "browser_vault_save_login": {"label": "x"},
}
TOOLS = list(ARGS)

print("== FAIL_OPEN_TOOLS set ==")
check("terminal fails open", "terminal" in mod.FAIL_OPEN_TOOLS)
check("execute_code fails open", "execute_code" in mod.FAIL_OPEN_TOOLS)
check("write_file reviewed", "write_file" not in mod.FAIL_OPEN_TOOLS)


def expect_split(tag, stub):
    print(f"== {tag}: verifier yields no verdict -> per-tool terminal state ==")
    orig = mod._ask_verifier
    mod._ask_verifier = stub
    try:
        for tool in TOOLS:
            out = call(tool, ARGS[tool])
            if tool in mod.FAIL_OPEN_TOOLS:
                check(f"{tag}: {tool} FAILS OPEN (allow)", out is None, repr(out))
            else:
                ok = (isinstance(out, dict) and out.get("action") == "approve"
                      and out.get("rule_key") == f"laya-unverified-{tool}")
                check(f"{tag}: {tool} FAILS TO REVIEW", ok, repr(out))
    finally:
        mod._ask_verifier = orig


# Branch A: verifier-down (returns None)
expect_split("verifier-down", lambda display, tool: (None, {"attempts": [], "elapsed_s": 0}))
# Branch B: ambiguous verdict (returns a dict that falls through the thresholds)
expect_split("ambiguous", lambda display, tool: ({"risk": "???", "confidence": 0.1}, {}))

print("== decisions.jsonl tags ==")
rows = [json.loads(l) for l in open(_LOG) if l.strip()]
vias = sorted({r.get("via") for r in rows})
check("down-failopen tag present", "verifier-down-failopen" in vias, repr(vias))
check("down-review tag present", "verifier-down-review" in vias, repr(vias))
check("ambiguous-failopen tag present", "verifier-ambiguous-failopen" in vias, repr(vias))
check("ambiguous-review tag present", "verifier-ambiguous-review" in vias, repr(vias))
for r in rows:
    if r.get("action") == "escalate":
        check(f"escalate row carries tool {r.get('tool')}", r.get("tool") in mod.SIDE_EFFECT_TOOLS
              and r.get("tool") not in mod.FAIL_OPEN_TOOLS)

os.unlink(_LOG)
print(f"\n{'-'*50}\nPASSED: {PASS}  FAILED: {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")
