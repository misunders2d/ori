"""Memory tools — thin wrappers over `tool_context.search_memory` /
`tool_context.add_memory`. The ADK 2.0 runner routes these to the
configured memory service (OriMemoryService at runtime).

Categories (Ori convention: human_preferences, technical_context,
background_tasks, etc.) are passed via `custom_metadata['category']`.
The OriMemoryService implementation routes by category to per-table
LanceDB storage.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.memory.memory_entry import MemoryEntry
from google.adk.tools.tool_context import ToolContext
from google.genai import types

logger = logging.getLogger(__name__)


async def remember_info(
    category: str,
    content: str,
    importance: int = 3,
    tags: str = "",
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Store a piece of information in long-term memory for future recall.

    Args:
        category: Memory bucket (human_preferences, technical_context,
                  background_tasks, ...).
        content: The fact, preference, or observation to remember.
        importance: Scale 1-5 (5 is vital).
        tags: Comma-separated descriptors.
    """
    if tool_context is None:
        return {"status": "error", "message": "remember_info requires tool_context"}
    try:
        custom_metadata = {
            "category": category.lower(),
            "importance": importance,
            "tags": [t.strip() for t in tags.split(",") if t.strip()],
        }
        entry = MemoryEntry(
            content=types.Content(role="user", parts=[types.Part.from_text(text=content)]),
            custom_metadata=custom_metadata,
        )
        await tool_context.add_memory([entry], custom_metadata=custom_metadata)
        return {
            "status": "success",
            "message": f"Saved to {category} memory: '{content[:50]}...'",
        }
    except Exception as e:
        logger.exception("remember_info failed")
        return {"status": "error", "error_code": "MEMORY_WRITE", "message": str(e)}


async def search_memory(
    query: str,
    category: Optional[str] = None,
    limit: int = 3,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Semantic search across long-term memory. Optional category filter."""
    if tool_context is None:
        return {"status": "error", "message": "search_memory requires tool_context"}
    try:
        resp = await tool_context.search_memory(query)
        memories = list(resp.memories) if resp and resp.memories else []
        if category:
            memories = [
                m for m in memories
                if (m.custom_metadata or {}).get("category", "").lower() == category.lower()
            ]
        memories = memories[:limit]
        if not memories:
            return {
                "status": "success",
                "message": "No matching records found in long-term memory.",
                "results": [],
            }
        out_lines = [f"🧠 Recall results for '{query}':"]
        results: list[dict[str, Any]] = []
        for i, m in enumerate(memories, start=1):
            md = m.custom_metadata or {}
            text = " ".join(p.text for p in m.content.parts if getattr(p, "text", None)) if m.content else ""
            cat = md.get("category", "unknown")
            out_lines.append(f"{i}. [{cat.upper()}] {text}")
            results.append({"text": text, "category": cat, "id": m.id, "metadata": md})
        return {"status": "success", "message": "\n".join(out_lines), "results": results}
    except Exception as e:
        logger.exception("search_memory failed")
        return {"status": "error", "error_code": "MEMORY_SEARCH", "message": str(e)}


async def recall_technical_context(query: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Recall technical decisions, bug fixes, or architecture notes."""
    return await search_memory(query, category="technical_context", limit=5, tool_context=tool_context)


async def recall_human_preferences(query: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Recall user-specific preferences, names, or professional details."""
    return await search_memory(query, category="human_preferences", limit=5, tool_context=tool_context)


# Note on update/delete: the BaseMemoryService API doesn't expose
# update_memory / delete_memory. Adding them would require subclassing.
# For the rebuild we accept this limitation — agents that need to mark a
# memory stale should write a new entry tagging the original as superseded
# rather than mutating in place. Documented in the agent instructions.
