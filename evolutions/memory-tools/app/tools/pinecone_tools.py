"""Pinecone vector knowledge base — shared long-term memory across bot instances.

Supports namespaces: personal, professional, people, technical.
All users can read/search. Only the record creator (or admins) can update/delete.
Uses Pinecone's integrated inference — no local embeddings needed.
"""

import json
import logging
import os
import uuid
from datetime import datetime

from google.adk.tools.tool_context import ToolContext
from pinecone import PineconeAsyncio, SearchQuery

# WARNING: This tool depends on app.core.graph which is NOT included in this package.
try:
    from app.core import graph as neo4j_graph
except ImportError:
    neo4j_graph = None

logger = logging.getLogger(__name__)

NAMESPACES = ("personal", "professional", "people", "technical")

MEMORY_CATEGORIES = {
    "idea": "New opportunities, proposals, brainstorms.",
    "memory": "Time-stamped events/decisions.",
    "knowledge": "Reference material, rules, pro-tips, best practices.",
    "procedure": "Step-by-step operational playbooks / SOPs.",
    "experiment": "Tests/experimental runs with results.",
    "incident": "Problems/complaints requiring follow-up.",
    "project": "Initiatives, plans, or requests requiring tracking.",
    "technical": "Engineering changes, debug notes, platform specifics.",
    "strategy": "High-level plans, positioning, promotional strategy.",
    "communication_style": "Templates / tone/style examples.",
    "policy": "Compliance / legal risks and guidance.",
    "operational": "Short operational updates, inventory, event-day notes.",
}


def _get_config():
    api_key = os.environ.get("PINECONE_API_KEY", "")
    index_name = os.environ.get("PINECONE_INDEX_NAME", "")
    return api_key, index_name


# Reuse a single async client + cached host to avoid unclosed aiohttp sessions
_pc_client: PineconeAsyncio | None = None
_pc_host: str | None = None


async def _get_client_and_host():
    """Return a shared (PineconeAsyncio, host) pair, creating on first call."""
    global _pc_client, _pc_host
    api_key, index_name = _get_config()
    if not api_key:
        return None, None, "PINECONE_API_KEY not configured. Set via /init."

    if _pc_client is None:
        _pc_client = PineconeAsyncio(api_key=api_key)

    if _pc_host is None:
        host, err = await _get_index(_pc_client, index_name)
        if err:
            return None, None, err
        _pc_host = host

    return _pc_client, _pc_host, None


def _get_admin_ids() -> list[str]:
    raw = os.environ.get("ADMIN_USER_IDS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _get_user_id(tool_context: ToolContext) -> str:
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    return state.get("user_id", "unknown")


async def _get_index(pc, index_name):
    """Get the Pinecone index host."""
    descr = await pc.describe_index(index_name)
    if not descr or not descr.host:
        return None, f"Could not connect to Pinecone index '{index_name}'"
    return descr.host, None


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

async def search_knowledge(
    search_query: str,
    namespace: str,
    top_k: int = 5,
    tool_context: ToolContext = None,
) -> dict:
    """Search the knowledge base using natural language.

    Args:
        search_query: Natural language search query.
        namespace: Namespace to search in. One of: personal, professional, people, technical.
        top_k: Number of results to return (default 5, max 20).
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}

    top_k = min(max(top_k, 1), 20)

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        query = SearchQuery(inputs={"text": search_query}, top_k=top_k)
        async with pc.IndexAsyncio(host) as index:
            results = await index.search_records(namespace=namespace, query=query)

        if results:
            hits = results.to_dict().get("result", {}).get("hits", [])
            if hits:
                # Flatten for readability
                summaries = []
                for hit in hits:
                    fields = hit.get("fields", {})
                    summaries.append({
                        "id": hit.get("_id", ""),
                        "score": hit.get("_score", 0),
                        "short_description": fields.get("short_description", ""),
                        "category": fields.get("category", ""),
                        "text": fields.get("text", ""),
                        "tags": fields.get("tags", []),
                        "user_id": fields.get("user_id", ""),
                        "created_at": fields.get("created_at", ""),
                    })
                return {"status": "success", "count": len(summaries), "results": summaries}
        return {"status": "success", "count": 0, "results": [], "message": "No matches found."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# GET BY ID
# ---------------------------------------------------------------------------

async def get_records(
    record_ids: list[str],
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """Fetch specific records by their IDs.

    Args:
        record_ids: List of record IDs to fetch.
        namespace: Namespace to fetch from. One of: personal, professional, people, technical.
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        async with pc.IndexAsyncio(host) as index:
            result = await index.fetch(ids=record_ids, namespace=namespace)

        if not result.vectors:
            return {"status": "success", "records": {}, "message": "No records found."}

        records = {}
        for rid, vec in result.vectors.items():
            records[rid] = vec.to_dict().get("metadata", {})
        return {"status": "success", "records": records}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------

async def list_records(
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """List all record IDs and short descriptions in a namespace.

    Args:
        namespace: Namespace to list. One of: personal, professional, people, technical.
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        all_ids = []
        async with pc.IndexAsyncio(host) as index:
            results = await index.list_paginated(namespace=namespace, limit=100)
            if results.vectors:
                all_ids.extend([v.id for v in results.vectors])
            while results.pagination:
                results = await index.list_paginated(
                    namespace=namespace,
                    limit=100,
                    pagination_token=results.pagination.next,
                )
                if results.vectors:
                    all_ids.extend([v.id for v in results.vectors])

        if not all_ids:
            return {"status": "success", "records": [], "message": "Namespace is empty."}

        # Fetch short descriptions in batches
        summaries = []
        batch_size = 50
        async with pc.IndexAsyncio(host) as index:
            for i in range(0, len(all_ids), batch_size):
                batch = all_ids[i:i + batch_size]
                result = await index.fetch(ids=batch, namespace=namespace)
                for rid, vec in result.vectors.items():
                    meta = vec.to_dict().get("metadata", {})
                    summaries.append({
                        "id": rid,
                        "short_description": meta.get("short_description", ""),
                        "category": meta.get("category", ""),
                        "user_id": meta.get("user_id", ""),
                    })

        return {"status": "success", "records": summaries, "total": len(summaries)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------

async def create_record(
    namespace: str,
    text: str,
    short_description: str,
    category: str,
    tags: list[str],
    related_people: list[str] = None,
    related_memories: list[str] = None,
    author: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Create a new knowledge record in Pinecone.

    Args:
        namespace: Target namespace. One of: personal, professional, people, technical.
        text: Full content of the record.
        short_description: Brief title/summary.
        category: Record category. One of: idea, memory, knowledge, procedure, experiment,
                  incident, project, technical, strategy, communication_style, policy, operational.
        tags: List of keyword tags for the record.
        related_people: Optional list of person record IDs this relates to.
        related_memories: Optional list of memory record IDs this relates to.
        author: Who initiated this memory. Use the user's ID when they explicitly ask
                to remember something. Use 'agent' when you decide to store something
                on your own without the user asking. Leave empty to auto-detect from context.
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}
    if not text or not short_description:
        return {"status": "error", "message": "Both 'text' and 'short_description' are required."}
    if category not in MEMORY_CATEGORIES:
        return {"status": "error", "message": f"Invalid category. Must be one of: {', '.join(MEMORY_CATEGORIES.keys())}"}

    user_id = _get_user_id(tool_context) if tool_context else "unknown"
    resolved_author = author if author else user_id
    now = datetime.now()
    record_id = f"mem_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:8]}"

    record = {
        "id": record_id,
        "user_id": user_id,
        "author": resolved_author,
        "created_at": int(now.timestamp()),
        "text": text,
        "short_description": short_description,
        "category": category,
        "tags": tags or [],
    }
    if related_people:
        record["related_people"] = related_people
    if related_memories:
        record["related_memories"] = json.dumps(related_memories)

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        async with pc.IndexAsyncio(host) as index:
            await index.upsert_records(namespace=namespace, records=[record])

        # Dual-write: create a corresponding entity in Neo4j graph
        await _sync_record_to_graph(
            record_id=record_id,
            short_description=short_description,
            category=category,
            related_people=related_people,
            related_memories=related_memories,
            author=resolved_author,
        )

        return {
            "status": "success",
            "record_id": record_id,
            "message": f"Record '{short_description}' created in {namespace}.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# CREATE PERSON
# ---------------------------------------------------------------------------

async def create_person(
    first_name: str,
    last_name: str,
    role: str,
    user_ids: str,
    relations: str = "[]",
    author: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Create a new person record in the people directory.

    Args:
        first_name: Person's first name.
        last_name: Person's last name (use empty string if unknown).
        role: Relationship role (e.g., friend, colleague, mentor, manager).
        user_ids: JSON string of identifiers, e.g. [{"id_type": "email", "id_value": "user@example.com"}].
        relations: Optional JSON string of relationships, e.g. [{"related_person_id": "per_...", "relation_type": "colleague"}].
        author: Who initiated this. Use the user's ID when they explicitly ask, 'agent' when
                you decide on your own, or leave empty to auto-detect.
    """
    if not first_name:
        return {"status": "error", "message": "'first_name' is required."}

    user_id = _get_user_id(tool_context) if tool_context else "unknown"
    resolved_author = author if author else user_id
    now = datetime.now()
    person_id = f"per_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:8]}"

    # Build searchable text
    try:
        ids_list = json.loads(user_ids) if isinstance(user_ids, str) else user_ids
        ids_str = ", ".join(item.get("id_value", "") for item in ids_list)
    except (json.JSONDecodeError, TypeError):
        ids_str = str(user_ids)

    record = {
        "id": person_id,
        "user_id": user_id,
        "author": resolved_author,
        "created_at": int(now.timestamp()),
        "first_name": first_name,
        "last_name": last_name or "",
        "text": f"{first_name} {last_name or ''}. IDs: {ids_str}".strip(),
        "role": role,
        "user_ids": user_ids if isinstance(user_ids, str) else json.dumps(user_ids),
        "relations": relations if isinstance(relations, str) else json.dumps(relations),
    }

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        async with pc.IndexAsyncio(host) as index:
            await index.upsert_records(namespace="people", records=[record])

        # Dual-write: create person entity in Neo4j graph
        await _sync_person_to_graph(
            person_id=person_id,
            first_name=first_name,
            last_name=last_name or "",
            role=role,
            relations=relations,
            author=resolved_author,
        )

        return {
            "status": "success",
            "person_id": person_id,
            "message": f"Person '{first_name} {last_name or ''}' created in people directory.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# UPDATE (creator-only)
# ---------------------------------------------------------------------------

_MEMORY_UPDATE_FIELDS = frozenset({
    "text", "short_description", "category", "tags",
    "related_people", "related_memories",
})

_PERSON_UPDATE_FIELDS = frozenset({
    "first_name", "last_name", "role", "user_ids", "relations",
})


async def update_record(
    record_id: str,
    namespace: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Update an existing record. Only the creator or admins can update.

    Args:
        record_id: ID of the record to update.
        namespace: Namespace of the record. One of: personal, professional, people, technical.
        updates: JSON string of fields to update, e.g. {"text": "new content", "tags": ["new", "tags"]}.
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}

    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else updates
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON in 'updates' parameter."}

    if not isinstance(updates_dict, dict) or not updates_dict:
        return {"status": "error", "message": "'updates' must be a non-empty JSON object."}

    # Validate fields based on namespace
    allowed = _PERSON_UPDATE_FIELDS if namespace == "people" else _MEMORY_UPDATE_FIELDS
    bad_keys = [k for k in updates_dict if k not in allowed]
    if bad_keys:
        return {"status": "error", "message": f"Cannot update fields: {', '.join(bad_keys)}. Allowed: {', '.join(allowed)}"}

    user_id = _get_user_id(tool_context) if tool_context else "unknown"
    admin_ids = _get_admin_ids()

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        async with pc.IndexAsyncio(host) as index:
            existing = await index.fetch(ids=[record_id], namespace=namespace)
            if not existing.vectors:
                return {"status": "error", "message": f"Record {record_id} not found in {namespace}."}

            # Creator check
            meta = existing.vectors[record_id].to_dict().get("metadata", {})
            creator = meta.get("user_id", "")
            if user_id != creator and user_id not in admin_ids:
                return {
                    "status": "forbidden",
                    "message": f"Record {record_id} was created by {creator}. Only the creator or admins can modify it.",
                }

            # Prepare update payload
            payload = updates_dict.copy()
            payload["updated_at"] = int(datetime.now().timestamp())
            if "related_memories" in payload and isinstance(payload["related_memories"], list):
                payload["related_memories"] = json.dumps(payload["related_memories"])
            if "relations" in payload and isinstance(payload["relations"], list):
                payload["relations"] = json.dumps(payload["relations"])
            if "user_ids" in payload and isinstance(payload["user_ids"], list):
                payload["user_ids"] = json.dumps(payload["user_ids"])

            await index.update(id=record_id, namespace=namespace, set_metadata=payload)

        return {
            "status": "success",
            "message": f"Record {record_id} updated in {namespace}.",
            "updated_fields": list(updates_dict.keys()),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# DELETE (creator-only)
# ---------------------------------------------------------------------------

async def delete_record(
    record_id: str,
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """Delete a record. Only the creator or admins can delete.

    Args:
        record_id: ID of the record to delete.
        namespace: Namespace of the record. One of: personal, professional, people, technical.
    """
    if namespace not in NAMESPACES:
        return {"status": "error", "message": f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}"}

    user_id = _get_user_id(tool_context) if tool_context else "unknown"
    admin_ids = _get_admin_ids()

    try:
        pc, host, err = await _get_client_and_host()
        if err:
            return {"status": "error", "message": err}

        async with pc.IndexAsyncio(host) as index:
            existing = await index.fetch(ids=[record_id], namespace=namespace)
            if not existing.vectors:
                return {"status": "error", "message": f"Record {record_id} not found in {namespace}."}

            # Creator check
            meta = existing.vectors[record_id].to_dict().get("metadata", {})
            creator = meta.get("user_id", "")
            if user_id != creator and user_id not in admin_ids:
                return {
                    "status": "forbidden",
                    "message": f"Record {record_id} was created by {creator}. Only the creator or admins can delete it.",
                }

            # Store backup in session state before deleting
            if tool_context:
                tool_context.state["memory_backup"] = meta

            await index.delete(ids=[record_id], namespace=namespace)

        # Clean up corresponding Neo4j entity (best-effort)
        if neo4j_graph and neo4j_graph.is_configured():
            try:
                await neo4j_graph.delete_entity(record_id)
            except Exception:
                pass  # graph cleanup is best-effort

        return {
            "status": "success",
            "message": f"Record {record_id} deleted from {namespace}. Backup stored in session state.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Neo4j dual-write helpers (best-effort — Pinecone is source of truth)
# ---------------------------------------------------------------------------

async def _sync_record_to_graph(
    record_id: str,
    short_description: str,
    category: str,
    related_people: list[str] | None,
    related_memories: list[str] | None,
    author: str,
) -> None:
    """Create a Neo4j entity for a Pinecone record and link related entities."""
    if not neo4j_graph or not neo4j_graph.is_configured():
        return

    try:
        # Map Pinecone categories to graph entity types
        type_map = {
            "idea": "concept", "memory": "event", "knowledge": "concept",
            "procedure": "concept", "experiment": "event", "incident": "event",
            "project": "project", "technical": "concept", "strategy": "concept",
            "communication_style": "concept", "policy": "concept", "operational": "event",
        }
        entity_type = type_map.get(category, "concept")

        await neo4j_graph.upsert_entity(
            entity_id=record_id,
            name=short_description,
            entity_type=entity_type,
            properties={"category": category, "source": "pinecone"},
            pinecone_id=record_id,
            author=author,
        )

        # Link to related people
        if related_people:
            for person_id in related_people:
                await neo4j_graph.add_relationship(
                    from_entity_id=record_id,
                    to_entity_id=person_id,
                    relation_type="INVOLVES",
                    author=author,
                )

        # Link to related memories
        if related_memories:
            for mem_id in related_memories:
                await neo4j_graph.add_relationship(
                    from_entity_id=record_id,
                    to_entity_id=mem_id,
                    relation_type="RELATED_TO",
                    author=author,
                )
    except Exception as e:
        logger.warning("Neo4j dual-write failed for record %s: %s", record_id, e)


async def _sync_person_to_graph(
    person_id: str,
    first_name: str,
    last_name: str,
    role: str,
    relations: str,
    author: str,
) -> None:
    """Create a Neo4j entity for a Pinecone person and link relations."""
    if not neo4j_graph or not neo4j_graph.is_configured():
        return

    try:
        full_name = f"{first_name} {last_name}".strip()
        await neo4j_graph.upsert_entity(
            entity_id=person_id,
            name=full_name,
            entity_type="person",
            properties={"role": role, "source": "pinecone"},
            pinecone_id=person_id,
            author=author,
        )

        # Parse and create relationship edges
        try:
            rels = json.loads(relations) if isinstance(relations, str) else relations
        except (json.JSONDecodeError, TypeError):
            rels = []

        for rel in rels:
            if isinstance(rel, dict) and "related_person_id" in rel:
                rel_type = rel.get("relation_type", "RELATED_TO")
                await neo4j_graph.add_relationship(
                    from_entity_id=person_id,
                    to_entity_id=rel["related_person_id"],
                    relation_type=rel_type,
                    author=author,
                )
    except Exception as e:
        logger.warning("Neo4j dual-write failed for person %s: %s", person_id, e)
