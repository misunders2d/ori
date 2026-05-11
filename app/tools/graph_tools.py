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
    node: str,
    max_depth: int = 2,
    limit: int = 25,
    tool_context: ToolContext = None,
) -> dict:
    """Find all nodes connected to a given memory / person / entity.

    Polymorphic — accepts any prefix-tagged id:
    - `mem_...` — starts from a Memory, returns neighbors of any label.
    - `per_...` — starts from a Person, returns neighbors of any label.
    - `ent_...` — starts from an Entity (legacy behaviour).
    - Bare name string — falls back to entity-name search.

    Use this to answer "what do you know about X?" or "what's nearby this
    memory?". Returns the subgraph around the starting node up to
    max_depth hops, with relationship types and depth on each neighbor.

    Args:
        node: A `mem_`/`per_`/`ent_` id, or a bare entity name to look up.
        max_depth: How many relationship hops to traverse (1-4, default 2).
        limit: Max results (default 25, max 100).
    """
    if isinstance(node, str) and node[:4] in ("mem_", "per_", "ent_"):
        return await graph.get_node_neighbors(node, max_depth=max_depth, limit=limit)

    # Bare name → resolve to an entity, then traverse entity-only.
    entity_id = await _resolve_entity(node)
    if not entity_id:
        return {"status": "error", "message": f"Node '{node}' not found."}
    return await graph.get_connections(entity_id, max_depth=max_depth, limit=limit)


async def find_connection_path(
    from_node: str,
    to_node: str,
    tool_context: ToolContext = None,
) -> dict:
    """Find the shortest connection path between two nodes.

    Polymorphic — both endpoints may be `mem_`/`per_`/`ent_` ids. When both
    are entity ids (or bare entity names that resolve to entities), the
    legacy entity-only shortestPath is used; otherwise the cross-label
    path is computed.

    Use this to answer 'how is X connected to Y?' — traces the relationship chain.

    Args:
        from_node: Starting node id (or entity name).
        to_node: Target node id (or entity name).
    """
    from_prefixed = isinstance(from_node, str) and from_node[:4] in ("mem_", "per_", "ent_")
    to_prefixed = isinstance(to_node, str) and to_node[:4] in ("mem_", "per_", "ent_")

    if from_prefixed and to_prefixed:
        return await graph.find_node_path(from_node, to_node)

    # At least one side is a bare name — resolve to an entity, then use
    # the entity-only path. If either resolution fails, surface a clear
    # error so the LLM can ask the user for a more specific identifier.
    from_id = from_node if from_prefixed else await _resolve_entity(from_node)
    to_id = to_node if to_prefixed else await _resolve_entity(to_node)

    if not from_id:
        return {"status": "error", "message": f"Node '{from_node}' not found."}
    if not to_id:
        return {"status": "error", "message": f"Node '{to_node}' not found."}

    # If both ended up as prefixed ids of different labels, run polymorphic.
    if isinstance(from_id, str) and isinstance(to_id, str):
        from_label = from_id[:4] in ("mem_", "per_", "ent_")
        to_label = to_id[:4] in ("mem_", "per_", "ent_")
        if from_label and to_label and (from_id[:4] != "ent_" or to_id[:4] != "ent_"):
            return await graph.find_node_path(from_id, to_id)

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


async def graph_stats(recent_mutations: int = 10, tool_context: ToolContext = None) -> dict:
    """Summarize the knowledge graph — node counts by label, edge counts by
    type, orphans, and the most recent mutations from the audit log.

    Use this to answer "what's in the knowledge base?" or "what did Ori
    learn this week?" without dumping every record.

    Args:
        recent_mutations: How many recent audit-log lines to include (max 50, default 10).
    """
    stats = await graph.get_graph_stats()
    if stats.get("status") != "success":
        return stats

    # Tail the audit JSONL for the most recent mutations.
    import json as _json
    import os as _os
    audit_path = _os.path.abspath("./data/graph_audit.jsonl")
    recent_mutations = min(max(recent_mutations, 0), 50)
    recent: list[dict] = []
    if recent_mutations and _os.path.isfile(audit_path):
        try:
            with open(audit_path) as f:
                lines = f.readlines()
            for line in lines[-recent_mutations:]:
                line = line.strip()
                if line:
                    try:
                        recent.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning("graph_stats: audit log read failed: %s", e)

    return {**stats, "recent_mutations": recent}


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


