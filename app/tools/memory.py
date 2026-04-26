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
from typing import Any

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
    category: str | None = None,
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


async def modify_memory(
    category: str,
    record_id: str,
    content: str = "",
    importance: int = 0,
    tags: str = "",
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Update an existing memory record by its ID.

    Empty `content` means leave text unchanged. `importance=0` means leave
    importance unchanged. Empty `tags` means leave tags unchanged.

    Args:
        category: Memory bucket (human_preferences, technical_context, ...).
        record_id: UUID of the record to update (from search_memory results).
        content: New text content. Empty = leave unchanged.
        importance: New importance score 1-5. 0 = leave unchanged.
        tags: New comma-separated tags. Empty = leave unchanged.
    """
    if tool_context is None:
        return {"status": "error", "message": "modify_memory requires tool_context"}
    svc = _resolve_memory_service(tool_context)
    if svc is None:
        return {
            "status": "error",
            "error_code": "MEMORY_SERVICE_UNAVAILABLE",
            "message": "OriMemoryService not wired on the runner.",
        }
    metadata_updates: dict[str, Any] = {}
    if importance:
        metadata_updates["importance"] = importance
    if tags:
        metadata_updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    app_name, user_id = _resolve_scope(tool_context)
    try:
        ok = await svc.update_memory_record(
            app_name=app_name,
            user_id=user_id,
            category=category.lower(),
            record_id=record_id,
            text=content if content else None,
            metadata_updates=metadata_updates or None,
        )
    except Exception as e:
        logger.exception("modify_memory failed")
        return {"status": "error", "error_code": "MEMORY_UPDATE", "message": str(e)}
    if not ok:
        return {
            "status": "error",
            "error_code": "RECORD_NOT_FOUND",
            "message": f"No record {record_id} in {category} for this user.",
        }
    return {
        "status": "success",
        "message": f"Updated memory record {record_id} in {category}.",
    }


async def delete_memory(
    category: str,
    record_id: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Delete a memory record by ID. Scoped to the current (app, user).

    Args:
        category: Memory bucket the record lives in.
        record_id: UUID of the record (from search_memory results).
    """
    if tool_context is None:
        return {"status": "error", "message": "delete_memory requires tool_context"}
    svc = _resolve_memory_service(tool_context)
    if svc is None:
        return {
            "status": "error",
            "error_code": "MEMORY_SERVICE_UNAVAILABLE",
            "message": "OriMemoryService not wired on the runner.",
        }
    app_name, user_id = _resolve_scope(tool_context)
    try:
        ok = await svc.delete_memory_record(
            app_name=app_name,
            user_id=user_id,
            category=category.lower(),
            record_id=record_id,
        )
    except Exception as e:
        logger.exception("delete_memory failed")
        return {"status": "error", "error_code": "MEMORY_DELETE", "message": str(e)}
    if not ok:
        return {
            "status": "error",
            "error_code": "RECORD_NOT_FOUND",
            "message": f"No record {record_id} in {category} for this user.",
        }
    return {
        "status": "success",
        "message": f"Deleted memory record {record_id} from {category}.",
    }


def _resolve_memory_service(tool_context: ToolContext):
    """Pull OriMemoryService off the invocation context. modify/delete need
    direct service access — they're not part of BaseMemoryService's public
    surface."""
    inv = getattr(tool_context, "_invocation_context", None) or getattr(tool_context, "invocation_context", None)
    if inv is None:
        return None
    return getattr(inv, "memory_service", None)


def _resolve_scope(tool_context: ToolContext) -> tuple[str, str]:
    """(app_name, user_id) for the current invocation, used as the
    LanceDB scope filter."""
    inv = getattr(tool_context, "_invocation_context", None) or getattr(tool_context, "invocation_context", None)
    sess = getattr(inv, "session", None) if inv else None
    return (
        getattr(sess, "app_name", "") or "",
        getattr(sess, "user_id", "") or "",
    )
