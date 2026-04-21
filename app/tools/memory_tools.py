"""Consolidated memory + knowledge-graph tool surface — Neo4j only.

Replaces the Pinecone + Neo4j dual-write model. All memory and person records
live in Neo4j now; embeddings are generated inline via Neo4j's GenAI plugin
calling OpenAI's `text-embedding-3-small` (1536-dim cosine).

Label convention (see `app/core/graph_schema.py` for the full rationale):
- Memories: `:Memory` + one of `:PersonalMemory`, `:ProfessionalMemory`, `:TechnicalMemory`.
- People: `:Person` + one or both of `:PersonalPerson`, `:ProfessionalPerson`.
- Authorship: `(caller:Person)-[:AUTHORED {via_bot, created_at}]->(record)`.

Access control is enforced in Python, per-namespace:
- `personal` — admins only read.
- `professional` — admins or callers whose `user_id` ends with `@<COMPANY_DOMAIN>`.
- `technical` — open (bot-to-bot knowledge sharing across departments).
Create is allowed for any authenticated caller. Update/delete run a Cypher
MATCH on the `:AUTHORED` edge — non-authors get `{status: "forbidden"}` unless
they use the admin-only `update_any_*` variants.
"""

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime

from google.adk.tools.tool_context import ToolContext

from app.core import graph as neo4j_graph
from app.core.graph_schema import ensure_schema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAMESPACES = ("personal", "professional", "technical")
PERSON_SCOPES = ("personal", "professional")

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

_NAMESPACE_TO_MEMORY_LABEL = {
    "personal": "PersonalMemory",
    "professional": "ProfessionalMemory",
    "technical": "TechnicalMemory",
}

_SCOPE_TO_PERSON_LABEL = {
    "personal": "PersonalPerson",
    "professional": "ProfessionalPerson",
}

_MEMORY_INDEX_BY_NAMESPACE = {
    "personal": "personal_memory_embedding",
    "professional": "professional_memory_embedding",
    "technical": "technical_memory_embedding",
}

_PERSON_INDEX_BY_SCOPE = {
    "personal": "personal_person_embedding",
    "professional": "professional_person_embedding",
}

# ---------------------------------------------------------------------------
# Config + identity helpers
# ---------------------------------------------------------------------------


def _get_openai_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip()


def _get_company_domain() -> str:
    """Matches `clickup_agent.py:_get_company_domain()` exactly."""
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def _get_admin_ids() -> list[str]:
    raw = os.environ.get("ADMIN_USER_IDS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _get_bot_name() -> str:
    return os.environ.get("BOT_NAME", "").strip() or "unknown_bot"


def _get_caller(tool_context: ToolContext | None) -> str:
    """Resolve the caller's platform user_id from the ADK tool_context state.

    `app/callbacks/guardrails.py:state_setter` populates `state['user_id']`
    from the `actual_caller_id` message tag before any tool runs.
    """
    if tool_context is None:
        return "unknown"
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    return state.get("user_id", "unknown")


def _get_acl_flags(caller: str) -> tuple[bool, bool]:
    """Return `(is_admin, is_company)` for a caller user_id.

    `is_company` follows the existing `clickup_agent.py:46-55` pattern: if
    `COMPANY_DOMAIN` is unset, every caller is a non-company user (the gate
    is effectively disabled — tools that only check `is_company` become
    open).
    """
    is_admin = caller in _get_admin_ids()
    domain = _get_company_domain()
    is_company = bool(domain) and caller.lower().endswith(f"@{domain}")
    return is_admin, is_company


def _can_read_namespace(namespace: str, is_admin: bool, is_company: bool) -> bool:
    if namespace == "personal":
        return is_admin
    if namespace == "professional":
        return is_admin or is_company
    if namespace == "technical":
        return True
    return False


def _can_read_person_scope(scope: str, is_admin: bool, is_company: bool) -> bool:
    if scope == "personal":
        return is_admin
    if scope == "professional":
        return is_admin or is_company
    return False


def _forbidden(message: str) -> dict:
    return {"status": "forbidden", "message": message}


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


# ---------------------------------------------------------------------------
# Driver + schema + auto-provisioning
# ---------------------------------------------------------------------------


async def _ready_driver():
    """Return the Neo4j driver, after ensuring schema is in place."""
    driver = neo4j_graph._get_driver()
    if driver is None:
        return None
    await ensure_schema(driver)
    return driver


def _new_record_id() -> str:
    return f"mem_{datetime.now().strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:8]}"


def _new_person_id() -> str:
    return f"per_{datetime.now().strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:8]}"


def _auto_person_id(caller: str) -> str:
    """Deterministic person_id for auto-provisioned caller Persons.

    Using a hash keeps the ID stable across re-provisions so references don't
    drift if a caller's Person node is ever recreated manually.
    """
    return f"per_auto_{hashlib.md5(caller.encode('utf-8')).hexdigest()[:8]}"


async def _auto_provision_person(driver, caller: str, is_company: bool) -> None:
    """MERGE the caller's :Person node, tag with the scope label.

    Idempotent. Sets the scope label on every call so if a caller's company
    status changes, the label tracks it. `is_auto_provisioned` distinguishes
    these stubs from curated Person records (`create_person`).
    """
    if caller == "unknown" or not caller:
        return
    scope_label = _SCOPE_TO_PERSON_LABEL["professional" if is_company else "personal"]
    person_id = _auto_person_id(caller)
    query = (
        "MERGE (p:Person {primary_user_id: $caller}) "
        "ON CREATE SET "
        "    p.person_id = $person_id, "
        "    p.full_name = $caller, "
        "    p.aliases = [], "
        "    p.is_auto_provisioned = true, "
        "    p.created_at = datetime(), "
        "    p.updated_at = datetime() "
        f"SET p:{scope_label}"
    )
    async with driver.session() as session:
        await session.run(query, caller=caller, person_id=person_id)


# ---------------------------------------------------------------------------
# Memory tools
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
        namespace: One of: personal, professional, technical.
        top_k: Number of results to return (default 5, max 20).
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    caller = _get_caller(tool_context)
    is_admin, is_company = _get_acl_flags(caller)
    if not _can_read_namespace(namespace, is_admin, is_company):
        return _forbidden(
            f"Read access to '{namespace}' memories is not available for your role."
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured (NEO4J_URI/NEO4J_PASSWORD missing).")

    openai_key = _get_openai_key()
    if not openai_key:
        return _error("OPENAI_API_KEY not configured.")

    top_k = max(1, min(int(top_k), 20))
    index_name = _MEMORY_INDEX_BY_NAMESPACE[namespace]

    query = (
        "WITH $query_text AS q, "
        "     {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        "WITH genai.vector.encode(q, 'OpenAI', cfg) AS vec "
        f"CALL db.index.vector.queryNodes('{index_name}', $top_k, vec) "
        "YIELD node, score "
        "RETURN node.record_id AS record_id, "
        "       node.short_description AS short_description, "
        "       node.text AS text, "
        "       node.category AS category, "
        "       node.tags AS tags, "
        "       toString(node.created_at) AS created_at, "
        "       score"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query, query_text=search_query, openai_key=openai_key, top_k=top_k
            )
            records = [dict(r) async for r in result]
        return {"status": "success", "count": len(records), "results": records}
    except Exception as e:
        logger.exception("search_knowledge failed")
        return _error(str(e))


async def get_records(
    record_ids: list[str],
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """Fetch specific records by their IDs.

    Args:
        record_ids: List of record IDs.
        namespace: One of: personal, professional, technical.
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    caller = _get_caller(tool_context)
    is_admin, is_company = _get_acl_flags(caller)
    if not _can_read_namespace(namespace, is_admin, is_company):
        return _forbidden(
            f"Read access to '{namespace}' memories is not available for your role."
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    query = (
        f"MATCH (m:{label}) "
        "WHERE m.record_id IN $ids "
        "RETURN m.record_id AS record_id, "
        "       m.short_description AS short_description, "
        "       m.text AS text, "
        "       m.category AS category, "
        "       m.tags AS tags, "
        "       toString(m.created_at) AS created_at, "
        "       toString(m.updated_at) AS updated_at"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, ids=record_ids)
            records = {r["record_id"]: dict(r) async for r in result}
        return {"status": "success", "records": records}
    except Exception as e:
        logger.exception("get_records failed")
        return _error(str(e))


async def list_records(
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """List all record IDs and short descriptions in a namespace.

    Args:
        namespace: One of: personal, professional, technical.
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    caller = _get_caller(tool_context)
    is_admin, is_company = _get_acl_flags(caller)
    if not _can_read_namespace(namespace, is_admin, is_company):
        return _forbidden(
            f"Read access to '{namespace}' memories is not available for your role."
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    query = (
        f"MATCH (m:{label}) "
        "RETURN m.record_id AS record_id, "
        "       m.short_description AS short_description, "
        "       m.category AS category "
        "ORDER BY m.created_at DESC"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query)
            records = [dict(r) async for r in result]
        return {"status": "success", "records": records, "total": len(records)}
    except Exception as e:
        logger.exception("list_records failed")
        return _error(str(e))


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
    """Create a new knowledge record.

    Args:
        namespace: One of: personal, professional, technical.
        text: Full content of the record (source for embedding).
        short_description: Brief title/summary.
        category: One of: idea, memory, knowledge, procedure, experiment, incident,
                  project, technical, strategy, communication_style, policy, operational.
        tags: List of keyword tags.
        related_people: Optional list of person IDs this memory involves.
        related_memories: Optional list of memory IDs this links to.
        author: Advisory only — retained for signature compat. Actual authorship
                is captured via an `:AUTHORED` edge from the caller's Person node.
    """
    # Intentionally unused — authorship is the edge, not a property.
    del author

    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")
    if not text or not short_description:
        return _error("Both 'text' and 'short_description' are required.")
    if category not in MEMORY_CATEGORIES:
        return _error(
            f"Invalid category. Must be one of: {', '.join(MEMORY_CATEGORIES)}"
        )

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    openai_key = _get_openai_key()
    if not openai_key:
        return _error("OPENAI_API_KEY not configured.")

    await _auto_provision_person(driver, caller, is_company)

    memory_label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    record_id = _new_record_id()
    bot_name = _get_bot_name()
    tags = tags or []

    query = (
        "MATCH (caller:Person {primary_user_id: $caller_id}) "
        "WITH caller, {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        f"CREATE (m:Memory:{memory_label} {{ "
        "    record_id: $record_id, "
        "    text: $text, "
        "    embedding: genai.vector.encode($text, 'OpenAI', cfg), "
        "    short_description: $short_description, "
        "    category: $category, "
        "    tags: $tags, "
        "    created_at: datetime(), "
        "    updated_at: datetime() "
        "}) "
        "CREATE (caller)-[:AUTHORED {via_bot: $bot_name, created_at: datetime()}]->(m) "
        "RETURN m.record_id AS record_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                caller_id=caller,
                openai_key=openai_key,
                record_id=record_id,
                text=text,
                short_description=short_description,
                category=category,
                tags=tags,
                bot_name=bot_name,
            )
            row = await result.single()
            if not row:
                return _error("Record creation did not return an ID — caller Person missing?")
            created_id = row["record_id"]

        # Secondary writes for relationship edges. Separate queries keep the
        # main create-Cypher small, and a failure here doesn't orphan the
        # already-committed memory node.
        if related_people:
            await _link_memory_to_people(driver, created_id, related_people, caller)
        if related_memories:
            await _link_memory_to_memories(driver, created_id, related_memories, caller)

        return {
            "status": "success",
            "record_id": created_id,
            "message": f"Record '{short_description}' created in {namespace}.",
        }
    except Exception as e:
        logger.exception("create_record failed")
        return _error(str(e))


async def update_record(
    record_id: str,
    namespace: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Update a record. Only the creator (via :AUTHORED edge) or admins can modify.

    Admins wanting to force-update a record they didn't author should use
    `update_any_record` instead.

    Args:
        record_id: ID of the record.
        namespace: One of: personal, professional, technical.
        updates: JSON string of fields to update.
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else dict(updates)
    except (json.JSONDecodeError, TypeError):
        return _error("Invalid JSON in 'updates' parameter.")

    allowed = {"text", "short_description", "category", "tags"}
    bad = [k for k in updates_dict if k not in allowed]
    if bad:
        return _error(
            f"Cannot update fields: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}"
        )
    if "category" in updates_dict and updates_dict["category"] not in MEMORY_CATEGORIES:
        return _error(
            f"Invalid category. Must be one of: {', '.join(MEMORY_CATEGORIES)}"
        )

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, _ = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    openai_key = _get_openai_key()

    # Build SET clauses dynamically. Text changes trigger an embedding refresh.
    set_parts = []
    params = {
        "record_id": record_id,
        "caller_id": caller,
        "openai_key": openai_key,
    }
    for key, value in updates_dict.items():
        set_parts.append(f"m.{key} = ${key}")
        params[key] = value
    if "text" in updates_dict:
        if not openai_key:
            return _error("OPENAI_API_KEY required for text updates (embedding refresh).")
        set_parts.append(
            "m.embedding = genai.vector.encode($text, 'OpenAI', "
            "{token: $openai_key, model: 'text-embedding-3-small'})"
        )
    set_parts.append("m.updated_at = datetime()")
    set_clause = ", ".join(set_parts)

    # Admin path skips the :AUTHORED gate (handled by `update_any_record`). This
    # tool enforces creator-only; admins who own the record also pass this.
    query = (
        f"MATCH (caller:Person {{primary_user_id: $caller_id}})-[:AUTHORED]->(m:{label} {{record_id: $record_id}}) "
        f"SET {set_clause} "
        "RETURN m.record_id AS record_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                if is_admin:
                    return _forbidden(
                        "This tool enforces creator-only updates. Use update_any_record for admin override."
                    )
                return _forbidden(
                    f"Record {record_id} was not authored by you. Only the creator or admins can modify it."
                )
        return {
            "status": "success",
            "record_id": record_id,
            "updated_fields": sorted(updates_dict),
        }
    except Exception as e:
        logger.exception("update_record failed")
        return _error(str(e))


async def update_any_record(
    record_id: str,
    namespace: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: update a record bypassing the :AUTHORED creator gate.

    Args: same shape as `update_record`.
    """
    caller = _get_caller(tool_context)
    is_admin, _ = _get_acl_flags(caller)
    if not is_admin:
        return _forbidden("update_any_record is admin-only.")

    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else dict(updates)
    except (json.JSONDecodeError, TypeError):
        return _error("Invalid JSON in 'updates' parameter.")

    allowed = {"text", "short_description", "category", "tags"}
    bad = [k for k in updates_dict if k not in allowed]
    if bad:
        return _error(
            f"Cannot update fields: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}"
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    openai_key = _get_openai_key()

    set_parts = []
    params = {"record_id": record_id, "openai_key": openai_key}
    for key, value in updates_dict.items():
        set_parts.append(f"m.{key} = ${key}")
        params[key] = value
    if "text" in updates_dict:
        if not openai_key:
            return _error("OPENAI_API_KEY required for text updates (embedding refresh).")
        set_parts.append(
            "m.embedding = genai.vector.encode($text, 'OpenAI', "
            "{token: $openai_key, model: 'text-embedding-3-small'})"
        )
    set_parts.append("m.updated_at = datetime()")
    set_clause = ", ".join(set_parts)

    query = (
        f"MATCH (m:{label} {{record_id: $record_id}}) "
        f"SET {set_clause} "
        "RETURN m.record_id AS record_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                return _error(f"Record {record_id} not found in {namespace}.")
        return {
            "status": "success",
            "record_id": record_id,
            "updated_fields": sorted(updates_dict),
        }
    except Exception as e:
        logger.exception("update_any_record failed")
        return _error(str(e))


async def delete_record(
    record_id: str,
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """Delete a record. Only the creator (via :AUTHORED) or admins can delete.

    Args:
        record_id: ID of the record.
        namespace: One of: personal, professional, technical.
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, _ = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]

    # Admin bypass handled inline: if is_admin, match without the AUTHORED
    # predicate; otherwise require it.
    if is_admin:
        query = (
            f"MATCH (m:{label} {{record_id: $record_id}}) "
            "DETACH DELETE m "
            "RETURN count(m) AS deleted"
        )
        params = {"record_id": record_id}
    else:
        query = (
            f"MATCH (caller:Person {{primary_user_id: $caller_id}})-[:AUTHORED]->(m:{label} {{record_id: $record_id}}) "
            "DETACH DELETE m "
            "RETURN count(m) AS deleted"
        )
        params = {"record_id": record_id, "caller_id": caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            deleted = row["deleted"] if row else 0
            if not deleted:
                if is_admin:
                    return _error(f"Record {record_id} not found in {namespace}.")
                return _forbidden(
                    f"Record {record_id} was not authored by you. Only the creator or admins can delete."
                )
        return {
            "status": "success",
            "message": f"Record {record_id} deleted from {namespace}.",
        }
    except Exception as e:
        logger.exception("delete_record failed")
        return _error(str(e))


# ---------------------------------------------------------------------------
# People tools
# ---------------------------------------------------------------------------


async def create_person(
    first_name: str,
    last_name: str,
    role: str,
    user_ids: str,
    relations: str = "[]",
    scopes: list[str] = None,
    author: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Create a new person record.

    Args:
        first_name, last_name: Person's name (last_name may be empty).
        role: Relationship role (e.g., friend, colleague, mentor).
        user_ids: JSON string of identifiers, e.g. [{"id_type": "email", "id_value": "x@y.com"}].
        relations: Optional JSON string of relationships to other people.
        scopes: List of scope labels. Valid entries: "personal", "professional".
                Default: ["professional"]. Dual-scope people pass both.
        author: Advisory — authorship is captured via :AUTHORED edge.
    """
    del author

    if not first_name:
        return _error("'first_name' is required.")

    scopes = scopes or ["professional"]
    if not scopes:
        return _error("At least one scope required (personal or professional).")
    for s in scopes:
        if s not in PERSON_SCOPES:
            return _error(f"Invalid scope '{s}'. Must be one of: {', '.join(PERSON_SCOPES)}")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    _, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    openai_key = _get_openai_key()
    if not openai_key:
        return _error("OPENAI_API_KEY not configured.")

    await _auto_provision_person(driver, caller, is_company)

    person_id = _new_person_id()
    full_name = f"{first_name} {last_name or ''}".strip()
    bot_name = _get_bot_name()

    # Normalize user_ids/relations inputs to JSON strings for storage.
    try:
        user_ids_list = json.loads(user_ids) if isinstance(user_ids, str) else user_ids
    except (json.JSONDecodeError, TypeError):
        user_ids_list = []
    try:
        relations_list = json.loads(relations) if isinstance(relations, str) else relations
    except (json.JSONDecodeError, TypeError):
        relations_list = []

    # Embedding text includes name + role + identifiers for richer semantic search.
    ids_summary = ", ".join(
        str(item.get("id_value", "")) for item in user_ids_list if isinstance(item, dict)
    )
    embed_text = f"{full_name}. Role: {role}. IDs: {ids_summary}".strip()

    scope_labels = [_SCOPE_TO_PERSON_LABEL[s] for s in sorted(set(scopes))]
    # Build the label suffix as a single Cypher fragment.
    extra_labels = "".join(f":{label}" for label in scope_labels)

    query = (
        "MATCH (caller:Person {primary_user_id: $caller_id}) "
        "WITH caller, {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        f"CREATE (p:Person{extra_labels} {{ "
        "    person_id: $person_id, "
        "    first_name: $first_name, "
        "    last_name: $last_name, "
        "    full_name: $full_name, "
        "    role: $role, "
        "    user_ids: $user_ids, "
        "    embedding: genai.vector.encode($embed_text, 'OpenAI', cfg), "
        "    aliases: [], "
        "    is_auto_provisioned: false, "
        "    created_at: datetime(), "
        "    updated_at: datetime() "
        "}) "
        "CREATE (caller)-[:AUTHORED {via_bot: $bot_name, created_at: datetime()}]->(p) "
        "RETURN p.person_id AS person_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                caller_id=caller,
                openai_key=openai_key,
                person_id=person_id,
                first_name=first_name,
                last_name=last_name or "",
                full_name=full_name,
                role=role,
                user_ids=json.dumps(user_ids_list),
                embed_text=embed_text,
                bot_name=bot_name,
            )
            row = await result.single()
            if not row:
                return _error("Person creation did not return an ID — caller Person missing?")
            created_id = row["person_id"]

        # Wire explicit person→person relationships (e.g. colleague, spouse).
        if relations_list:
            await _link_person_relations(driver, created_id, relations_list, caller)

        return {
            "status": "success",
            "person_id": created_id,
            "scopes": sorted(set(scopes)),
            "message": f"Person '{full_name}' created with scope(s): {', '.join(sorted(set(scopes)))}.",
        }
    except Exception as e:
        logger.exception("create_person failed")
        return _error(str(e))


async def search_people(
    search_query: str,
    scope: str,
    top_k: int = 5,
    tool_context: ToolContext = None,
) -> dict:
    """Search people by natural language description, scoped.

    Admins wanting to search both scopes should call this twice.

    Args:
        search_query: Natural language.
        scope: One of: personal, professional.
        top_k: Max results (default 5, max 20).
    """
    if scope not in PERSON_SCOPES:
        return _error(f"Invalid scope. Must be one of: {', '.join(PERSON_SCOPES)}")

    caller = _get_caller(tool_context)
    is_admin, is_company = _get_acl_flags(caller)
    if not _can_read_person_scope(scope, is_admin, is_company):
        return _forbidden(
            f"Read access to '{scope}' people is not available for your role."
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    openai_key = _get_openai_key()
    if not openai_key:
        return _error("OPENAI_API_KEY not configured.")

    top_k = max(1, min(int(top_k), 20))
    index_name = _PERSON_INDEX_BY_SCOPE[scope]

    query = (
        "WITH $query_text AS q, "
        "     {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        "WITH genai.vector.encode(q, 'OpenAI', cfg) AS vec "
        f"CALL db.index.vector.queryNodes('{index_name}', $top_k, vec) "
        "YIELD node, score "
        "RETURN node.person_id AS person_id, "
        "       node.full_name AS full_name, "
        "       node.role AS role, "
        "       node.user_ids AS user_ids, "
        "       score"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query, query_text=search_query, openai_key=openai_key, top_k=top_k
            )
            records = [dict(r) async for r in result]
        return {"status": "success", "count": len(records), "results": records}
    except Exception as e:
        logger.exception("search_people failed")
        return _error(str(e))


async def update_person(
    person_id: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Update a person record. Only the creator (via :AUTHORED) or admins can modify.

    Admins wanting to override should use `update_any_person`.
    """
    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else dict(updates)
    except (json.JSONDecodeError, TypeError):
        return _error("Invalid JSON in 'updates' parameter.")

    allowed = {"first_name", "last_name", "role", "user_ids"}
    bad = [k for k in updates_dict if k not in allowed]
    if bad:
        return _error(
            f"Cannot update fields: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}"
        )

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, _ = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    # Build SET clauses. Name/role changes trigger embedding refresh.
    openai_key = _get_openai_key()
    touches_embedding = bool({"first_name", "last_name", "role", "user_ids"} & updates_dict.keys())
    if touches_embedding and not openai_key:
        return _error("OPENAI_API_KEY required for updates that refresh embeddings.")

    set_parts = []
    params = {"person_id": person_id, "caller_id": caller, "openai_key": openai_key}
    for key, value in updates_dict.items():
        if key == "user_ids":
            params[key] = json.dumps(value) if not isinstance(value, str) else value
        else:
            params[key] = value
        set_parts.append(f"p.{key} = ${key}")
    # Full-name denormalization + embedding refresh if any name component changed.
    if {"first_name", "last_name"} & updates_dict.keys():
        set_parts.append(
            "p.full_name = trim(coalesce(p.first_name, '') + ' ' + coalesce(p.last_name, ''))"
        )
    if touches_embedding:
        set_parts.append(
            "p.embedding = genai.vector.encode("
            "trim(coalesce(p.first_name, '') + ' ' + coalesce(p.last_name, '')) + "
            "'. Role: ' + coalesce(p.role, '') + '. IDs: ' + coalesce(p.user_ids, ''), "
            "'OpenAI', "
            "{token: $openai_key, model: 'text-embedding-3-small'})"
        )
    set_parts.append("p.updated_at = datetime()")
    set_clause = ", ".join(set_parts)

    query = (
        "MATCH (caller:Person {primary_user_id: $caller_id})-[:AUTHORED]->(p:Person {person_id: $person_id}) "
        f"SET {set_clause} "
        "RETURN p.person_id AS person_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                if is_admin:
                    return _forbidden(
                        "This tool enforces creator-only updates. Use update_any_person for admin override."
                    )
                return _forbidden(
                    f"Person {person_id} was not authored by you. Only the creator or admins can modify."
                )
        return {
            "status": "success",
            "person_id": person_id,
            "updated_fields": sorted(updates_dict),
        }
    except Exception as e:
        logger.exception("update_person failed")
        return _error(str(e))


async def update_any_person(
    person_id: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: update a person record bypassing the :AUTHORED creator gate."""
    caller = _get_caller(tool_context)
    is_admin, _ = _get_acl_flags(caller)
    if not is_admin:
        return _forbidden("update_any_person is admin-only.")

    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else dict(updates)
    except (json.JSONDecodeError, TypeError):
        return _error("Invalid JSON in 'updates' parameter.")

    allowed = {"first_name", "last_name", "role", "user_ids"}
    bad = [k for k in updates_dict if k not in allowed]
    if bad:
        return _error(
            f"Cannot update fields: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}"
        )

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    openai_key = _get_openai_key()
    touches_embedding = bool({"first_name", "last_name", "role", "user_ids"} & updates_dict.keys())
    if touches_embedding and not openai_key:
        return _error("OPENAI_API_KEY required for updates that refresh embeddings.")

    set_parts = []
    params = {"person_id": person_id, "openai_key": openai_key}
    for key, value in updates_dict.items():
        if key == "user_ids":
            params[key] = json.dumps(value) if not isinstance(value, str) else value
        else:
            params[key] = value
        set_parts.append(f"p.{key} = ${key}")
    if {"first_name", "last_name"} & updates_dict.keys():
        set_parts.append(
            "p.full_name = trim(coalesce(p.first_name, '') + ' ' + coalesce(p.last_name, ''))"
        )
    if touches_embedding:
        set_parts.append(
            "p.embedding = genai.vector.encode("
            "trim(coalesce(p.first_name, '') + ' ' + coalesce(p.last_name, '')) + "
            "'. Role: ' + coalesce(p.role, '') + '. IDs: ' + coalesce(p.user_ids, ''), "
            "'OpenAI', "
            "{token: $openai_key, model: 'text-embedding-3-small'})"
        )
    set_parts.append("p.updated_at = datetime()")
    set_clause = ", ".join(set_parts)

    query = (
        "MATCH (p:Person {person_id: $person_id}) "
        f"SET {set_clause} "
        "RETURN p.person_id AS person_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                return _error(f"Person {person_id} not found.")
        return {
            "status": "success",
            "person_id": person_id,
            "updated_fields": sorted(updates_dict),
        }
    except Exception as e:
        logger.exception("update_any_person failed")
        return _error(str(e))


async def promote_person(
    person_id: str,
    add_scope: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: add a scope label to an existing person.

    Enables dual-scope after the fact (e.g., a friend who becomes a colleague).
    """
    caller = _get_caller(tool_context)
    is_admin, _ = _get_acl_flags(caller)
    if not is_admin:
        return _forbidden("promote_person is admin-only.")

    if add_scope not in PERSON_SCOPES:
        return _error(f"Invalid scope '{add_scope}'. Must be one of: {', '.join(PERSON_SCOPES)}")

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _SCOPE_TO_PERSON_LABEL[add_scope]
    query = (
        "MATCH (p:Person {person_id: $person_id}) "
        f"SET p:{label}, p.updated_at = datetime() "
        "RETURN p.person_id AS person_id, labels(p) AS labels"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, person_id=person_id)
            row = await result.single()
            if not row:
                return _error(f"Person {person_id} not found.")
        return {
            "status": "success",
            "person_id": person_id,
            "labels": row["labels"],
            "message": f"Added scope '{add_scope}' to person {person_id}.",
        }
    except Exception as e:
        logger.exception("promote_person failed")
        return _error(str(e))


# ---------------------------------------------------------------------------
# Relationship helpers (internal — called from create_record / create_person)
# ---------------------------------------------------------------------------


async def _link_memory_to_people(
    driver, record_id: str, people_ids: list[str], author_caller: str
) -> None:
    """Create (:Memory)-[:INVOLVES]->(:Person) edges."""
    query = (
        "MATCH (m:Memory {record_id: $record_id}) "
        "UNWIND $people AS pid "
        "MATCH (p:Person {person_id: pid}) "
        "MERGE (m)-[r:INVOLVES]->(p) "
        "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
    )
    try:
        async with driver.session() as session:
            await session.run(query, record_id=record_id, people=people_ids, caller=author_caller)
    except Exception as e:
        logger.warning("Failed to link memory %s to people: %s", record_id, e)


async def _link_memory_to_memories(
    driver, record_id: str, memory_ids: list[str], author_caller: str
) -> None:
    """Create (:Memory)-[:RELATED_TO]->(:Memory) edges."""
    query = (
        "MATCH (m:Memory {record_id: $record_id}) "
        "UNWIND $others AS oid "
        "MATCH (o:Memory {record_id: oid}) "
        "MERGE (m)-[r:RELATED_TO]->(o) "
        "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
    )
    try:
        async with driver.session() as session:
            await session.run(query, record_id=record_id, others=memory_ids, caller=author_caller)
    except Exception as e:
        logger.warning("Failed to link memory %s to memories: %s", record_id, e)


async def _link_person_relations(
    driver, person_id: str, relations: list, author_caller: str
) -> None:
    """Create (:Person)-[:RELATED_TO {relation_type}]->(:Person) edges.

    `relations` is a list of dicts: {"related_person_id": <id>, "relation_type": <snake_case>}.
    Relation_type is sanitized and injected as a Cypher label (no APOC).
    """
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        target = rel.get("related_person_id")
        rel_type = rel.get("relation_type", "RELATED_TO")
        if not target:
            continue
        safe = "".join(c for c in rel_type.upper().replace(" ", "_") if c.isalnum() or c == "_")
        if not safe:
            safe = "RELATED_TO"
        query = (
            "MATCH (a:Person {person_id: $from}), (b:Person {person_id: $to}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, **{"from": person_id, "to": target, "caller": author_caller})
        except Exception as e:
            logger.warning("Failed to link person %s → %s (%s): %s", person_id, target, safe, e)
