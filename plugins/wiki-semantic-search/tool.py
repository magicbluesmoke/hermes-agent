"""Agent-callable wiki_semantic_search tool."""

from __future__ import annotations

import json
import os
import subprocess
from tools.registry import tool_result, tool_error

DSPY_PY = os.path.expanduser("~/.venvs/dspy/bin/python")
SEARCH_SCRIPT = os.path.expanduser("~/.hermes/scripts/wiki_vec_search.py")

SCHEMA = {
    "name": "wiki_semantic_search",
    "description": "Semantic search over the wiki via sqlite-vec embeddings. Returns top-k pages by cosine distance.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query."},
            "k": {"type": "integer", "description": "Number of results (default 5).", "default": 5},
        },
        "required": ["query"],
    },
}

def check_fn() -> bool:
    return os.path.isfile(DSPY_PY) and os.path.isfile(SEARCH_SCRIPT)

def handle_wiki_semantic_search(args, **kwargs) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return tool_error("query is required")
    k = int(args.get("k") or 5)
    code = (
        "import sys, json; sys.path.insert(0,'/home/michael/.hermes/scripts'); "
        "import wiki_vec_search as s; "
        "import sqlite3, sqlite_vec, os; "
        "from sentence_transformers import SentenceTransformer; "
        "DB=os.path.expanduser('~/.hermes/wiki_vec.db'); "
        "conn=sqlite3.connect(DB); sqlite_vec.load(conn); "
        "m=SentenceTransformer('all-MiniLM-L6-v2'); e=m.encode(%r).tolist(); "
        "rows=conn.execute('SELECT mm.path, mm.title, mm.teaser, v.distance FROM wiki_vec v JOIN wiki_meta mm ON mm.rowid=v.rowid WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance',(sqlite_vec.serialize_float32(e),%d)).fetchall(); "
        "print(json.dumps([{'path':r[0],'title':r[1],'teaser':r[2],'distance':r[3]} for r in rows]))"
        % (q, k)
    )
    try:
        r = subprocess.run([DSPY_PY, "-c", code], capture_output=True, text=True, timeout=120)
    except Exception as e:
        return tool_error(str(e))
    if r.returncode != 0:
        return tool_error(r.stderr.strip() or "search failed")
    try:
        results = json.loads(r.stdout.strip())
    except Exception:
        return tool_result({"raw": r.stdout.strip()})
    return tool_result({"results": results})