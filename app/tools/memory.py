"""Tools for interacting with Ori's local long-term memory (LanceDB)."""

import logging
from typing import Dict, Any, List, Optional
from google.adk.tools.tool_context import ToolContext
from app.core.memory import memory

logger = logging.getLogger(__name__)

async def remember_info(
    category: str, 
    content: str, 
    importance: int = 3,
    tags: str = "",
    tool_context: ToolContext = None
) -> Dict[str, Any]:
    """
    Stores a piece of information in the long-term memory for future recall.
    
    Args:
        category: Memory lobe (human, technical, research).
        content: The fact, preference, or observation to remember.
        importance: Scale 1-5 (5 is vital).
        tags: Comma-separated descriptors.
    """
    try:
        metadata = {
            "importance": importance,
            "tags": [t.strip() for t in tags.split(",") if t.strip()]
        }
        record_id = await memory.remember(category.lower(), content, metadata)
        return {
            "status": "success",
            "message": f"Saved to {category} memory (ID: {record_id}): '{content[:50]}...'",
            "id": record_id
        }
    except Exception as e:
        return {"status": "error", "message": f"Memory storage failed: {e}"}

async def search_memory(
    query: str, 
    category: Optional[str] = None, 
    limit: int = 3,
    tool_context: ToolContext = None
) -> Dict[str, Any]:
    """
    Performs a semantic search across my internal memory banks to recall facts.
    """
    categories = [category.lower()] if category else ["human", "technical", "research"]
    all_results = []
    
    try:
        for cat in categories:
            results = await memory.search(cat, query, limit=limit)
            for r in results:
                r["category"] = cat
                all_results.append(r)
        
        # Sort by proximity if needed, but LanceDB does this per table
        # We'll just present the findings
        if not all_results:
            return {"status": "success", "message": "No matching records found in long-term memory.", "results": []}
            
        msg = f"🧠 **Recall Results for '{query}'**\n\n"
        for i, res in enumerate(all_results[:limit]):
            text = res.get("text", "")
            cat = res.get("category", "unknown")
            record_id = res.get("id", "legacy")
            msg += f"{i+1}. [{cat.upper()}] (ID: {record_id}) {text}\n"
            
        return {
            "status": "success",
            "message": msg,
            "results": all_results[:limit]
        }
    except Exception as e:
        return {"status": "error", "message": f"Memory recall failed: {e}"}

async def modify_memory(
    category: str,
    record_id: str,
    content: Optional[str] = None,
    importance: Optional[int] = None,
    tags: Optional[str] = None,
    tool_context: ToolContext = None
) -> Dict[str, Any]:
    """
    Updates an existing memory record by its ID.
    
    Args:
        category: The memory category (human, technical, research).
        record_id: The unique ID (UUID) of the record to update.
        content: New text content (optional).
        importance: New importance score 1-5 (optional).
        tags: New comma-separated tags (optional).
    """
    try:
        # Since LanceDB update is simple, we'll try to keep existing metadata for the merge
        # but the tool logic should ideally be robust.
        # For simplicity, if content/metadata is provided, it's overwritten.
        metadata = None
        if importance is not None or tags is not None:
            metadata = {}
            if importance is not None: metadata["importance"] = importance
            if tags is not None: metadata["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            
        await memory.update(category.lower(), record_id, content, metadata)
        return {
            "status": "success",
            "message": f"Updated memory record {record_id} in {category}."
        }
    except Exception as e:
        return {"status": "error", "message": f"Memory update failed: {e}"}

async def delete_memory(
    category: str,
    record_id: str,
    tool_context: ToolContext = None
) -> Dict[str, Any]:
    """
    Deletes a specific memory record by its ID.
    
    Args:
        category: The memory category (human, technical, research).
        record_id: The unique ID (UUID) of the record to delete.
    """
    try:
        await memory.forget(category.lower(), record_id)
        return {
            "status": "success",
            "message": f"Deleted memory record {record_id} from {category}."
        }
    except Exception as e:
        return {"status": "error", "message": f"Memory deletion failed: {e}"}

async def recall_technical_context(query: str, tool_context: ToolContext = None) -> Dict[str, Any]:
    """Recalls technical decisions, bug fixes, or architecture notes."""
    return await search_memory(query, category="technical", limit=5)

async def recall_human_preferences(query: str, tool_context: ToolContext = None) -> Dict[str, Any]:
    """Recalls user-specific preferences, names, or professional details."""
    return await search_memory(query, category="human", limit=5)
