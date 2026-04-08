"""Neo4j Aura graph database client for entity/relationship tracking.

Provides async wrappers around the Neo4j Python driver for:
- Entity CRUD (people, concepts, projects, etc.)
- Relationship management with temporal metadata
- Graph traversal queries (connections, paths)
- Cross-referencing with Pinecone record IDs
"""

import logging
import os
from datetime import datetime, timezone

from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_driver = None


def _get_driver():
    """Lazily initialize and return the async Neo4j driver."""
    global _driver
    if _driver is not None:
        return _driver

    uri = os.environ.get("NEO4J_URI", "")
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")

    if not uri or not password:
        return None

    _driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    logger.info("Neo4j driver initialized for %s", uri)
    return _driver


async def close():
    """Shut down the driver (call on app teardown)."""
    global _driver
    if _driver:
        await _driver.close()
        _driver = None


def is_configured() -> bool:
    """Check if Neo4j credentials are present in environment."""
    return bool(os.environ.get("NEO4J_URI")) and bool(os.environ.get("NEO4J_PASSWORD"))


# ---------------------------------------------------------------------------
# Entity operations
# ---------------------------------------------------------------------------

async def upsert_entity(
    entity_id: str,
    name: str,
    entity_type: str,
    properties: dict | None = None,
    pinecone_id: str | None = None,
    author: str = "unknown",
) -> dict:
    """Create or update an entity node.

    Args:
        entity_id: Unique identifier (e.g. 'per_2026_04_08_abc123' or generated).
        name: Human-readable name.
        entity_type: Label — person, project, concept, product, company, event, etc.
        properties: Optional dict of extra properties to store on the node.
        pinecone_id: Optional Pinecone record ID for cross-referencing.
        author: Who created this — user ID, 'agent', or 'agent:auto'.
    """
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "NEO4J_URI/NEO4J_PASSWORD not configured."}

    now = datetime.now(timezone.utc).isoformat()
    props = {
        "entity_id": entity_id,
        "name": name,
        "entity_type": entity_type,
        "author": author,
        "updated_at": now,
        **(properties or {}),
    }
    if pinecone_id:
        props["pinecone_id"] = pinecone_id

    query = """
    MERGE (e:Entity {entity_id: $entity_id})
    ON CREATE SET e += $props, e.created_at = $now
    ON MATCH SET e += $props
    RETURN e.entity_id AS entity_id, e.name AS name
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, entity_id=entity_id, props=props, now=now)
            record = await result.single()
            return {
                "status": "success",
                "entity_id": record["entity_id"],
                "name": record["name"],
            }
    except Exception as e:
        logger.error("Neo4j upsert_entity error: %s", e)
        return {"status": "error", "message": str(e)}


async def get_entity(entity_id: str) -> dict | None:
    """Fetch a single entity by ID."""
    driver = _get_driver()
    if not driver:
        return None

    query = "MATCH (e:Entity {entity_id: $entity_id}) RETURN e"
    try:
        async with driver.session() as session:
            result = await session.run(query, entity_id=entity_id)
            record = await result.single()
            if record:
                return dict(record["e"])
            return None
    except Exception as e:
        logger.error("Neo4j get_entity error: %s", e)
        return None


async def delete_entity(entity_id: str) -> dict:
    """Delete an entity and all its relationships."""
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    query = "MATCH (e:Entity {entity_id: $entity_id}) DETACH DELETE e RETURN count(e) AS deleted"
    try:
        async with driver.session() as session:
            result = await session.run(query, entity_id=entity_id)
            record = await result.single()
            return {"status": "success", "deleted": record["deleted"]}
    except Exception as e:
        logger.error("Neo4j delete_entity error: %s", e)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Relationship operations
# ---------------------------------------------------------------------------

async def add_relationship(
    from_entity_id: str,
    to_entity_id: str,
    relation_type: str,
    properties: dict | None = None,
    author: str = "unknown",
) -> dict:
    """Create or update a relationship between two entities.

    Args:
        from_entity_id: Source entity ID.
        to_entity_id: Target entity ID.
        relation_type: Relationship label (e.g. 'works_with', 'manages', 'related_to').
        properties: Optional dict of edge properties (context, notes, etc.).
        author: Who created this relationship.
    """
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    now = datetime.now(timezone.utc).isoformat()
    props = {
        "author": author,
        "updated_at": now,
        **(properties or {}),
    }

    # Use APOC-free approach: dynamic rel type via string concatenation in Cypher
    # Neo4j doesn't allow parameterized relationship types, so we sanitize and inject
    safe_rel = "".join(c for c in relation_type.upper().replace(" ", "_") if c.isalnum() or c == "_")
    if not safe_rel:
        return {"status": "error", "message": "Invalid relation_type."}

    query = f"""
    MATCH (a:Entity {{entity_id: $from_id}})
    MATCH (b:Entity {{entity_id: $to_id}})
    MERGE (a)-[r:{safe_rel}]->(b)
    ON CREATE SET r += $props, r.created_at = $now
    ON MATCH SET r += $props
    RETURN a.name AS from_name, b.name AS to_name, type(r) AS rel_type
    """
    try:
        async with driver.session() as session:
            result = await session.run(
                query, from_id=from_entity_id, to_id=to_entity_id, props=props, now=now
            )
            record = await result.single()
            if not record:
                return {
                    "status": "error",
                    "message": "One or both entities not found. Create them first.",
                }
            return {
                "status": "success",
                "from": record["from_name"],
                "to": record["to_name"],
                "relationship": record["rel_type"],
            }
    except Exception as e:
        logger.error("Neo4j add_relationship error: %s", e)
        return {"status": "error", "message": str(e)}


async def remove_relationship(
    from_entity_id: str, to_entity_id: str, relation_type: str | None = None
) -> dict:
    """Remove relationship(s) between two entities. If relation_type is None, removes all."""
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    if relation_type:
        safe_rel = "".join(c for c in relation_type.upper().replace(" ", "_") if c.isalnum() or c == "_")
        query = f"""
        MATCH (a:Entity {{entity_id: $from_id}})-[r:{safe_rel}]->(b:Entity {{entity_id: $to_id}})
        DELETE r RETURN count(r) AS deleted
        """
    else:
        query = """
        MATCH (a:Entity {entity_id: $from_id})-[r]->(b:Entity {entity_id: $to_id})
        DELETE r RETURN count(r) AS deleted
        """
    try:
        async with driver.session() as session:
            result = await session.run(query, from_id=from_entity_id, to_id=to_entity_id)
            record = await result.single()
            return {"status": "success", "deleted": record["deleted"]}
    except Exception as e:
        logger.error("Neo4j remove_relationship error: %s", e)
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Query operations
# ---------------------------------------------------------------------------

async def get_connections(entity_id: str, max_depth: int = 2, limit: int = 50) -> dict:
    """Get all entities connected to a given entity within max_depth hops.

    Returns nodes and edges for the subgraph around the entity.
    """
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    max_depth = min(max(max_depth, 1), 4)  # cap at 4 hops
    limit = min(max(limit, 1), 100)

    query = f"""
    MATCH path = (start:Entity {{entity_id: $entity_id}})-[*1..{max_depth}]-(connected:Entity)
    WITH connected, relationships(path) AS rels, length(path) AS depth
    ORDER BY depth
    LIMIT $limit
    RETURN connected.entity_id AS entity_id,
           connected.name AS name,
           connected.entity_type AS entity_type,
           connected.pinecone_id AS pinecone_id,
           depth,
           [r IN rels | {{type: type(r), from: startNode(r).entity_id, to: endNode(r).entity_id}}] AS path_rels
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, entity_id=entity_id, limit=limit)
            records = [dict(r) async for r in result]

        if not records:
            return {"status": "success", "connections": [], "message": "No connections found."}

        return {"status": "success", "count": len(records), "connections": records}
    except Exception as e:
        logger.error("Neo4j get_connections error: %s", e)
        return {"status": "error", "message": str(e)}


async def find_path(from_entity_id: str, to_entity_id: str, max_depth: int = 5) -> dict:
    """Find the shortest path between two entities."""
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    max_depth = min(max(max_depth, 1), 8)
    query = f"""
    MATCH path = shortestPath(
        (a:Entity {{entity_id: $from_id}})-[*..{max_depth}]-(b:Entity {{entity_id: $to_id}})
    )
    RETURN [n IN nodes(path) | {{entity_id: n.entity_id, name: n.name, type: n.entity_type}}] AS nodes,
           [r IN relationships(path) | {{type: type(r), from: startNode(r).entity_id, to: endNode(r).entity_id}}] AS edges,
           length(path) AS hops
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, from_id=from_entity_id, to_id=to_entity_id)
            record = await result.single()
            if not record:
                return {"status": "success", "path": None, "message": "No path found."}
            return {
                "status": "success",
                "hops": record["hops"],
                "nodes": record["nodes"],
                "edges": record["edges"],
            }
    except Exception as e:
        logger.error("Neo4j find_path error: %s", e)
        return {"status": "error", "message": str(e)}


async def search_entities(
    query_text: str, entity_type: str | None = None, limit: int = 10
) -> dict:
    """Search entities by name (case-insensitive contains match).

    Args:
        query_text: Text to search for in entity names.
        entity_type: Optional filter by entity type.
        limit: Max results (default 10).
    """
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    limit = min(max(limit, 1), 50)

    if entity_type:
        query = """
        MATCH (e:Entity)
        WHERE toLower(e.name) CONTAINS toLower($query_text)
          AND e.entity_type = $entity_type
        RETURN e.entity_id AS entity_id, e.name AS name, e.entity_type AS entity_type,
               e.pinecone_id AS pinecone_id, e.author AS author
        LIMIT $limit
        """
        params = {"query_text": query_text, "entity_type": entity_type, "limit": limit}
    else:
        query = """
        MATCH (e:Entity)
        WHERE toLower(e.name) CONTAINS toLower($query_text)
        RETURN e.entity_id AS entity_id, e.name AS name, e.entity_type AS entity_type,
               e.pinecone_id AS pinecone_id, e.author AS author
        LIMIT $limit
        """
        params = {"query_text": query_text, "limit": limit}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            records = [dict(r) async for r in result]
        return {"status": "success", "count": len(records), "entities": records}
    except Exception as e:
        logger.error("Neo4j search_entities error: %s", e)
        return {"status": "error", "message": str(e)}


async def get_entity_history(entity_id: str) -> dict:
    """Get all relationships for an entity with temporal metadata, ordered by creation time."""
    driver = _get_driver()
    if not driver:
        return {"status": "error", "message": "Neo4j not configured."}

    query = """
    MATCH (e:Entity {entity_id: $entity_id})-[r]-(other:Entity)
    RETURN other.entity_id AS entity_id,
           other.name AS name,
           other.entity_type AS entity_type,
           type(r) AS relationship,
           r.created_at AS since,
           r.author AS author,
           startNode(r).entity_id = $entity_id AS outgoing,
           properties(r) AS properties
    ORDER BY r.created_at DESC
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, entity_id=entity_id)
            records = [dict(r) async for r in result]
        return {"status": "success", "count": len(records), "history": records}
    except Exception as e:
        logger.error("Neo4j get_entity_history error: %s", e)
        return {"status": "error", "message": str(e)}
