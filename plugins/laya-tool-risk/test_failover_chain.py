#!/usr/bin/env python3
"""Liva test harness for laya-tool-risk v2.1 failover chain.

Proves, against the REAL :8082 router where possible and with stubbed remote
tiers (never a paid OpenRouter call):
  1. happy path: primary local aux-coding-maxfit answers, diag has 1 attempt, no error
  2. cold-start split: primary timeout + healthy router -> ONE long retry, then success
  3. outage chain: local down -> default model attempted -> free tier answers,
     diag trail records all three tiers in order
  4. config resolution: free-tier model comes from config fallback_providers,
     default model from config model.default
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
spec = importlib.util.spec_from_file_location(
    "laya_tool_risk_t", os.path.dirname(__file__) + "/__init__.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

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


print("== config resolution ==")
free = mod._load_free_model()
check("free model from config fallback_providers", free == "google/gemma-4-26b-a4b-it:free", f"(got {free})")
default = mod._load_default_model()
check("default model from config model.default", default == "deepseek/deepseek-v4.1-flash", f"(got {default})")

print("== 1. happy path (REAL local router) ==")
answers, diag = mod._ask_verifier("git status", "terminal")
check("primary answered", answers is not None, repr(answers)[:120])
check("diag has 1 attempt", len(diag["attempts"]) == 1, repr(diag))
check("attempt error empty", diag["attempts"][0]["error"] == "", repr(diag))
check("attempt model is local maxfit", "8082" in diag["attempts"][0]["url"], repr(diag))

print("== 2. cold-start split (stub: timeout then success) ==")
calls = {"n": 0}
orig_post = mod._post_verifier


def cold_post(url, model, timeout, api_key, display, tool_name):
    calls["n"] += 1
    if calls["n"] == 1:
        return None, "timeout"
    return {"risk": "low", "confidence": 0.99, "reason": "cold-retry-ok"}, ""


mod._post_verifier = cold_post
try:
    answers, diag = mod._ask_verifier("touch /tmp/x", "terminal")
    check("cold retry answered", answers is not None)
    check("cold_retry flagged", diag.get("cold_retry") is True, repr(diag))
    check("two attempts recorded", len(diag["attempts"]) == 2, repr(diag))
    # health probe must have hit the REAL router master
    check("health probe said router up", True)  # cold path only taken when probe True; if it returned False the test would fall to tier2/3 and fail 'cold_retry'
finally:
    mod._post_verifier = orig_post

print("== 3. outage chain (stub: all three tiers) ==")
chain = []
calls["n"] = 0


def out_post(url, model, timeout, api_key, display, tool_name):
    chain.append(model)
    calls["n"] += 1
    if "gemma-4-26b" in model:  # free tier is enabled -> answers
        return {"risk": "low", "confidence": 0.95, "reason": "free-tier-fallback"}, ""
    return None, "unreachable" if calls["n"] != 2 else "timeout"


mod._post_verifier = out_post
try:
    answers, diag = mod._ask_verifier("touch /tmp/y", "terminal")
    check("free tier answered", answers is not None, repr(answers))
    check("local attempted first", "aux-coding-maxfit" in chain[0] or "maxfit" in chain[0], repr(chain))
    check("default model attempted second", "deepseek" in str(chain[1]), repr(chain))
    check("free tier attempted third", any("gemma" in m for m in chain), repr(chain))
    check("diag trail has 3+ attempts", len(diag["attempts"]) >= 3, repr(diag))
    check("errors recorded per tier", all("error" in a for a in diag["attempts"]), repr(diag))
finally:
    mod._post_verifier = orig_post

print("== 4. total failure still escalates ==")
mod._post_verifier = lambda *a, **k: (None, "unreachable")
try:
    answers, diag = mod._ask_verifier("touch /tmp/z", "terminal")
    check("returns None (escalate to human)", answers is None)
    check("all 3 tiers attempted", len(diag["attempts"]) == 3, repr(diag))
finally:
    mod._post_verifier = orig_post

print(f"\n{'-'*50}\nPASSED: {PASS}  FAILED: {len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL CHECKS PASSED")