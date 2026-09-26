#!/usr/bin/env python3
"""Regression test for laya-tool-risk SAFE_TERMINAL_RE (ReDoS fix 2026-09-26).

The original tail `(\\s+([^>|;&]|'[^']*'|"[^"]*")*)?$` backtracks quadratically on
commands with a long whitespace run followed by a pipe/and/semicolon/redirect
(47s @ 50k spaces; hours at 1M). The fixed helper (atomic tail + quoted-region
neutralization) must be linear and must keep the same verdicts for normal
commands, except pipeline-script commands (python3 <script>) which now
deterministically safe-pass as originally intended.

Run: python3 test_safe_terminal.py   (exit 0 = pass; needs `requests` importable
or stub it via a venv; the plugin only uses requests at runtime).
"""
import importlib.util
import re
import sys
import threading
import time
import types

HERE = __file__  # noqa: F841 (kept for clarity)

sys.modules.setdefault("requests", types.ModuleType("requests"))
spec = importlib.util.spec_from_file_location(
    "laya_tool_risk_under_test", __file__.rsplit("/", 1)[0] + "/__init__.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

OLD = re.compile(
    r"^(ls|pwd|date|df|du|free|uptime|whoami|hostname|uname|env|printenv|which|type|"
    r"echo|cat|head|tail|grep|find|wc|sort|uniq|cut|tr|sed|awk|stat|file|tree|"
    r"git\s+(status|diff|log|branch|remote\s+-v|config|show|rev-parse)\b|"
    r"python3?\s+(\""
    r"/home/michael/\.hermes/scripts/|~/.hermes/scripts/|\$HOME/.hermes/scripts/|\.hermes/scripts/)\""
    r"\S+\.py\b)"
    r"(\s+([^>|;&]|'[^']*'|\"[^\"]*\")*)?$"
)

# (command, old_verdict) — new must agree with old EXCEPT the two python3-script
# entries, which old could never match due to stray literal quotes (dead branch).
CORPUS = [
    ("ls", True), ("pwd", True), ("date", True), ("df -h", True),
    ("du -sh ~/src", True), ("free -h", True), ("uptime", True),
    ("whoami", True), ("hostname", True), ("uname -a", True), ("env", True),
    ("which python3", True), ("echo hello", True), ("echo 'a | b'", True),
    ("cat /tmp/x", True), ("head -5 /tmp/x", True), ("grep foo /tmp/x", True),
    ("grep 'a | b' file | head", False), ("grep foo /tmp/x | sort", False),
    ("find ~/src -name '*.py'", True), ("wc -l file", True), ("sort file", True),
    ("uniq -c", True), ("cut -d, -f1 f", True), ("tr a b < f", True),
    ("sed -i 's/x/y/' f", True), ("awk '{print $1}' f", True),
    ("stat file", True), ("file /tmp/x", True), ("ls | wc -l", False),
    ("echo a > /tmp/o", False), ("cat a; rm -rf /tmp/x", False),
    ("echo a && echo b", False), ("git status", True), ("git diff --stat", True),
    ("git log --oneline -5", True), ("git branch -a", True),
    ("git remote -v", True), ("git show HEAD", True),
    ("git rev-parse HEAD", True), ("ps -eo pid | head", False),
    ("grep -E 'x|y' f", True), ("sudo rm -rf /", False),
    ("rm -rf /home/michael/Desktop/x", False), ("date '+%Y-%m-%d'", True),
    ("grep a | grep b", False), ("sed 's|/a/b|/c/d|' f", True),
    ("echo $(date)", True), ("echo it's", True), ("cat 'a' | head", False),
    # python3 script entries: old_verdict False is the DEAD-BRANCH bug; new is True.
    ("python3 ~/.hermes/scripts/kanban_recovery.py", True),
    ("python3 /home/michael/.hermes/scripts/redos_probe.py", True),
]


def timed(fn, s, cap):
    box = {}

    def run():
        t0 = time.perf_counter()
        box["m"] = bool(fn(s))
        box["dt"] = time.perf_counter() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=cap)
    if th.is_alive():
        return None
    return box["m"], box["dt"] * 1000


def main():
    failures = []
    for s, old_verdict in CORPUS:
        old = timed(lambda t, s=s: OLD.match(t.strip()), s, 1.0)
        new = timed(mod._is_safe_terminal, s, 1.0)
        if new is None:
            failures.append("HUNG on %r" % s)
            continue
        new_verdict = new[0]
        if new_verdict != old_verdict:
            failures.append("verdict mismatch on %r: old=%s new=%s"
                            % (s, old and old[0], new_verdict))

    # Pathological inputs: must be FAST and correct (False = falls to verifier).
    for n in (50_000, 200_000, 1_000_000):
        s = "grep" + " " * n + "|"
        res = timed(mod._is_safe_terminal, s, 1.0)
        if res is None:
            failures.append("HUNG on 1M-space input (n=%d)" % n)
        elif res[0] is not False:
            failures.append("1M-space input classified unsafe incorrectly: %r" % res)
    for n in (50_000, 200_000):
        s = "grep" + "\t" * n + "&&"
        if timed(mod._is_safe_terminal, s, 1.0) is None:
            failures.append("HUNG on tab-run input (n=%d)" % n)

    if failures:
        print("FAIL (%d):" % len(failures))
        for f in failures:
            print("  -", f)
        return 1
    print("OK: %d verdicts match, all pathological inputs linear." % len(CORPUS))
    return 0


if __name__ == "__main__":
    sys.exit(main())