"""Graph memory tools — entity and relationship management via Neo4j.

These tools let the agent track entities (people, projects, concepts) and their
relationships. Operates on the generic :Entity label alongside the
namespace-scoped :Memory / :Person labels that memory_tools.py manages.
"""

import logging
import uuid
from datetime import datetime

from google.adk.tools.tool_context import ToolContext

from app.core import graph

logger = logging.getLogger(__name__)


def _get_user_id(tool_context: ToolContext | None) -> str:
    if not tool_context:
        return "unknown"
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    return state.get("user_id", "unknown")


# ---------------------------------------------------------------------------
# Entity tools
# ---------------------------------------------------------------------------

async def add_entity(
    name: str,
    entity_type: str,
    properties: str = "{}",
    pinecone_id: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Create or update an entity in the knowledge graph.

    Use this to register people, companies, projects, products, concepts, or events
    that the agent should track relationships for.

    Args:
        name: Human-readable name (e.g. 'Alice Chen', 'Project Atlas', 'DHL').
        entity_type: Category — one of: person, company, project, product, concept, event.
        properties: Optional JSON string of extra properties, e.g. '{"role": "supplier", "email": "a@b.com"}'.
        pinecone_id: Optional Pinecone record ID to cross-reference (e.g. 'per_2026_04_08_abc123').
    """
    import json

    if not name or not entity_type:
        return {"status": "error", "message": "'name' and 'entity_type' are required."}

    allowed_types = {"person", "company", "project", "product", "concept", "event"}
    if entity_type not in allowed_types:
        return {"status": "error", "message": f"entity_type must be one of: {', '.join(sorted(allowed_types))}"}

    try:
        props = json.loads(properties) if isinstance(properties, str) and properties else {}
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON in 'properties'."}

    entity_id = f"ent_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"
    user_id = _get_user_id(tool_context)

    result = await graph.upsert_entity(
        entity_id=entity_id,
        name=name,
        entity_type=entity_type,
        properties=props,
        pinecone_id=pinecone_id or None,
        author=user_id,
    )
    return result


async def link_entities(
    from_entity: str,
    to_entity: str,
    relation_type: str,
    properties: str = "{}",
    tool_context: ToolContext = None,
) -> dict:
    """Create a relationship between two entities in the knowledge graph.

    Use this to record that two entities are connected (e.g. Alice works_with Bob,
    Project Atlas uses DHL, pricing_incident related_to SupplierCo).

    Args:
        from_entity: Source entity ID or name. If a name, the closest match will be used.
        to_entity: Target entity ID or name.
        relation_type: Relationship label (e.g. 'works_with', 'manages', 'supplies', 'related_to', 'involved_in').
        properties: Optional JSON string of edge properties, e.g. '{"context": "since Q2 2025"}'.
    """
    import json

    if not from_entity or not to_entity or not relation_type:
        return {"status": "error", "message": "from_entity, to_entity, and relation_type are all required."}

    try:
        props = json.loads(properties) if isinstance(properties, str) and properties else {}
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON in 'properties'."}

    # Resolve names to entity IDs if needed
    from_id = await _resolve_entity(from_entity)
    to_id = await _resolve_entity(to_entity)

    if not from_id:
        return {"status": "error", "message": f"Entity '{from_entity}' not found. Create it first with add_entity."}
    if not to_id:
        return {"status": "error", "message": f"Entity '{to_entity}' not found. Create it first with add_entity."}

    user_id = _get_user_id(tool_context)
    return await graph.add_relationship(
        from_entity_id=from_id,
        to_entity_id=to_id,
        relation_type=relation_type,
        properties=props,
        author=user_id,
    )


async def query_connections(
    entity: str,
    max_depth: int = 2,
    limit: int = 25,
    tool_context: ToolContext = None,
) -> dict:
    """Find all entities connected to a given entity.

    Use this to explore relationships — 'who/what is connected to X?'
    Returns the subgraph around the entity up to max_depth hops.

    Args:
        entity: Entity ID or name to explore from.
        max_depth: How many relationship hops to traverse (1-4, default 2).
        limit: Max results (default 25, max 100).
    """
    entity_id = await _resolve_entity(entity)
    if not entity_id:
        return {"status": "error", "message": f"Entity '{entity}' not found."}
    return await graph.get_connections(entity_id, max_depth=max_depth, limit=limit)


async def find_connection_path(
    from_entity: str,
    to_entity: str,
    tool_context: ToolContext = None,
) -> dict:
    """Find the shortest connection path between two entities.

    Use this to answer 'how is X connected to Y?' — traces the relationship chain.

    Args:
        from_entity: Starting entity ID or name.
        to_entity: Target entity ID or name.
    """
    from_id = await _resolve_entity(from_entity)
    to_id = await _resolve_entity(to_entity)

    if not from_id:
        return {"status": "error", "message": f"Entity '{from_entity}' not found."}
    if not to_id:
        return {"status": "error", "message": f"Entity '{to_entity}' not found."}

    return await graph.find_path(from_id, to_id)


async def entity_timeline(
    entity: str,
    tool_context: ToolContext = None,
) -> dict:
    """Get the full relationship history of an entity, ordered by time.

    Use this to see when relationships were established and how an entity's
    connections evolved over time.

    Args:
        entity: Entity ID or name.
    """
    entity_id = await _resolve_entity(entity)
    if not entity_id:
        return {"status": "error", "message": f"Entity '{entity}' not found."}
    return await graph.get_entity_history(entity_id)


async def search_graph(
    query: str,
    entity_type: str = "",
    limit: int = 10,
    tool_context: ToolContext = None,
) -> dict:
    """Search for entities in the knowledge graph by name.

    Args:
        query: Text to search for in entity names (case-insensitive).
        entity_type: Optional filter — person, company, project, product, concept, event.
        limit: Max results (default 10).
    """
    return await graph.search_entities(
        query_text=query,
        entity_type=entity_type or None,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _resolve_entity(identifier: str) -> str | None:
    """Resolve a name or ID to an entity_id. Returns None if not found."""
    if not identifier:
        return None

    # If it looks like an entity ID, try direct lookup first
    if identifier.startswith("ent_") or identifier.startswith("per_"):
        entity = await graph.get_entity(identifier)
        if entity:
            return identifier

    # Search by name
    result = await graph.search_entities(identifier, limit=1)
    if result.get("status") == "success" and result.get("entities"):
        return result["entities"][0]["entity_id"]

    # If the original identifier looked like an ID but wasn't found, return None
    return None


