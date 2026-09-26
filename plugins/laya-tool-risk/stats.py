#!/usr/bin/env python3
"""laya-tool-risk stats: measure the auto-decision rate from decisions.jsonl.

Usage:
  /usr/bin/python3 ~/.hermes/plugins/laya-tool-risk/stats.py [--json]

Reads the decision log written by the plugin and prints auto-rate (allow+block /
total), escalation count, and breakdown by decision path. Run after a few real
sessions to verify the >=95% auto target.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

LOG = Path(__file__).resolve().parent / "decisions.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON summary")
    ap.add_argument("--log", default=str(LOG), help="path to decisions.jsonl")
    args = ap.parse_args()

    if not Path(args.log).exists():
        print(f"no decision log yet at {args.log}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(args.log) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue

    total = len(rows)
    actions = Counter(r.get("action") for r in rows)
    vias = Counter(f"{r.get('action')}:{r.get('via')}" for r in rows)
    auto = actions.get("allow", 0) + actions.get("block", 0)
    rate = (100.0 * auto / total) if total else 0.0

    if args.json:
        print(json.dumps({
            "total": total, "allow": actions.get("allow", 0),
            "block": actions.get("block", 0), "escalate": actions.get("escalate", 0),
            "auto_rate_pct": round(rate, 2), "breakdown": dict(vias.most_common()),
        }, indent=2))
        return

    print(f"decision log: {args.log}")
    print(f"total side-effect calls: {total}")
    print(f"  allow     : {actions.get('allow', 0)}")
    print(f"  block     : {actions.get('block', 0)}")
    print(f"  escalate  : {actions.get('escalate', 0)}")
    print(f"  AUTO RATE : {rate:.1f}%  (target >= 95%)")
    print("by path:")
    for k, v in vias.most_common():
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    main()
