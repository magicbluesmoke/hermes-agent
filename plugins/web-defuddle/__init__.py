"""defuddle web extract provider plugin.

Registers a WebSearchProvider whose ``extract`` capability shells out to the
local ``defuddle`` CLI (``defuddle parse <url> [-m] -j``). Search is
unsupported — pair with ``web.backend: ddgs`` for search. No API key, free,
~0.3s per page, no content cap. Works on server-rendered pages only.

Author: Michael Anselmi. Lives at ~/.hermes/plugins/web-defuddle/ (survives
`hermes update`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List

from plugins.web._common import BaseWebSearchProvider, document, page_error, setup_schema
from tools.interrupt import is_interrupted
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

_EXTRACT_TIMEOUT_SECS = 60


def _defuddle_bin() -> str:
    return shutil.which("defuddle") or ""


class DefuddleWebSearchProvider(BaseWebSearchProvider):
    """Extract-only provider backed by the local defuddle CLI."""

    NAME = "defuddle"
    DISPLAY_NAME = "defuddle (local CLI)"
    EXTRACT = True

    def is_available(self) -> bool:
        """True when the defuddle CLI is on PATH. No network I/O."""
        return bool(_defuddle_bin())

    def supports_search(self) -> bool:
        return False

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "defuddle (local CLI)", "free · no key",
            "Extract page content to Markdown/HTML via the local defuddle CLI — "
            "no API key, works on server-rendered pages.",
        )

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Per-URL defuddle parse; failures become entries with an ``error`` field.

        ``format``: "markdown" (default) | "html".
        """
        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        fmt = kwargs.get("format")
        return [await self._extract_one(url, fmt) for url in urls]

    async def _extract_one(self, url: str, fmt: Any) -> Dict[str, Any]:
        if blocked := check_website_access(url):
            logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
            return page_error(url, blocked["message"])
        cmd = [_defuddle_bin(), "parse", url, "-j"]
        if fmt != "html":
            cmd.append("-m")
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._run, cmd, url), timeout=_EXTRACT_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            logger.warning("defuddle parse timed out for %s after %ds", url, _EXTRACT_TIMEOUT_SECS)
            return page_error(url, f"defuddle parse timed out after {_EXTRACT_TIMEOUT_SECS}s")
        except Exception as exc:  # noqa: BLE001 — per-URL failures are entries, never raised
            logger.debug("defuddle parse failed for %s: %s", url, exc)
            return page_error(url, str(exc))

    @staticmethod
    def _run(cmd: List[str], url: str) -> Dict[str, Any]:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_EXTRACT_TIMEOUT_SECS
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or f"exit code {proc.returncode}"
            raise RuntimeError(f"defuddle parse failed: {detail[:300]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"defuddle returned invalid JSON: {proc.stdout[:200]!r}") from exc
        content = payload.get("content") or ""
        if not content:
            raise RuntimeError("defuddle returned empty content")
        return document(url, payload.get("title") or "", content)


def register(ctx) -> None:
    ctx.register_web_search_provider(DefuddleWebSearchProvider())
