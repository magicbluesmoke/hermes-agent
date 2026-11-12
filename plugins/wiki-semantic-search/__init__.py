"""Wiki semantic search plugin — exposes wiki_semantic_search as an agent-callable tool."""

from __future__ import annotations

from .tool import SCHEMA, check_fn, handle_wiki_semantic_search


def register(ctx) -> None:
    ctx.register_tool(
        name="wiki_semantic_search",
        toolset="research",
        schema=SCHEMA,
        handler=handle_wiki_semantic_search,
        check_fn=check_fn,
        description="Semantic search over the wiki via sqlite-vec embeddings.",
        emoji="🔎",
    )