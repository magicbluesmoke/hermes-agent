#!/usr/bin/env python3
"""
Run regression tests for complexity-routing plugin findings (t_58ca04f9)
"""
import sys
import os

# Add plugin src to path
plugin_src = "/home/michael/.hermes/plugins/kanban-complexity-routing/src"
if plugin_src not in sys.path:
    sys.path.insert(0, plugin_src)

os.chdir(plugin_src)

# Load config first to get real routing block
from hermes_cli.config import load_config
from routing import get_complexity_routing

config = load_config()
block = get_complexity_routing(config)

from routing import (
    select_target,
    _match_route,
    _fallback_target,
    classify_task,
    estimate_complexity,
)

results = []

def test(name, condition, error=""):
    results.append((name, "PASS" if condition else "FAIL", error))
    print(f"  {'✓' if condition else '✗'} {name}")
    if not condition:
        if error:
            print(f"    Error: {error}")
        else:
            print(f"    Error: assertion failed")
    return condition

print("\n" + "="*60)
print("Running complexity-routing findings regression tests")
print("="*60)

# F2 tests
print("\n--- F2: Hook behavior (returns {} when no route matches) ---")
config_disabled = {"complexity_routing": {"enabled": False}}
r = test("F2-1: Returns None when routing disabled", 
         select_target("test", config=config_disabled) is None)

r2 = test("F2-2: Returns None when no route matches", 
          select_target("what is 2+2", config=block) is None)

# F3 tests
print("\n--- F3: Fallback provider is string '0' ---")
provider = block["fallback"].get("provider")
print(f"  Fallback provider: {provider!r} (type: {type(provider).__name__})")
r3 = test("F3-1: Fallback provider is string '0'", provider == "0")
r3 &= test("F3-2: Fallback provider is not int 0", 
            not isinstance(provider, int) or provider != 0)

# Verify routes use string
routes = block.get("routes", [])
all_strings = all(isinstance(r.get("when", {}).get("provider", ""), str) for r in routes if "provider" in r.get("when", {}))
r3 &= test("F3-3: All route providers are strings", all_strings)

# F3-2: _fallback_target resolves correctly
target = _fallback_target(block)
print(f"  _fallback_target: {target}")
r3 &= test("F3-4: _fallback_target returns provider '0'", 
            target is not None and target["provider"] == "0")
r3 &= test("F3-5: _fallback_target returns model 'aux-coding-maxfit'", 
            target is not None and target["model"] == "aux-coding-maxfit")

# F4 tests
print("\n--- F4: Threshold removal allows route 1 at complexity 3 ---")
thresholds = block.get("thresholds", {})
print(f"  Thresholds: {thresholds}")
r4 = test("F4-1: thresholds.escalate is removed", "escalate" not in thresholds)

# Test route matching at complexity boundaries
task1 = "classify this document"
c1 = estimate_complexity(task1)
t1 = classify_task(task1)
route_1 = routes[0] if routes else None

print(f"  Route 1: {route_1}")
r4 &= test("F4-2: Route 1 exists with min_complexity: 3", 
            route_1 is not None and route_1["when"]["min_complexity"] == 3)
r4 &= test("F4-3: Route 1 matches classification task", 
            _match_route(routes, t1, c1) == route_1)

task2 = "analyze this code snippet and explain the logic"
c2 = estimate_complexity(task2)
t2 = classify_task(task2)
r4 &= test("F4-4: Route 1 matches analysis task", 
            _match_route(routes, t2, c2) == route_1)

task3 = "design a complex distributed system architecture"
c3 = estimate_complexity(task3)
t3 = classify_task(task3)
route_2 = routes[1] if len(routes) > 1 else None
r4 &= test("F4-5: Route 2 exists with min_complexity: 7", 
            route_2 is not None and route_2["when"]["min_complexity"] == 7)

# Full flow test
print("\n--- Full select_target flow ---")
task = "classify this document as legal or technical"
result = select_target(task, config=block)
print(f"  Result: {result}")
r = test("F4-6: select_target returns valid target for complexity 3 task", 
         result is not None and result["provider"] == "0")

# Summary
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
passed = sum(1 for _, status, _ in results if status == "PASS")
failed = sum(1 for _, status, _ in results if status == "FAIL")
print(f"Total: {passed}/{passed + failed} tests passed")

if failed > 0:
    print("\nFailed tests:")
    for name, status, error in results:
        if status == "FAIL":
            print(f"  - {name}")
    sys.exit(1)
else:
    print("\n✓ All tests passed!")
    sys.exit(0)
