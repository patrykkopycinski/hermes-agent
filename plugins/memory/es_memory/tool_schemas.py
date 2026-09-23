"""OpenAI function-calling schemas for the ``es_memory`` tools (pure data)."""

from __future__ import annotations

from typing import Any

_KINDS = ["turn", "fact", "preference", "decision"]


def _schema(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}


TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    _schema(
        "es_memory_remember",
        "Persist a durable fact, preference, or decision to Elasticsearch long-term memory.",
        {"content": {"type": "string", "description": "The fact to remember."},
         "kind": {"type": "string", "enum": _KINDS, "description": "Category (default: fact)."},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels for later filtering."}},
        ("content",),
    ),
    _schema(
        "es_memory_search",
        "Search stored memories. Uses semantic retrieval when available, otherwise BM25.",
        {"query": {"type": "string", "description": "What to search for."},
         "kind": {"type": "string", "enum": _KINDS, "description": "Restrict to one category."},
         "top_k": {"type": "integer", "description": "Max results (default: 5, max: 25)."}},
        ("query",),
    ),
    _schema(
        "es_memory_list",
        "List the most recently stored memories, newest first.",
        {"kind": {"type": "string", "enum": _KINDS, "description": "Restrict to one category."},
         "limit": {"type": "integer", "description": "Max results (default: 20, max: 100)."}},
    ),
    _schema(
        "es_memory_forget",
        "Delete one memory by its id (as returned by es_memory_search or es_memory_list).",
        {"memory_id": {"type": "string", "description": "Memory id to delete."}},
        ("memory_id",),
    ),
    _schema(
        "es_memory_status",
        "Report cluster reachability, the active index, the retrieval mode, and how many memories are stored.",
    ),
)
