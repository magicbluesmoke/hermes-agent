from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict

from tools.registry import tool_result, tool_error

# Last-resort bundled value — used ONLY when env override and config both fail.
_DEFAULT_MCP_BASE_URL = "http://localhost:5000"


def _resolve_base_url() -> str:
    """MCP file-tools endpoint base: env override > config entry > bundled last resort.

    Reads `plugins.entries.mcp-file-tools.base_url` from the active profile's
    config.yaml whenever the env var is unset, so the endpoint can move without
    a code edit."""
    url = os.environ.get("MCP_FILE_TOOLS_URL", "").strip()
    if url:
        return url.rstrip("/")
    try:
        cfg_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"
        if not cfg_path.exists():
            cfg_path = Path.home() / ".hermes" / "config.yaml"
        if cfg_path.exists():
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("mcp-file-tools") or {}
            url = str(entry.get("base_url") or "").strip()
            if url:
                return url.rstrip("/")
    except Exception:
        pass  # config must never gate the plugin
    return _DEFAULT_MCP_BASE_URL

def _post_json(url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """POST JSON to URL and return parsed JSON response."""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return tool_error(f"HTTP {e.code}: {e.read().decode('utf-8')}", code=e.code)
    except urllib.error.URLError as e:
        return tool_error(f"URL Error: {str(e.reason)}", code=500)
    except Exception as e:
        return tool_error(f"Unexpected error: {str(e)}", code=500)

def mcp_search_files_handler(args: dict) -> str:
    """Handle search-files MCP endpoint call."""
    # Extract arguments
    query = args.get("query", "")
    directory = args.get("directory", ".")
    content_search = args.get("content_search", True)
    file_glob = args.get("file_glob")
    limit = args.get("limit", 50)
    offset = args.get("offset", 0)
    order = args.get("order", "discovery")
    output_mode = args.get("output_mode", "content")
    context_lines = args.get("context", 0)
    
    # Build payload for MCP endpoint
    payload = {
        "query": query,
        "directory": directory,
        "content_search": content_search,
        "limit": limit,
        "offset": offset,
        "order": order,
        "output_mode": output_mode
    }
    if file_glob is not None:
        payload["file_glob"] = file_glob
    if context_lines != 0:
        payload["context"] = context_lines
    
    result = _post_json(_resolve_base_url() + "/api/search-files", payload)
    # If tool_error was returned, it's already a string; otherwise dump JSON
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)
    return json.dumps(result)

def mcp_read_file_handler(args: dict) -> str:
    """Handle read-file MCP endpoint call."""
    file_path = args.get("path", "")
    offset = args.get("offset", 1)
    limit = args.get("limit", 2000)
    
    payload = {
        "file_path": file_path,
        "offset": offset,
        "limit": limit
    }
    
    result = _post_json(_resolve_base_url() + "/api/read-file", payload)
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result)
    return json.dumps(result)

def register(ctx) -> None:
    """Register MCP file tools with Hermes."""
    # Search files tool
    ctx.register_tool(
        name="mcp_search_files",
        toolset="file",  # Using existing file toolset
        schema={
            "name": "mcp_search_files",
            "description": "Search file contents or find files by name via MCP endpoint (http://<host>:<port>/api/search-files)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Regex pattern for content search, or glob pattern for file search"},
                    "directory": {"type": "string", "description": "Directory to search in (default: current directory)", "default": "."},
                    "content_search": {"type": "boolean", "description": "Search inside file contents (default: true)", "default": True},
                    "file_glob": {"type": "string", "description": "Filter files by pattern (e.g., '*.py')"},
                    "limit": {"type": "integer", "description": "Maximum number of results (default: 50)", "default": 50},
                    "offset": {"type": "integer", "description": "Skip first N results (default: 0)", "default": 0},
                    "order": {"type": "string", "enum": ["discovery", "modified"], "description": "File-search order (default: discovery)", "default": "discovery"},
                    "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format (default: content)", "default": "content"},
                    "context": {"type": "integer", "description": "Number of context lines before/after each match (default: 0)", "default": 0}
                },
                "required": ["query"]
            }
        },
        handler=mcp_search_files_handler,
        description="Search files via MCP endpoint",
        emoji="🔍"
    )
    
    # Read file tool
    ctx.register_tool(
        name="mcp_read_file",
        toolset="file",
        schema={
            "name": "mcp_read_file",
            "description": "Read a text file with line numbers via MCP endpoint (http://<host>:<port>/api/read-file)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read"},
                    "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1},
                    "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 2000)", "default": 2000}
                },
                "required": ["path"]
            }
        },
        handler=mcp_read_file_handler,
        description="Read file via MCP endpoint",
        emoji="📄"
    )