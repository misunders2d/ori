"""Neo4j schema initializer for the consolidated memory + knowledge graph.

Idempotent — all statements use `IF NOT EXISTS`, so `ensure_schema(driver)` is
safe to call repeatedly. A module-level flag plus an asyncio.Lock make
concurrent first-calls from multiple tool paths serialize correctly.

Label convention
----------------
Every memory node carries two labels: the generic kind-label `:Memory` plus
exactly one scope label from `:PersonalMemory`, `:ProfessionalMemory`,
`:TechnicalMemory`. Every person node carries `:Person` plus one or two of
`:PersonalPerson`, `:ProfessionalPerson` (dual-scope people have both).

Vector indexes live on the scope-specific labels because Neo4j vector indexes
are single-label — scoping at the index level prevents cross-namespace leaks
even if the Python ACL wrapper has a bug. The full-text index lives on the
generic `:Memory` label so admin ad-hoc keyword search spans all scopes in
one call.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Vector indexes use OpenAI text-embedding-3-small via Neo4j's GenAI plugin
# (genai.vector.encode — or ai.text.embed on 2025.11+). 1536 is the model's
# native dimensionality.
_VECTOR_DIMS = 1536
_VECTOR_SIM = "cosine"

# Three memory scope labels + two person scope labels.
_MEMORY_SCOPE_LABELS = ("PersonalMemory", "ProfessionalMemory", "TechnicalMemory")
_PERSON_SCOPE_LABELS = ("PersonalPerson", "ProfessionalPerson")


def _vector_index_name(scope_label: str) -> str:
    """Map scope label → index name. `PersonalMemory` → `personal_memory_embedding`."""
    # Convert CamelCase to snake_case + _embedding suffix.
    out = [scope_label[0].lower()]
    for ch in scope_label[1:]:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out) + "_embedding"


_schema_ensured = False
_schema_lock: asyncio.Lock | None = None


async def ensure_schema(driver) -> None:
    """Create all constraints and indexes the consolidated graph needs.

    Idempotent — re-runs are no-ops after the first successful completion in
    the current process. Pass the Neo4j async driver; must be called before
    any memory/person tool executes.
    """
    global _schema_ensured, _schema_lock
    if _schema_ensured:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ensured:
            return
        await _run_schema_statements(driver)
        _schema_ensured = True
        logger.info("Neo4j schema ensured (constraints + vector + full-text indexes)")


async def _run_schema_statements(driver) -> None:
    """The actual schema DDL. Split out for testability."""
    statements: list[str] = []

    # Uniqueness constraints — one per entity type, on the human-readable ID.
    # The caller-Person auto-provisioning path relies on primary_user_id being
    # unique to make MERGE deterministic.
    statements.append(
        "CREATE CONSTRAINT memory_record_id_unique IF NOT EXISTS "
        "FOR (m:Memory) REQUIRE m.record_id IS UNIQUE"
    )
    statements.append(
        "CREATE CONSTRAINT person_person_id_unique IF NOT EXISTS "
        "FOR (p:Person) REQUIRE p.person_id IS UNIQUE"
    )
    statements.append(
        "CREATE CONSTRAINT person_primary_user_id_unique IF NOT EXISTS "
        "FOR (p:Person) REQUIRE p.primary_user_id IS UNIQUE"
    )

    # Vector indexes — one per scope label, OpenAI text-embedding-3-small shape.
    for scope in _MEMORY_SCOPE_LABELS + _PERSON_SCOPE_LABELS:
        index_name = _vector_index_name(scope)
        statements.append(
            f"CREATE VECTOR INDEX {index_name} IF NOT EXISTS "
            f"FOR (n:{scope}) ON (n.embedding) "
            f"OPTIONS {{indexConfig: {{"
            f"`vector.dimensions`: {_VECTOR_DIMS}, "
            f"`vector.similarity_function`: '{_VECTOR_SIM}'"
            f"}}}}"
        )

    # Full-text index — covers all three memory scopes via the generic :Memory
    # label. Useful for admin ad-hoc keyword browsing in Neo4j Browser.
    statements.append(
        "CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS "
        "FOR (m:Memory) ON EACH [m.text, m.short_description]"
    )

    async with driver.session() as session:
        for stmt in statements:
            try:
                await session.run(stmt)
            except Exception as e:
                # Log and continue — IF NOT EXISTS covers idempotency, but
                # older Neo4j versions may reject the multi-line syntax or
                # certain options. Fail loudly at the end, not mid-loop, so
                # partial state is less likely.
                logger.error("Schema statement failed: %s — %s", stmt, e)
                raise
