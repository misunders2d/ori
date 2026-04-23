"""Consolidated memory + knowledge-graph tool surface — Neo4j only.

Replaces the Pinecone + Neo4j dual-write model. All memory and person records
live in Neo4j now; embeddings are generated inline via Neo4j's GenAI plugin
calling OpenAI's `text-embedding-3-small` (1536-dim cosine).

Label convention (see `app/core/graph_schema.py` for the full rationale):
- Memories: `:Memory` + one of `:PersonalMemory`, `:ProfessionalMemory`, `:TechnicalMemory`.
- People: `:Person` + one or both of `:PersonalPerson`, `:ProfessionalPerson`.
- Authorship: stored as `author_user_id`, `via_bot`, `created_at` properties
  on each `:Memory` / `:Person` node. The author's `:Person` node remains the
  canonical identity and is resolvable by `primary_user_id`; authorship is not
  a graph edge.

Access control is enforced in Python, per-namespace:
- `personal` — admins only read.
- `professional` — admins or callers whose `user_id` ends with `@<COMPANY_DOMAIN>`.
- `technical` — open (bot-to-bot knowledge sharing across departments).
Create is allowed for any authenticated caller. Update/delete run a Cypher
MATCH with a `WHERE node.author_user_id = $caller_id` predicate — non-authors
get `{status: "forbidden"}` unless they use the admin-only `update_any_*` variants.
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

# :Entity is the catch-all referable-thing label (brand, company, department,
# product, project, location, tool, etc.). Single generic vector index; the
# agent-supplied `entity_type` is a canonicalized property, not a sublabel.
_ENTITY_INDEX_NAME = "entity_embedding"

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


def _new_entity_id() -> str:
    return f"ent_{datetime.now().strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:8]}"


def _sanitize_relation_type(rel_type: str) -> str:
    """Uppercase + underscore-only form safe for direct Cypher label injection.

    Used by `_link_person_relations`, `relate_persons`, and `relate_entities`.
    Empty/invalid input falls back to `RELATED_TO`.
    """
    safe = "".join(
        c for c in (rel_type or "").upper().replace(" ", "_")
        if c.isalnum() or c == "_"
    )
    return safe or "RELATED_TO"


def _canonicalize_entity_type(entity_type: str) -> str:
    """Lowercase + snake_case form of the agent-supplied entity_type.

    Kills case/space drift so `Brand`, `brand`, and `BRAND` all land as
    `brand`. Invalid characters are dropped. Empty input returns ''.
    """
    out = []
    for ch in (entity_type or "").strip().replace("-", "_").replace(" ", "_"):
        if ch.isalnum() or ch == "_":
            out.append(ch.lower())
    return "".join(out)


def _normalize_related(items, default_type: str, id_keys: tuple) -> list[tuple[str, str]]:
    """Normalize polymorphic related_* input to [(id, sanitized_rel_type), ...].

    Accepts both shapes for backwards compatibility:
    - `["per_1", "per_2"]`  — bare IDs, all get `default_type`.
    - `[{"person_id": "per_1", "relation_type": "raised_by"}, ...]` — typed.
      Any of `id_keys` works as the ID field name; `relation_type` defaults
      to `default_type` when omitted.

    Returns tuples of `(id, UPPER_SNAKE_CASE_relation)`. Invalid items are
    silently dropped (not worth surfacing — callers shouldn't have to handle
    malformed lists).
    """
    default_safe = _sanitize_relation_type(default_type)
    out: list[tuple[str, str]] = []
    for item in items or []:
        if isinstance(item, str):
            if item:
                out.append((item, default_safe))
        elif isinstance(item, dict):
            raw_id = None
            for k in id_keys:
                if item.get(k):
                    raw_id = item[k]
                    break
            if not raw_id:
                continue
            rel_type = item.get("relation_type")
            safe = _sanitize_relation_type(rel_type) if rel_type else default_safe
            out.append((raw_id, safe))
    return out


def _group_by_relation(items: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Group normalized `(id, rel_type)` pairs by rel_type → list of IDs.

    Lets the link helpers emit one UNWIND-MERGE per rel_type instead of one
    query per item. For the common case (all items share the default type),
    this collapses to a single query.
    """
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for item_id, rel_type in items:
        groups[rel_type].append(item_id)
    return dict(groups)


async def _resolve_caller_person(driver, caller: str, is_company: bool) -> str:
    """Find-or-create the caller's :Person node. Return its canonical primary_user_id.

    First tries to match an existing :Person whose `primary_user_id` or
    `aliases` list contains the raw caller id. If found, returns that node's
    canonical primary_user_id — subsequent MATCH queries then resolve to the
    same node even when the caller's transport uses an alias identifier.

    If no match, auto-provisions a new stub :Person with the raw caller id as
    its primary_user_id and the appropriate scope label.

    This is the prevention layer for duplicate :Person nodes. Duplicates that
    already exist (e.g. 'Telegram: 330959414' vs 'tg_330959414' from the
    pre-alias era) are merged via the admin-only `merge_persons` tool.
    """
    if not caller or caller == "unknown":
        return caller

    lookup_query = (
        "MATCH (p:Person) "
        "WHERE p.primary_user_id = $caller "
        "   OR $caller IN coalesce(p.aliases, []) "
        "RETURN p.primary_user_id AS canonical LIMIT 1"
    )
    async with driver.session() as session:
        result = await session.run(lookup_query, caller=caller)
        row = await result.single()
        # Use .get to survive mock results that don't include the aliased key;
        # production Neo4j always does, but this is cheap defensive code.
        canonical = row.get("canonical") if row else None
        if canonical:
            return canonical

    scope_label = _SCOPE_TO_PERSON_LABEL["professional" if is_company else "personal"]
    person_id = _auto_person_id(caller)
    # author_user_id is set to the caller's own primary_user_id — an
    # auto-provisioned stub is self-authored. Keeps the ACL invariant
    # (every :Person has an author_user_id) from leaking.
    create_query = (
        "MERGE (p:Person {primary_user_id: $caller}) "
        "ON CREATE SET "
        "    p.person_id = $person_id, "
        "    p.full_name = $caller, "
        "    p.aliases = [], "
        "    p.is_auto_provisioned = true, "
        "    p.author_user_id = $caller, "
        "    p.created_at = datetime(), "
        "    p.updated_at = datetime() "
        f"SET p:{scope_label}"
    )
    async with driver.session() as session:
        await session.run(create_query, caller=caller, person_id=person_id)
    return caller


# ---------------------------------------------------------------------------
# Identity + duplicate enforcement (code-level, not LLM-decided)
# ---------------------------------------------------------------------------

# Callers are considered "identified" when their :Person has a first_name set,
# OR when the node was curated (not auto-provisioned). A bare auto-provisioned
# stub with only a platform user_id is blocked from creating memories / people
# until it's enriched via update_person(first_name=..., last_name=...).
#
# This is a hard gate (no bypass flag) because anonymous memories are exactly
# what the authorship model is trying to prevent.


async def _get_person_info(driver, primary_user_id: str) -> dict | None:
    if not primary_user_id:
        return None
    query = (
        "MATCH (p:Person {primary_user_id: $id}) "
        "RETURN p.person_id AS person_id, "
        "       p.first_name AS first_name, "
        "       p.last_name AS last_name, "
        "       p.full_name AS full_name, "
        "       p.is_auto_provisioned AS is_auto_provisioned "
        "LIMIT 1"
    )
    async with driver.session() as session:
        result = await session.run(query, id=primary_user_id)
        row = await result.single()
        return dict(row) if row else None


def _has_identity(info: dict | None) -> bool:
    """True when the caller's :Person has enough identity to attribute writes."""
    if not info:
        return False
    if not info.get("is_auto_provisioned"):
        return True  # Curated via create_person → always identified.
    first = (info.get("first_name") or "").strip()
    return bool(first)


def _needs_identity_response(info: dict, caller_user_id: str) -> dict:
    pid = (info or {}).get("person_id", "unknown")
    return {
        "status": "needs_identity",
        "message": (
            f"Cannot write: caller `{caller_user_id}` is not identified yet. "
            f"Ask the user for their first and last name, then call "
            f"`update_person(person_id=\"{pid}\", updates='{{\"first_name\": \"...\", \"last_name\": \"...\"}}')`. "
            "Do NOT retry the write until that succeeds. This is a hard gate — there is no bypass."
        ),
        "person_id": pid,
        "caller_user_id": caller_user_id,
    }


async def _find_possible_duplicates(
    driver, first_name: str, last_name: str, role: str, user_ids_list: list,
) -> list[dict]:
    """Return existing :Person nodes that may represent the same human.

    Matches on any of:
    - same first_name (case-insensitive, curated records only)
    - any shared id_value from the proposed `user_ids_list` appearing
      anywhere in a stored `user_ids` JSON blob (substring match)

    Intentionally broad — the caller decides via the returned list whether
    a match is a genuine duplicate. Only excludes auto-provisioned stubs
    (those don't have first_name + role; they're not duplicates-worth-asking).
    """
    id_values = [
        item.get("id_value") for item in (user_ids_list or [])
        if isinstance(item, dict) and item.get("id_value")
    ]
    query = (
        "MATCH (p:Person) "
        "WHERE p.is_auto_provisioned = false "
        "  AND ("
        "    toLower(coalesce(p.first_name, '')) = toLower($first_name) "
        "    OR (size($id_values) > 0 AND "
        "        any(v IN $id_values WHERE coalesce(p.user_ids, '') CONTAINS v))"
        "  ) "
        "RETURN p.person_id AS person_id, "
        "       p.full_name AS full_name, "
        "       p.first_name AS first_name, "
        "       p.last_name AS last_name, "
        "       p.role AS role, "
        "       p.user_ids AS user_ids, "
        "       labels(p) AS labels "
        "LIMIT 5"
    )
    async with driver.session() as session:
        result = await session.run(
            query, first_name=first_name, id_values=id_values,
        )
        return [dict(r) async for r in result]


# Memory semantic-duplicate threshold. Cosine similarity on the
# text-embedding-3-small embedding space. 0.92 was picked to catch near-
# paraphrases of the same fact without blocking legitimate follow-up records
# that share vocabulary but carry different content. Tune per observation.
_MEMORY_DUPLICATE_THRESHOLD = 0.92


async def _find_semantic_duplicate_memory(
    driver, text: str, namespace: str, openai_key: str,
    threshold: float = _MEMORY_DUPLICATE_THRESHOLD,
) -> list[dict]:
    """Return memories in `namespace` whose embedding is >= threshold similar to `text`.

    Uses the same inline `genai.vector.encode` + `db.index.vector.queryNodes`
    path as `search_knowledge`. Top 3 matches above threshold are returned,
    highest first. Returns an empty list on any error (empty index, plugin
    unavailable, network hiccup) — the check is a convenience, not a hard
    invariant, and a failed dedup should not block a legitimate write.
    """
    index_name = _MEMORY_INDEX_BY_NAMESPACE[namespace]
    query = (
        "WITH genai.vector.encode($text, 'OpenAI', "
        "{token: $openai_key, model: 'text-embedding-3-small'}) AS vec "
        f"CALL db.index.vector.queryNodes('{index_name}', 3, vec) "
        "YIELD node, score "
        "WHERE score >= $threshold "
        "RETURN node.record_id AS record_id, "
        "       node.short_description AS short_description, "
        "       substring(coalesce(node.text, ''), 0, 200) AS text_preview, "
        "       toString(node.created_at) AS created_at, "
        "       score "
        "ORDER BY score DESC"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query, text=text, openai_key=openai_key, threshold=threshold,
            )
            return [dict(r) async for r in result]
    except Exception:
        logger.warning(
            "Semantic duplicate check failed in namespace %r; proceeding without dedup.",
            namespace, exc_info=True,
        )
        return []


_ENTITY_DUPLICATE_THRESHOLD = 0.92


async def _find_semantic_duplicate_entity(
    driver, embed_text: str, openai_key: str,
    threshold: float = _ENTITY_DUPLICATE_THRESHOLD,
) -> list[dict]:
    """Same pattern as `_find_semantic_duplicate_memory` but for :Entity.

    Catches synonym-level duplicates ('Mellanni' vs 'Mellanni Inc.') and the
    common case where the agent reaches for a slightly different entity_type
    label ('company' vs 'business') to refer to the same real thing.
    """
    query = (
        "WITH genai.vector.encode($text, 'OpenAI', "
        "{token: $openai_key, model: 'text-embedding-3-small'}) AS vec "
        f"CALL db.index.vector.queryNodes('{_ENTITY_INDEX_NAME}', 3, vec) "
        "YIELD node, score "
        "WHERE score >= $threshold "
        "RETURN node.entity_id AS entity_id, "
        "       node.name AS name, "
        "       node.entity_type AS entity_type, "
        "       substring(coalesce(node.description, ''), 0, 200) AS description_preview, "
        "       toString(node.created_at) AS created_at, "
        "       score "
        "ORDER BY score DESC"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query, text=embed_text, openai_key=openai_key, threshold=threshold,
            )
            return [dict(r) async for r in result]
    except Exception:
        logger.warning(
            "Entity semantic duplicate check failed; proceeding without dedup.",
            exc_info=True,
        )
        return []


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
    related_entities: list[str] = None,
    author: str = "",
    force_create: bool = False,
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
                        Two shapes accepted:
                        - `["per_1", "per_2"]` — bare IDs, edge type defaults
                          to `:INVOLVES`.
                        - `[{"person_id": "per_1", "relation_type": "raised_by"}, ...]`
                          — typed edges. relation_type sanitized to
                          UPPER_SNAKE_CASE. Useful types: `raised_by`,
                          `decided_by`, `reported_by`, `assigned_to`,
                          `attended_by`, `mentioned`.
        related_memories: Optional list of memory IDs this links to. Same two
                          shapes; default edge type is `:RELATED_TO`. Dict form
                          accepts `memory_id`, `record_id`, or `id` as the key.
                          Useful types: `supersedes`, `follows_up`, `corrects`,
                          `references`.
        related_entities: Optional list of entity IDs this memory is about.
                          Same two shapes; default edge type is `:ABOUT`. Use
                          when the memory references a brand / company /
                          department / product / tool. Check `search_entities`
                          first to find the right IDs.
        author: Advisory only — retained for signature compat. Actual authorship
                is stamped as `author_user_id` on the created node, resolved
                from the caller's :Person node.
        force_create: Set to True ONLY after the user has explicitly confirmed
                that a semantically-similar existing memory flagged by the
                pre-create dedup check is a different record. Default False.
                Do not set True pre-emptively to bypass the check — the whole
                point is user confirmation on near-matches.
    """
    # Intentionally unused — authorship is derived from the caller, not the arg.
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

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    # Hard gate: anonymous stubs must be enriched with a real name before any
    # memory is written. Prevents accumulation of unattributable records.
    caller_info = await _get_person_info(driver, canonical_caller)
    if not _has_identity(caller_info):
        return _needs_identity_response(caller_info, canonical_caller)

    # Semantic dedup gate — vector search the proposed text against the
    # target namespace. If a near-paraphrase already exists, surface it and
    # require the caller to either update the existing record or confirm
    # intent with force_create=True.
    if not force_create:
        dup_matches = await _find_semantic_duplicate_memory(
            driver, text, namespace, openai_key,
        )
        if dup_matches:
            return {
                "status": "possible_duplicate",
                "matches": dup_matches,
                "threshold": _MEMORY_DUPLICATE_THRESHOLD,
                "message": (
                    f"Found {len(dup_matches)} existing record(s) in {namespace} "
                    f"semantically similar (cosine ≥ {_MEMORY_DUPLICATE_THRESHOLD}) "
                    f"to the proposed text. Before creating a new one:\n"
                    f"- If any match is the same knowledge, call `update_record` on "
                    f"that existing `record_id` to merge or refine — don't create a "
                    f"duplicate.\n"
                    f"- If the user (with explicit confirmation) says this is a "
                    f"different record despite the similarity, retry `create_record` "
                    f"with `force_create=true`."
                ),
            }

    memory_label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    record_id = _new_record_id()
    bot_name = _get_bot_name()
    tags = tags or []

    # The MATCH on the caller :Person is retained as an existence check — it
    # guarantees the author is a real, identified Person, and fails the CREATE
    # (no row returned) if the caller node was somehow purged between the
    # identity gate above and this query. Authorship itself is stored as a
    # property on the new :Memory node, not as an edge.
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
        "    author_user_id: caller.primary_user_id, "
        "    via_bot: $bot_name, "
        "    created_at: datetime(), "
        "    updated_at: datetime() "
        "}) "
        "RETURN m.record_id AS record_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                caller_id=canonical_caller,
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
        # already-committed memory node. Each `related_*` list is normalized
        # to `[(id, rel_type), ...]` — bare strings inherit the default
        # (INVOLVES / RELATED_TO / ABOUT), dicts carry their own type.
        if related_people:
            await _link_memory_to_people(
                driver, created_id,
                _normalize_related(related_people, "INVOLVES", ("person_id", "id")),
                canonical_caller,
            )
        if related_memories:
            await _link_memory_to_memories(
                driver, created_id,
                _normalize_related(related_memories, "RELATED_TO", ("memory_id", "record_id", "id")),
                canonical_caller,
            )
        if related_entities:
            await _link_memory_to_entities(
                driver, created_id,
                _normalize_related(related_entities, "ABOUT", ("entity_id", "id")),
                canonical_caller,
            )

        return {
            "status": "success",
            "record_id": created_id,
            "message": f"Record `{created_id}` created in {namespace}: '{short_description}'.",
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
    """Update a record. Only the creator (via `author_user_id`) or admins can modify.

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

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    openai_key = _get_openai_key()

    # Build SET clauses dynamically. Text changes trigger an embedding refresh.
    set_parts = []
    params = {
        "record_id": record_id,
        "caller_id": canonical_caller,
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

    # Admin path skips this creator gate (handled by `update_any_record`).
    # This tool enforces creator-only; admins who own the record also pass.
    query = (
        f"MATCH (m:{label} {{record_id: $record_id}}) "
        "WHERE m.author_user_id = $caller_id "
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
                        f"Record `{record_id}`: this tool enforces creator-only updates. "
                        "Use update_any_record for admin override."
                    )
                return _forbidden(
                    f"Record `{record_id}` was not authored by you. "
                    "Only the creator or admins can modify it."
                )
        return {
            "status": "success",
            "record_id": record_id,
            "updated_fields": sorted(updates_dict),
            "message": f"Record `{record_id}` updated: {', '.join(sorted(updates_dict))}.",
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
    """Admin-only: update a record bypassing the `author_user_id` creator gate.

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
                return _error(f"Record `{record_id}` not found in {namespace}.")
        return {
            "status": "success",
            "record_id": record_id,
            "updated_fields": sorted(updates_dict),
            "message": f"Record `{record_id}` updated (admin override): {', '.join(sorted(updates_dict))}.",
        }
    except Exception as e:
        logger.exception("update_any_record failed")
        return _error(str(e))


async def delete_record(
    record_id: str,
    namespace: str,
    tool_context: ToolContext = None,
) -> dict:
    """Delete a record. Only the creator (via `author_user_id`) or admins can delete.

    Args:
        record_id: ID of the record.
        namespace: One of: personal, professional, technical.
    """
    if namespace not in NAMESPACES:
        return _error(f"Invalid namespace. Must be one of: {', '.join(NAMESPACES)}")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]

    # Admin bypass handled inline: if is_admin, match without the author
    # predicate; otherwise require it.
    if is_admin:
        query = (
            f"MATCH (m:{label} {{record_id: $record_id}}) "
            "DETACH DELETE m "
            "RETURN count(m) AS deleted"
        )
        params = {"record_id": record_id}
    else:
        canonical_caller = await _resolve_caller_person(driver, caller, is_company)
        query = (
            f"MATCH (m:{label} {{record_id: $record_id}}) "
            "WHERE m.author_user_id = $caller_id "
            "DETACH DELETE m "
            "RETURN count(m) AS deleted"
        )
        params = {"record_id": record_id, "caller_id": canonical_caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            deleted = row["deleted"] if row else 0
            if not deleted:
                if is_admin:
                    return _error(f"Record `{record_id}` not found in {namespace}.")
                return _forbidden(
                    f"Record `{record_id}` was not authored by you. "
                    "Only the creator or admins can delete."
                )
        return {
            "status": "success",
            "message": f"Record `{record_id}` deleted from {namespace}.",
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
    force_create: bool = False,
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
        author: Advisory — authorship is stamped as `author_user_id` on the node.
        force_create: Set to True ONLY after the user has explicitly confirmed
                that a near-match found by the pre-create duplicate check is
                actually a different person. Defaults to False. Do not set
                True to bypass the check — the whole point of the check is to
                force a human-in-the-loop confirmation.
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

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    # Hard gate: caller must be identified before creating anything.
    caller_info = await _get_person_info(driver, canonical_caller)
    if not _has_identity(caller_info):
        return _needs_identity_response(caller_info, canonical_caller)

    # Normalize user_ids/relations inputs to JSON strings for storage.
    try:
        user_ids_list = json.loads(user_ids) if isinstance(user_ids, str) else user_ids
    except (json.JSONDecodeError, TypeError):
        user_ids_list = []
    try:
        relations_list = json.loads(relations) if isinstance(relations, str) else relations
    except (json.JSONDecodeError, TypeError):
        relations_list = []

    # Duplicate-prevention gate: unless the caller passes force_create, check
    # for existing :Person records that plausibly represent the same human.
    # See _find_possible_duplicates for the match criteria.
    if not force_create:
        duplicates = await _find_possible_duplicates(
            driver, first_name, last_name or "", role or "", user_ids_list,
        )
        if duplicates:
            return {
                "status": "possible_duplicate",
                "matches": duplicates,
                "message": (
                    f"Found {len(duplicates)} existing person(s) matching first_name="
                    f"'{first_name}' or sharing a user_id. Before creating a new record:\n"
                    f"- If any match is the same human, use `update_person` to enrich the "
                    f"existing record (add missing fields, extra user_ids, or new relations), "
                    f"or `promote_person` to add a scope. DO NOT create a duplicate.\n"
                    f"- If you (with the user's confirmation) are sure this is a different "
                    f"person, retry `create_person` with `force_create=true`."
                ),
            }

    person_id = _new_person_id()
    full_name = f"{first_name} {last_name or ''}".strip()
    bot_name = _get_bot_name()

    # Embedding text includes name + role + identifiers for richer semantic search.
    ids_summary = ", ".join(
        str(item.get("id_value", "")) for item in user_ids_list if isinstance(item, dict)
    )
    embed_text = f"{full_name}. Role: {role}. IDs: {ids_summary}".strip()

    scope_labels = [_SCOPE_TO_PERSON_LABEL[s] for s in sorted(set(scopes))]
    # Build the label suffix as a single Cypher fragment.
    extra_labels = "".join(f":{label}" for label in scope_labels)

    # See create_record: caller MATCH is an existence check on the author's
    # :Person node; authorship itself is stored as a property on the new node.
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
        "    author_user_id: caller.primary_user_id, "
        "    via_bot: $bot_name, "
        "    created_at: datetime(), "
        "    updated_at: datetime() "
        "}) "
        "RETURN p.person_id AS person_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                caller_id=canonical_caller,
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
            await _link_person_relations(driver, created_id, relations_list, canonical_caller)

        return {
            "status": "success",
            "person_id": created_id,
            "scopes": sorted(set(scopes)),
            "message": (
                f"Person `{created_id}` created: {full_name}, "
                f"scope(s): {', '.join(sorted(set(scopes)))}."
            ),
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
    """Update a person record. Only the creator (via `author_user_id`) or admins can modify.

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

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    # Build SET clauses. Name/role changes trigger embedding refresh.
    openai_key = _get_openai_key()
    touches_embedding = bool({"first_name", "last_name", "role", "user_ids"} & updates_dict.keys())
    if touches_embedding and not openai_key:
        return _error("OPENAI_API_KEY required for updates that refresh embeddings.")

    set_parts = []
    params = {"person_id": person_id, "caller_id": canonical_caller, "openai_key": openai_key}
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
        "MATCH (p:Person {person_id: $person_id}) "
        "WHERE p.author_user_id = $caller_id "
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
                        f"Person `{person_id}`: this tool enforces creator-only updates. "
                        "Use update_any_person for admin override."
                    )
                return _forbidden(
                    f"Person `{person_id}` was not authored by you. "
                    "Only the creator or admins can modify."
                )
        return {
            "status": "success",
            "person_id": person_id,
            "updated_fields": sorted(updates_dict),
            "message": f"Person `{person_id}` updated: {', '.join(sorted(updates_dict))}.",
        }
    except Exception as e:
        logger.exception("update_person failed")
        return _error(str(e))


async def update_any_person(
    person_id: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: update a person record bypassing the `author_user_id` creator gate."""
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
                return _error(f"Person `{person_id}` not found.")
        return {
            "status": "success",
            "person_id": person_id,
            "updated_fields": sorted(updates_dict),
            "message": f"Person `{person_id}` updated (admin override): {', '.join(sorted(updates_dict))}.",
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
                return _error(f"Person `{person_id}` not found.")
        return {
            "status": "success",
            "person_id": person_id,
            "labels": row["labels"],
            "message": f"Added scope '{add_scope}' to person `{person_id}`.",
        }
    except Exception as e:
        logger.exception("promote_person failed")
        return _error(str(e))


async def delete_person(
    person_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Delete a person. Only the creator (via `author_user_id`) or admins can delete.

    Use `delete_any_person` for admin override on a person you did not create.

    Args:
        person_id: ID of the person to delete.
    """
    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    if is_admin:
        query = (
            "MATCH (p:Person {person_id: $person_id}) "
            "DETACH DELETE p "
            "RETURN count(p) AS deleted"
        )
        params = {"person_id": person_id}
    else:
        canonical_caller = await _resolve_caller_person(driver, caller, is_company)
        query = (
            "MATCH (p:Person {person_id: $person_id}) "
            "WHERE p.author_user_id = $caller_id "
            "DETACH DELETE p "
            "RETURN count(p) AS deleted"
        )
        params = {"person_id": person_id, "caller_id": canonical_caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            deleted = row["deleted"] if row else 0
            if not deleted:
                if is_admin:
                    return _error(f"Person `{person_id}` not found.")
                return _forbidden(
                    f"Person `{person_id}` was not authored by you. "
                    "Only the creator or admins can delete."
                )
        return {
            "status": "success",
            "message": f"Person `{person_id}` deleted.",
        }
    except Exception as e:
        logger.exception("delete_person failed")
        return _error(str(e))


async def delete_any_person(
    person_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: delete a person bypassing the `author_user_id` creator gate."""
    caller = _get_caller(tool_context)
    is_admin, _ = _get_acl_flags(caller)
    if not is_admin:
        return _forbidden("delete_any_person is admin-only.")

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    query = (
        "MATCH (p:Person {person_id: $person_id}) "
        "DETACH DELETE p "
        "RETURN count(p) AS deleted"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, person_id=person_id)
            row = await result.single()
            deleted = row["deleted"] if row else 0
            if not deleted:
                return _error(f"Person `{person_id}` not found.")
        return {
            "status": "success",
            "message": f"Person `{person_id}` deleted (admin override).",
        }
    except Exception as e:
        logger.exception("delete_any_person failed")
        return _error(str(e))


async def merge_persons(
    canonical_id: str,
    alias_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Admin-only: merge a duplicate :Person node into a canonical one.

    Use this to consolidate records that represent the same real identity —
    e.g. a contact registered twice under slightly different names, or a user
    whose Telegram ID was stored in two different formats ("Telegram: 123"
    and "tg_123") before alias-aware resolution was in place.

    What it does:
    1. Rewrites `author_user_id` on every memory/person the alias authored so
       that it points at the canonical's `primary_user_id` instead.
    2. Reassigns every incoming `:INVOLVES` edge (memories that referenced the
       alias person) to point at the canonical.
    3. Appends alias's `primary_user_id` (and any of its own aliases) to the
       canonical's `aliases` list — future calls from that identifier will
       resolve to the canonical without creating a duplicate.
    4. DETACH DELETEs the alias node. Any other edges on the alias (e.g.
       person-to-person `:RELATED_TO`) are dropped in this step; reassign
       those manually in Neo4j Browser before merging if you need them.

    Args:
        canonical_id: person_id of the record to KEEP.
        alias_id: person_id of the record to MERGE IN (deleted afterwards).
    """
    caller = _get_caller(tool_context)
    is_admin, _ = _get_acl_flags(caller)
    if not is_admin:
        return _forbidden("merge_persons is admin-only.")

    if not canonical_id or not alias_id:
        return _error("Both canonical_id and alias_id are required.")
    if canonical_id == alias_id:
        return _error("canonical_id and alias_id must be different.")

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    # Phase 1: rewrite authorship property on nodes the alias authored, and
    # reassign inbound :INVOLVES edges onto the canonical.
    rewrite_authored = (
        "MATCH (canon:Person {person_id: $canonical_id}), "
        "      (alias:Person {person_id: $alias_id}) "
        "WHERE canon.person_id <> alias.person_id "
        "WITH canon, alias "
        "OPTIONAL MATCH (n) "
        "WHERE (n:Memory OR n:Person) "
        "  AND n.author_user_id = alias.primary_user_id "
        "  AND n <> alias "
        "WITH canon, alias, collect(DISTINCT n) AS targets "
        "FOREACH (t IN targets | SET t.author_user_id = canon.primary_user_id) "
        "RETURN size(targets) AS moved"
    )
    reassign_involves = (
        "MATCH (canon:Person {person_id: $canonical_id}), "
        "      (alias:Person {person_id: $alias_id}) "
        "OPTIONAL MATCH (m:Memory)-[:INVOLVES]->(alias) "
        "WITH canon, alias, collect(DISTINCT m) AS memories "
        "FOREACH (x IN memories | MERGE (x)-[:INVOLVES]->(canon)) "
        "RETURN size(memories) AS moved"
    )

    # Phase 2: append aliases + delete alias node. Filters the new list to
    # exclude the canonical's own primary_user_id (defensive) and null values.
    finalize = (
        "MATCH (canon:Person {person_id: $canonical_id}), "
        "      (alias:Person {person_id: $alias_id}) "
        "WHERE canon.person_id <> alias.person_id "
        "WITH canon, alias, "
        "     [x IN (coalesce(canon.aliases, []) + [alias.primary_user_id] + coalesce(alias.aliases, [])) "
        "      WHERE x IS NOT NULL AND x <> canon.primary_user_id] AS new_aliases "
        "SET canon.aliases = new_aliases, canon.updated_at = datetime() "
        "WITH canon, alias "
        "DETACH DELETE alias "
        "RETURN canon.person_id AS person_id, "
        "       canon.primary_user_id AS primary_user_id, "
        "       canon.aliases AS aliases"
    )

    try:
        async with driver.session() as session:
            r1 = await (await session.run(
                rewrite_authored, canonical_id=canonical_id, alias_id=alias_id
            )).single()
            if r1 is None:
                return _error(
                    f"Canonical `{canonical_id}` or alias `{alias_id}` not found, "
                    "or they're the same node."
                )
            authored_rewritten = r1["moved"]

            r2 = await (await session.run(
                reassign_involves, canonical_id=canonical_id, alias_id=alias_id
            )).single()
            involves_moved = r2["moved"] if r2 else 0

            r3 = await (await session.run(
                finalize, canonical_id=canonical_id, alias_id=alias_id
            )).single()
            if r3 is None:
                return _error("Merge finalization failed after authorship rewrite.")

        return {
            "status": "success",
            "person_id": r3["person_id"],
            "primary_user_id": r3["primary_user_id"],
            "aliases": r3["aliases"],
            "authored_records_rewritten": authored_rewritten,
            "involves_edges_moved": involves_moved,
            "message": (
                f"Merged `{alias_id}` → `{canonical_id}`. "
                f"Rewrote author_user_id on {authored_rewritten} record(s); "
                f"moved {involves_moved} :INVOLVES edge(s). "
                f"Canonical now has {len(r3['aliases'])} alias(es)."
            ),
        }
    except Exception as e:
        logger.exception("merge_persons failed")
        return _error(str(e))


async def relate_persons(
    from_person_id: str,
    to_person_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed directional relationship between two existing people.

    Idempotent: uses MERGE, so re-calling with the same triple is a no-op.
    ACL: admin, or the author (`author_user_id`) of the `from` person. Author
    of `to` alone is NOT sufficient — the edge is outgoing from `from`, so
    it's considered a modification of `from`'s relationships.

    Use this when the user describes a post-hoc relationship between people
    who already exist as :Person nodes. Example: "Ruslan reports to Sergey"
    → `relate_persons(from='per_ruslan', to='per_sergey', relation_type='reports_to')`.

    For brand-new people, prefer `create_person(..., relations=[...])` which
    wires the edge in the same transaction as the node creation.

    Args:
        from_person_id: source :Person.person_id (the owner of the outgoing edge).
        to_person_id: target :Person.person_id.
        relation_type: Human-readable relation, e.g. 'manages', 'reports_to',
                       'spouse_of'. Sanitized to UPPER_SNAKE_CASE; invalid
                       forms fall back to 'RELATED_TO'.
    """
    if not from_person_id or not to_person_id:
        return _error("Both from_person_id and to_person_id are required.")
    if from_person_id == to_person_id:
        return _error("from_person_id and to_person_id must differ.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)
    safe = _sanitize_relation_type(relation_type)

    # ACL gate. Admin path matches both nodes unconditionally; non-admin
    # must author the `from` person. A forbidden/missing match returns no
    # row, which we distinguish from "nodes not found" via a follow-up
    # existence check.
    if is_admin:
        query = (
            "MATCH (a:Person {person_id: $from_id}), (b:Person {person_id: $to_id}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            "RETURN a.person_id AS from_id, b.person_id AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )
        params = {"from_id": from_person_id, "to_id": to_person_id, "caller": canonical_caller}
    else:
        query = (
            "MATCH (a:Person {person_id: $from_id}) "
            "WHERE a.author_user_id = $caller "
            "MATCH (b:Person {person_id: $to_id}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            "RETURN a.person_id AS from_id, b.person_id AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )
        params = {"from_id": from_person_id, "to_id": to_person_id, "caller": canonical_caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                # Distinguish forbidden-vs-not-found for the non-admin case.
                if not is_admin:
                    existence_q = (
                        "MATCH (a:Person {person_id: $from_id}) "
                        "RETURN a.author_user_id AS author_user_id"
                    )
                    e_result = await session.run(existence_q, from_id=from_person_id)
                    e_row = await e_result.single()
                    if e_row is None:
                        return _error(f"Person `{from_person_id}` not found.")
                    return _forbidden(
                        f"Person `{from_person_id}` was not authored by you. "
                        "Only the author or admins can add outgoing relations."
                    )
                return _error(
                    f"Person `{from_person_id}` or `{to_person_id}` not found."
                )
            return {
                "status": "success",
                "from_person_id": row["from_id"],
                "to_person_id": row["to_id"],
                "relation_type": row["relation_type"],
                "outcome": row["outcome"],
                "message": (
                    f"Relation {row['from_id']} -[:{row['relation_type']}]-> {row['to_id']} "
                    f"{row['outcome']}."
                ),
            }
    except Exception as e:
        logger.exception("relate_persons failed")
        return _error(str(e))


# ---------------------------------------------------------------------------
# Entity tools — referable-thing nodes (brand, company, department, product,
# project, location, tool, etc.). Distinct from :Memory (observations) and
# :Person (actors). entity_type is a canonicalized property (snake_case), not
# a sublabel; add-a-new-type is zero-friction for the agent.
# ---------------------------------------------------------------------------


def _entity_embed_text(name: str, entity_type: str, description: str) -> str:
    """Canonical text fed to the embedding model for :Entity nodes."""
    parts = [name.strip()]
    if entity_type:
        parts.append(f"Type: {entity_type}")
    if description:
        parts.append(description.strip())
    return ". ".join(p for p in parts if p)


async def create_entity(
    entity_type: str,
    name: str,
    description: str = "",
    tags: list[str] = None,
    related_entities: list[str] = None,
    related_people: list[str] = None,
    force_create: bool = False,
    tool_context: ToolContext = None,
) -> dict:
    """Create a referable :Entity node (brand, company, department, product, etc.).

    Use this when the content you want to represent is a *thing other things
    point at*, not an observation. Brands, companies, departments, projects,
    products, tools, marketplaces — these are entities. Meetings, decisions,
    incidents, best-practices — these are memories; use `create_record`.

    Args:
        entity_type: Free-form type label. Will be canonicalized to snake_case
                     (e.g. 'Brand' → 'brand'). Prefer re-using types you've
                     seen via `search_entities` before inventing a new one.
        name: Human-readable display name.
        description: Optional longer context — what the entity is, why it
                     matters, how it's used. Embedded alongside the name.
        tags: Optional keyword list for filtered search.
        related_entities: Optional list of related entities. Accepts bare IDs
                          (default edge type `:RELATED_TO`) or typed dicts
                          `[{"entity_id": "ent_...", "relation_type": "part_of"}]`.
        related_people: Optional list of related people. Accepts bare IDs
                        (default edge type `:INVOLVES`) or typed dicts
                        `[{"person_id": "per_...", "relation_type": "led_by"}]`.
        force_create: Set to True only after the user confirms a possible-
                      duplicate (cosine ≥ 0.92) is a genuinely different
                      entity. Default False.

    Returns `{"status": "possible_duplicate", "matches": [...]}` on near-
    matches — inspect and either update the existing entity or retry with
    force_create=True.
    """
    if not name or not name.strip():
        return _error("`name` is required.")
    canonical_type = _canonicalize_entity_type(entity_type)
    if not canonical_type:
        return _error("`entity_type` is required (e.g. 'brand', 'company').")

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

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)
    caller_info = await _get_person_info(driver, canonical_caller)
    if not _has_identity(caller_info):
        return _needs_identity_response(caller_info, canonical_caller)

    embed_text = _entity_embed_text(name, canonical_type, description)

    if not force_create:
        dup_matches = await _find_semantic_duplicate_entity(
            driver, embed_text, openai_key,
        )
        if dup_matches:
            return {
                "status": "possible_duplicate",
                "matches": dup_matches,
                "threshold": _ENTITY_DUPLICATE_THRESHOLD,
                "message": (
                    f"Found {len(dup_matches)} existing entity(ies) semantically "
                    f"similar (cosine ≥ {_ENTITY_DUPLICATE_THRESHOLD}) to `{name}`. "
                    "Before creating a new one:\n"
                    "- If any match is the same real thing, use `update_entity` "
                    "to enrich it, or `relate_entities` to link it. DO NOT "
                    "create a duplicate.\n"
                    "- If this is genuinely a different entity, retry "
                    "`create_entity` with `force_create=true`."
                ),
            }

    entity_id = _new_entity_id()
    bot_name = _get_bot_name()
    tags = tags or []
    description = description or ""

    query = (
        "MATCH (caller:Person {primary_user_id: $caller_id}) "
        "WITH caller, {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        "CREATE (e:Entity { "
        "    entity_id: $entity_id, "
        "    name: $name, "
        "    entity_type: $entity_type, "
        "    description: $description, "
        "    tags: $tags, "
        "    embedding: genai.vector.encode($embed_text, 'OpenAI', cfg), "
        "    author_user_id: caller.primary_user_id, "
        "    via_bot: $bot_name, "
        "    created_at: datetime(), "
        "    updated_at: datetime() "
        "}) "
        "RETURN e.entity_id AS entity_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                caller_id=canonical_caller,
                openai_key=openai_key,
                entity_id=entity_id,
                name=name.strip(),
                entity_type=canonical_type,
                description=description,
                tags=tags,
                embed_text=embed_text,
                bot_name=bot_name,
            )
            row = await result.single()
            if not row:
                return _error("Entity creation did not return an ID — caller Person missing?")
            created_id = row["entity_id"]

        if related_entities:
            await _link_entity_to_entities(
                driver, created_id,
                _normalize_related(related_entities, "RELATED_TO", ("entity_id", "id")),
                canonical_caller,
            )
        if related_people:
            await _link_entity_to_people(
                driver, created_id,
                _normalize_related(related_people, "INVOLVES", ("person_id", "id")),
                canonical_caller,
            )

        return {
            "status": "success",
            "entity_id": created_id,
            "entity_type": canonical_type,
            "message": (
                f"Entity `{created_id}` created: {name} (type: {canonical_type})."
            ),
        }
    except Exception as e:
        logger.exception("create_entity failed")
        return _error(str(e))


async def search_entities(
    search_query: str,
    entity_type: str = "",
    top_k: int = 5,
    tool_context: ToolContext = None,
) -> dict:
    """Semantic search across :Entity nodes, optionally filtered by entity_type.

    Args:
        search_query: Natural-language description of what you're looking for.
        entity_type: Optional — when set, only entities with that canonicalized
                     type are returned. Omit or pass '' to search across all types.
        top_k: Max results (default 5, max 20).
    """
    if not search_query or not search_query.strip():
        return _error("`search_query` is required.")

    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    openai_key = _get_openai_key()
    if not openai_key:
        return _error("OPENAI_API_KEY not configured.")

    top_k = max(1, min(int(top_k), 20))
    canonical_type = _canonicalize_entity_type(entity_type) if entity_type else ""

    # Fetch more than top_k when filtering by type, so the type filter
    # doesn't starve results when the top vector hits are other types.
    fetch_k = top_k * 4 if canonical_type else top_k

    query = (
        "WITH $query_text AS q, "
        "     {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        "WITH genai.vector.encode(q, 'OpenAI', cfg) AS vec "
        f"CALL db.index.vector.queryNodes('{_ENTITY_INDEX_NAME}', $fetch_k, vec) "
        "YIELD node, score "
        "WHERE $canonical_type = '' OR node.entity_type = $canonical_type "
        "RETURN node.entity_id AS entity_id, "
        "       node.name AS name, "
        "       node.entity_type AS entity_type, "
        "       substring(coalesce(node.description, ''), 0, 300) AS description_preview, "
        "       node.tags AS tags, "
        "       toString(node.created_at) AS created_at, "
        "       score "
        "ORDER BY score DESC "
        "LIMIT $top_k"
    )
    try:
        async with driver.session() as session:
            result = await session.run(
                query,
                query_text=search_query,
                openai_key=openai_key,
                fetch_k=fetch_k,
                canonical_type=canonical_type,
                top_k=top_k,
            )
            records = [dict(r) async for r in result]
        return {
            "status": "success",
            "count": len(records),
            "entity_type_filter": canonical_type or None,
            "results": records,
        }
    except Exception as e:
        logger.exception("search_entities failed")
        return _error(str(e))


async def update_entity(
    entity_id: str,
    updates: str,
    tool_context: ToolContext = None,
) -> dict:
    """Update an :Entity. Only the creator (via `author_user_id`) or admins can modify.

    Embedding is refreshed automatically if name, entity_type, or description
    change. Admins wanting to force-update should use `update_any_entity`.

    Args:
        entity_id: ID of the entity.
        updates: JSON string. Allowed fields: name, entity_type, description, tags.
    """
    try:
        updates_dict = json.loads(updates) if isinstance(updates, str) else dict(updates)
    except (json.JSONDecodeError, TypeError):
        return _error("Invalid JSON in 'updates' parameter.")

    allowed = {"name", "entity_type", "description", "tags"}
    bad = [k for k in updates_dict if k not in allowed]
    if bad:
        return _error(
            f"Cannot update fields: {', '.join(bad)}. Allowed: {', '.join(sorted(allowed))}"
        )
    if "entity_type" in updates_dict:
        updates_dict["entity_type"] = _canonicalize_entity_type(
            updates_dict["entity_type"]
        )
        if not updates_dict["entity_type"]:
            return _error("`entity_type` cannot be empty.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)
    openai_key = _get_openai_key()
    touches_embedding = bool(
        {"name", "entity_type", "description"} & updates_dict.keys()
    )
    if touches_embedding and not openai_key:
        return _error("OPENAI_API_KEY required for updates that refresh embeddings.")

    set_parts = []
    params = {"entity_id": entity_id, "caller_id": canonical_caller, "openai_key": openai_key}
    for key, value in updates_dict.items():
        set_parts.append(f"e.{key} = ${key}")
        params[key] = value
    if touches_embedding:
        set_parts.append(
            "e.embedding = genai.vector.encode("
            "coalesce(e.name, '') + '. Type: ' + coalesce(e.entity_type, '') + "
            "'. ' + coalesce(e.description, ''), "
            "'OpenAI', "
            "{token: $openai_key, model: 'text-embedding-3-small'})"
        )
    set_parts.append("e.updated_at = datetime()")
    set_clause = ", ".join(set_parts)

    query = (
        "MATCH (e:Entity {entity_id: $entity_id}) "
        "WHERE e.author_user_id = $caller_id "
        f"SET {set_clause} "
        "RETURN e.entity_id AS entity_id"
    )
    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                if is_admin:
                    return _forbidden(
                        f"Entity `{entity_id}`: this tool enforces creator-only updates. "
                        "Admins should implement `update_any_entity` if broader override is needed."
                    )
                return _forbidden(
                    f"Entity `{entity_id}` was not authored by you. "
                    "Only the creator or admins can modify it."
                )
        return {
            "status": "success",
            "entity_id": entity_id,
            "updated_fields": sorted(updates_dict),
            "message": f"Entity `{entity_id}` updated: {', '.join(sorted(updates_dict))}.",
        }
    except Exception as e:
        logger.exception("update_entity failed")
        return _error(str(e))


async def delete_entity(
    entity_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Delete an :Entity. Only the creator (via `author_user_id`) or admins.

    Removes all outgoing and incoming edges (DETACH DELETE).

    Args:
        entity_id: ID of the entity.
    """
    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    if is_admin:
        query = (
            "MATCH (e:Entity {entity_id: $entity_id}) "
            "DETACH DELETE e "
            "RETURN count(e) AS deleted"
        )
        params = {"entity_id": entity_id}
    else:
        canonical_caller = await _resolve_caller_person(driver, caller, is_company)
        query = (
            "MATCH (e:Entity {entity_id: $entity_id}) "
            "WHERE e.author_user_id = $caller_id "
            "DETACH DELETE e "
            "RETURN count(e) AS deleted"
        )
        params = {"entity_id": entity_id, "caller_id": canonical_caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            deleted = row["deleted"] if row else 0
            if not deleted:
                if is_admin:
                    return _error(f"Entity `{entity_id}` not found.")
                return _forbidden(
                    f"Entity `{entity_id}` was not authored by you. "
                    "Only the creator or admins can delete."
                )
        return {
            "status": "success",
            "message": f"Entity `{entity_id}` deleted.",
        }
    except Exception as e:
        logger.exception("delete_entity failed")
        return _error(str(e))


async def relate_entities(
    from_entity_id: str,
    to_entity_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge (:Entity)-[:<REL>]->(:Entity). Idempotent.

    ACL: admin, or the author of the `from` entity. Same pattern as
    `relate_persons`. Re-calling with the same triple is a no-op.

    Typical relations: 'part_of' (department → company), 'owns'
    (brand → products), 'uses' (project → tool), 'located_in' (warehouse →
    market). Sanitized to UPPER_SNAKE_CASE; invalid forms fall back to
    'RELATED_TO'.

    Args:
        from_entity_id: source :Entity.entity_id.
        to_entity_id: target :Entity.entity_id.
        relation_type: Human-readable relation.
    """
    if not from_entity_id or not to_entity_id:
        return _error("Both from_entity_id and to_entity_id are required.")
    if from_entity_id == to_entity_id:
        return _error("from_entity_id and to_entity_id must differ.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)
    safe = _sanitize_relation_type(relation_type)

    if is_admin:
        query = (
            "MATCH (a:Entity {entity_id: $from_id}), (b:Entity {entity_id: $to_id}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            "RETURN a.entity_id AS from_id, b.entity_id AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )
    else:
        query = (
            "MATCH (a:Entity {entity_id: $from_id}) "
            "WHERE a.author_user_id = $caller "
            "MATCH (b:Entity {entity_id: $to_id}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            "RETURN a.entity_id AS from_id, b.entity_id AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )
    params = {"from_id": from_entity_id, "to_id": to_entity_id, "caller": canonical_caller}

    try:
        async with driver.session() as session:
            result = await session.run(query, **params)
            row = await result.single()
            if not row:
                if not is_admin:
                    e_result = await session.run(
                        "MATCH (a:Entity {entity_id: $from_id}) "
                        "RETURN a.author_user_id AS author_user_id",
                        from_id=from_entity_id,
                    )
                    e_row = await e_result.single()
                    if e_row is None:
                        return _error(f"Entity `{from_entity_id}` not found.")
                    return _forbidden(
                        f"Entity `{from_entity_id}` was not authored by you. "
                        "Only the author or admins can add outgoing relations."
                    )
                return _error(
                    f"Entity `{from_entity_id}` or `{to_entity_id}` not found."
                )
            return {
                "status": "success",
                "from_entity_id": row["from_id"],
                "to_entity_id": row["to_id"],
                "relation_type": row["relation_type"],
                "outcome": row["outcome"],
                "message": (
                    f"Relation {row['from_id']} -[:{row['relation_type']}]-> {row['to_id']} "
                    f"{row['outcome']}."
                ),
            }
    except Exception as e:
        logger.exception("relate_entities failed")
        return _error(str(e))


async def _relate_cross_type(
    driver,
    from_label: str,       # 'Person' or 'Entity'
    from_key: str,         # 'person_id' or 'entity_id'
    from_id: str,
    to_label: str,
    to_key: str,
    to_id: str,
    relation_type: str,
    canonical_caller: str,
    is_admin: bool,
) -> dict:
    """Shared core for (:Person)→(:Entity) and (:Entity)→(:Person) edges.

    ACL: admin path matches both nodes unconditionally; non-admin path
    requires the `from` node's `author_user_id` to equal the caller. On
    gate failure the non-admin path does an existence check to return
    `forbidden` (node exists, wrong author) vs `error` (node missing).
    """
    safe = _sanitize_relation_type(relation_type)

    if is_admin:
        query = (
            f"MATCH (a:{from_label} {{{from_key}: $from_id}}), "
            f"      (b:{to_label} {{{to_key}: $to_id}}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            f"RETURN a.{from_key} AS from_id, b.{to_key} AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )
    else:
        query = (
            f"MATCH (a:{from_label} {{{from_key}: $from_id}}) "
            "WHERE a.author_user_id = $caller "
            f"MATCH (b:{to_label} {{{to_key}: $to_id}}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller "
            "ON MATCH SET r.last_confirmed_at = datetime() "
            f"RETURN a.{from_key} AS from_id, b.{to_key} AS to_id, "
            "       type(r) AS relation_type, "
            "       CASE WHEN r.last_confirmed_at IS NULL THEN 'created' ELSE 'existing' END AS outcome"
        )

    params = {"from_id": from_id, "to_id": to_id, "caller": canonical_caller}
    async with driver.session() as session:
        result = await session.run(query, **params)
        row = await result.single()
        if not row:
            if not is_admin:
                existence_q = (
                    f"MATCH (a:{from_label} {{{from_key}: $from_id}}) "
                    "RETURN a.author_user_id AS author_user_id"
                )
                e_result = await session.run(existence_q, from_id=from_id)
                e_row = await e_result.single()
                if e_row is None:
                    return _error(f"{from_label} `{from_id}` not found.")
                return _forbidden(
                    f"{from_label} `{from_id}` was not authored by you. "
                    "Only the author or admins can add outgoing relations."
                )
            return _error(f"{from_label} `{from_id}` or {to_label} `{to_id}` not found.")
        return {
            "status": "success",
            "from_id": row["from_id"],
            "to_id": row["to_id"],
            "relation_type": row["relation_type"],
            "outcome": row["outcome"],
            "message": (
                f"Relation {row['from_id']} -[:{row['relation_type']}]-> {row['to_id']} "
                f"{row['outcome']}."
            ),
        }


async def relate_person_to_entity(
    from_person_id: str,
    to_entity_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge (:Person)-[:<REL>]->(:Entity). Idempotent.

    Use this when the person is the grammatical subject of the relation —
    typical cases: 'owns', 'works_at', 'manages', 'uses', 'runs', 'leads'.
    Example: "Igor owns Poluco" → `relate_person_to_entity(from='per_igor',
    to='ent_poluco', relation_type='owns')` creating
    `(:Person {Igor})-[:OWNS]->(:Entity {Poluco})`.

    For the reverse direction (entity is subject, e.g. "Amazon Dept is led by
    Sergey"), use `relate_entity_to_person`.

    ACL: admin or author of the `from_person_id`. Re-calling with the same
    triple is a no-op (MERGE).

    Args:
        from_person_id: source :Person.person_id.
        to_entity_id: target :Entity.entity_id.
        relation_type: Human-readable relation. Sanitized to UPPER_SNAKE_CASE;
                       invalid forms fall back to 'RELATED_TO'.
    """
    if not from_person_id or not to_entity_id:
        return _error("Both from_person_id and to_entity_id are required.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    try:
        result = await _relate_cross_type(
            driver,
            from_label="Person", from_key="person_id", from_id=from_person_id,
            to_label="Entity", to_key="entity_id", to_id=to_entity_id,
            relation_type=relation_type,
            canonical_caller=canonical_caller,
            is_admin=is_admin,
        )
        # Normalize payload keys for this tool's caller-facing shape.
        if result.get("status") == "success":
            return {
                "status": "success",
                "from_person_id": result["from_id"],
                "to_entity_id": result["to_id"],
                "relation_type": result["relation_type"],
                "outcome": result["outcome"],
                "message": result["message"],
            }
        return result
    except Exception as e:
        logger.exception("relate_person_to_entity failed")
        return _error(str(e))


async def relate_entity_to_person(
    from_entity_id: str,
    to_person_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge (:Entity)-[:<REL>]->(:Person). Idempotent.

    Use this when the entity is the grammatical subject — typical cases:
    'led_by', 'employs', 'owned_by', 'contracted_with'. Example: "Amazon
    Department is led by Sergey" → `relate_entity_to_person(from='ent_amz',
    to='per_sergey', relation_type='led_by')` creating
    `(:Entity {Amazon Dept})-[:LED_BY]->(:Person {Sergey})`.

    For the reverse direction (person is subject), use
    `relate_person_to_entity`.

    ACL: admin or author of the `from_entity_id`. Re-calling with the same
    triple is a no-op (MERGE).

    Args:
        from_entity_id: source :Entity.entity_id.
        to_person_id: target :Person.person_id.
        relation_type: Human-readable relation. Sanitized to UPPER_SNAKE_CASE;
                       invalid forms fall back to 'RELATED_TO'.
    """
    if not from_entity_id or not to_person_id:
        return _error("Both from_entity_id and to_person_id are required.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    try:
        result = await _relate_cross_type(
            driver,
            from_label="Entity", from_key="entity_id", from_id=from_entity_id,
            to_label="Person", to_key="person_id", to_id=to_person_id,
            relation_type=relation_type,
            canonical_caller=canonical_caller,
            is_admin=is_admin,
        )
        if result.get("status") == "success":
            return {
                "status": "success",
                "from_entity_id": result["from_id"],
                "to_person_id": result["to_id"],
                "relation_type": result["relation_type"],
                "outcome": result["outcome"],
                "message": result["message"],
            }
        return result
    except Exception as e:
        logger.exception("relate_entity_to_person failed")
        return _error(str(e))


async def relate_memory_to_person(
    from_memory_id: str,
    to_person_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge (:Memory)-[:<REL>]->(:Person). Idempotent.

    Use this when adding a person-link to an existing memory with semantics
    richer than the generic `:INVOLVES` that `create_record(related_people=...)`
    provides. Examples:
    - *"Igor raised that incident about Alexa"* → add
      `(:Memory incident)-[:RAISED_BY]->(:Person Igor)`.
    - *"Sergey decided the DHL switch"* → add
      `(:Memory DHL decision)-[:DECIDED_BY]->(:Person Sergey)`.

    For generic "this memory involves this person" links, prefer passing
    `related_people` on `create_record` at creation time. Use this tool when
    the memory already exists, or when you need a non-default edge type.

    ACL: admin or author of the memory. Idempotent (MERGE).

    Args:
        from_memory_id: source :Memory.record_id.
        to_person_id: target :Person.person_id.
        relation_type: Human-readable relation (e.g. 'raised_by',
                       'decided_by', 'assigned_to'). Sanitized to
                       UPPER_SNAKE_CASE; invalid forms → 'RELATED_TO'.
    """
    if not from_memory_id or not to_person_id:
        return _error("Both from_memory_id and to_person_id are required.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    try:
        result = await _relate_cross_type(
            driver,
            from_label="Memory", from_key="record_id", from_id=from_memory_id,
            to_label="Person", to_key="person_id", to_id=to_person_id,
            relation_type=relation_type,
            canonical_caller=canonical_caller,
            is_admin=is_admin,
        )
        if result.get("status") == "success":
            return {
                "status": "success",
                "from_memory_id": result["from_id"],
                "to_person_id": result["to_id"],
                "relation_type": result["relation_type"],
                "outcome": result["outcome"],
                "message": result["message"],
            }
        return result
    except Exception as e:
        logger.exception("relate_memory_to_person failed")
        return _error(str(e))


async def relate_memory_to_entity(
    from_memory_id: str,
    to_entity_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge (:Memory)-[:<REL>]->(:Entity). Idempotent.

    Use this when adding an entity-link to an existing memory with semantics
    richer than the default `:ABOUT`. Examples:
    - *"This incident affected the Mellanni brand"* → add
      `(:Memory)-[:AFFECTED]->(:Entity Mellanni)`.
    - *"This memory contradicts what we documented about Helium 10"* → add
      `(:Memory)-[:CONTRADICTS_DOCS_ABOUT]->(:Entity Helium 10)`.

    For generic "this memory is about this entity", prefer
    `related_entities` on `create_record`. Use this tool for non-default
    edge types or post-hoc additions.

    ACL: admin or author of the memory. Idempotent (MERGE).

    Args:
        from_memory_id: source :Memory.record_id.
        to_entity_id: target :Entity.entity_id.
        relation_type: Sanitized to UPPER_SNAKE_CASE; invalid → 'RELATED_TO'.
    """
    if not from_memory_id or not to_entity_id:
        return _error("Both from_memory_id and to_entity_id are required.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    try:
        result = await _relate_cross_type(
            driver,
            from_label="Memory", from_key="record_id", from_id=from_memory_id,
            to_label="Entity", to_key="entity_id", to_id=to_entity_id,
            relation_type=relation_type,
            canonical_caller=canonical_caller,
            is_admin=is_admin,
        )
        if result.get("status") == "success":
            return {
                "status": "success",
                "from_memory_id": result["from_id"],
                "to_entity_id": result["to_id"],
                "relation_type": result["relation_type"],
                "outcome": result["outcome"],
                "message": result["message"],
            }
        return result
    except Exception as e:
        logger.exception("relate_memory_to_entity failed")
        return _error(str(e))


async def relate_memories(
    from_memory_id: str,
    to_memory_id: str,
    relation_type: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create or reaffirm a typed edge between two existing memories. Idempotent.

    Use this for typed memory-to-memory links beyond the default `:RELATED_TO`
    that `related_memories` creates. Examples:
    - `supersedes` — new SOP replaces an old one.
    - `follows_up` — incident progression.
    - `corrects` — a correction record refers back to the wrong one.
    - `references` — a procedure references a policy.

    ACL: admin or author of the `from` memory. Idempotent (MERGE).

    Args:
        from_memory_id: source :Memory.record_id.
        to_memory_id: target :Memory.record_id.
        relation_type: Sanitized to UPPER_SNAKE_CASE; invalid → 'RELATED_TO'.
    """
    if not from_memory_id or not to_memory_id:
        return _error("Both from_memory_id and to_memory_id are required.")
    if from_memory_id == to_memory_id:
        return _error("from_memory_id and to_memory_id must differ.")

    caller = _get_caller(tool_context)
    if caller == "unknown":
        return _error("Could not resolve caller identity from tool context.")

    is_admin, is_company = _get_acl_flags(caller)
    driver = await _ready_driver()
    if driver is None:
        return _error("Neo4j not configured.")

    canonical_caller = await _resolve_caller_person(driver, caller, is_company)

    try:
        result = await _relate_cross_type(
            driver,
            from_label="Memory", from_key="record_id", from_id=from_memory_id,
            to_label="Memory", to_key="record_id", to_id=to_memory_id,
            relation_type=relation_type,
            canonical_caller=canonical_caller,
            is_admin=is_admin,
        )
        if result.get("status") == "success":
            return {
                "status": "success",
                "from_memory_id": result["from_id"],
                "to_memory_id": result["to_id"],
                "relation_type": result["relation_type"],
                "outcome": result["outcome"],
                "message": result["message"],
            }
        return result
    except Exception as e:
        logger.exception("relate_memories failed")
        return _error(str(e))


# ---------------------------------------------------------------------------
# Relationship helpers (internal — called from create_record / create_person)
# ---------------------------------------------------------------------------


async def _link_memory_to_people(
    driver, record_id: str, items: list[tuple[str, str]], author_caller: str
) -> None:
    """Create (:Memory)-[:<REL>]->(:Person) edges.

    `items` is the output of `_normalize_related(...)` — list of pre-sanitized
    `(person_id, rel_type)` tuples. Items are grouped by rel_type and emitted
    as one UNWIND-MERGE query per unique type. Default type (INVOLVES) is
    applied at normalize time, not here.
    """
    if not items:
        return
    for rel_type, ids in _group_by_relation(items).items():
        query = (
            "MATCH (m:Memory {record_id: $record_id}) "
            "UNWIND $people AS pid "
            "MATCH (p:Person {person_id: pid}) "
            f"MERGE (m)-[r:{rel_type}]->(p) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, record_id=record_id, people=ids, caller=author_caller)
        except Exception as e:
            logger.warning(
                "Failed to link memory %s to people (%s): %s", record_id, rel_type, e,
            )


async def _link_memory_to_memories(
    driver, record_id: str, items: list[tuple[str, str]], author_caller: str
) -> None:
    """Create (:Memory)-[:<REL>]->(:Memory) edges. See `_link_memory_to_people`."""
    if not items:
        return
    for rel_type, ids in _group_by_relation(items).items():
        query = (
            "MATCH (m:Memory {record_id: $record_id}) "
            "UNWIND $others AS oid "
            "MATCH (o:Memory {record_id: oid}) "
            f"MERGE (m)-[r:{rel_type}]->(o) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, record_id=record_id, others=ids, caller=author_caller)
        except Exception as e:
            logger.warning(
                "Failed to link memory %s to memories (%s): %s", record_id, rel_type, e,
            )


async def _link_memory_to_entities(
    driver, record_id: str, items: list[tuple[str, str]], author_caller: str
) -> None:
    """Create (:Memory)-[:<REL>]->(:Entity) edges. Default rel_type is ABOUT."""
    if not items:
        return
    for rel_type, ids in _group_by_relation(items).items():
        query = (
            "MATCH (m:Memory {record_id: $record_id}) "
            "UNWIND $entities AS eid "
            "MATCH (e:Entity {entity_id: eid}) "
            f"MERGE (m)-[r:{rel_type}]->(e) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, record_id=record_id, entities=ids, caller=author_caller)
        except Exception as e:
            logger.warning(
                "Failed to link memory %s to entities (%s): %s", record_id, rel_type, e,
            )


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
        safe = _sanitize_relation_type(rel_type)
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


async def _link_entity_to_entities(
    driver, entity_id: str, items: list[tuple[str, str]], author_caller: str
) -> None:
    """Create (:Entity)-[:<REL>]->(:Entity) edges. Default rel_type is RELATED_TO."""
    if not items:
        return
    for rel_type, ids in _group_by_relation(items).items():
        query = (
            "MATCH (e:Entity {entity_id: $entity_id}) "
            "UNWIND $others AS oid "
            "MATCH (o:Entity {entity_id: oid}) "
            "WHERE o.entity_id <> e.entity_id "
            f"MERGE (e)-[r:{rel_type}]->(o) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, entity_id=entity_id, others=ids, caller=author_caller)
        except Exception as e:
            logger.warning(
                "Failed to link entity %s to entities (%s): %s", entity_id, rel_type, e,
            )


async def _link_entity_to_people(
    driver, entity_id: str, items: list[tuple[str, str]], author_caller: str
) -> None:
    """Create (:Entity)-[:<REL>]->(:Person) edges. Default rel_type is INVOLVES."""
    if not items:
        return
    for rel_type, ids in _group_by_relation(items).items():
        query = (
            "MATCH (e:Entity {entity_id: $entity_id}) "
            "UNWIND $people AS pid "
            "MATCH (p:Person {person_id: pid}) "
            f"MERGE (e)-[r:{rel_type}]->(p) "
            "ON CREATE SET r.created_at = datetime(), r.link_author_user_id = $caller"
        )
        try:
            async with driver.session() as session:
                await session.run(query, entity_id=entity_id, people=ids, caller=author_caller)
        except Exception as e:
            logger.warning(
                "Failed to link entity %s to people (%s): %s", entity_id, rel_type, e,
            )
