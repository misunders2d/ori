"""One-shot migration: Pinecone knowledge base → Neo4j consolidated graph.

Reads every record across the four Pinecone namespaces (personal, professional,
technical, people), re-embeds them via Neo4j's ai.text.embed('OpenAI', ...) and
writes them into the new Neo4j schema defined in `app/core/graph_schema.py`.

The target Neo4j is expected to be empty (or about to be wiped via --purge).
This script is not idempotent in the "re-run safely" sense — it purges and
reloads. Re-running after a full completion recreates identical data; re-running
after a partial failure is also safe because MERGE is used for Person nodes
(though memory records would be duplicated in that case — use --purge to reset).

Usage:
    uv run python scripts/migrate_pinecone_to_neo4j.py --purge
    uv run python scripts/migrate_pinecone_to_neo4j.py --purge --dry-run

Required env:
    PINECONE_API_KEY, PINECONE_INDEX_NAME
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD
    OPENAI_API_KEY
    COMPANY_DOMAIN (for auto-provisioning author Persons to the right scope)
    ADMIN_USER_IDS, BOT_NAME
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Make the project root importable regardless of invocation cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.graph_schema import ensure_schema  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate")


REPORT_PATH = PROJECT_ROOT / "data" / "migration_report.json"

PINECONE_NAMESPACES = ("personal", "professional", "technical", "people")
MEMORY_NAMESPACES = ("personal", "professional", "technical")

_NAMESPACE_TO_MEMORY_LABEL = {
    "personal": "PersonalMemory",
    "professional": "ProfessionalMemory",
    "technical": "TechnicalMemory",
}

_SCOPE_TO_PERSON_LABEL = {
    "personal": "PersonalPerson",
    "professional": "ProfessionalPerson",
}

_LEGACY_AUTHOR_USER_ID = "legacy_unknown"


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


REQUIRED_ENV_VARS = (
    "PINECONE_API_KEY",
    "PINECONE_INDEX_NAME",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "OPENAI_API_KEY",
    "COMPANY_DOMAIN",
    "ADMIN_USER_IDS",
    "BOT_NAME",
)


def assert_env() -> None:
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name, "").strip()]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(2)


def company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def is_company_user(user_id: str) -> bool:
    domain = company_domain()
    return bool(domain) and user_id.lower().endswith(f"@{domain}")


def bot_name() -> str:
    return os.environ.get("BOT_NAME", "").strip() or "unknown_bot"


def openai_key() -> str:
    return os.environ["OPENAI_API_KEY"].strip()


# ---------------------------------------------------------------------------
# Person ID helpers
# ---------------------------------------------------------------------------


def auto_person_id(user_id: str) -> str:
    """Deterministic ID for author Persons provisioned during migration.

    Mirrors the runtime pattern in memory_tools._auto_person_id.
    """
    return f"per_auto_{hashlib.md5(user_id.encode('utf-8')).hexdigest()[:8]}"


def resolve_author_user_id(raw: str | None) -> str:
    """Sanitize a stored Pinecone author field into a canonical user_id.

    Pinecone records carry the caller's platform ID in `user_id` (e.g.
    `tg_330959414`, `sergey@mellanni.com`). Empty/missing values route to the
    legacy sentinel.
    """
    if not raw or raw == "unknown":
        return _LEGACY_AUTHOR_USER_ID
    return raw.strip()


# ---------------------------------------------------------------------------
# Pinecone reader
# ---------------------------------------------------------------------------


async def pinecone_fetch_namespace(namespace: str) -> list[dict]:
    """Page through a namespace and return every record's metadata as a list."""
    from pinecone import PineconeAsyncio

    api_key = os.environ["PINECONE_API_KEY"]
    index_name = os.environ["PINECONE_INDEX_NAME"]

    out: list[dict] = []
    async with PineconeAsyncio(api_key=api_key) as pc:
        descr = await pc.describe_index(index_name)
        if not descr or not descr.host:
            raise RuntimeError(f"Could not describe Pinecone index '{index_name}'.")
        host = descr.host

        async with pc.IndexAsyncio(host) as index:
            # Page through IDs first.
            all_ids: list[str] = []
            results = await index.list_paginated(namespace=namespace, limit=100)
            if results.vectors:
                all_ids.extend(v.id for v in results.vectors)
            while results.pagination:
                results = await index.list_paginated(
                    namespace=namespace,
                    limit=100,
                    pagination_token=results.pagination.next,
                )
                if results.vectors:
                    all_ids.extend(v.id for v in results.vectors)

            # Fetch metadata in batches.
            batch_size = 50
            for i in range(0, len(all_ids), batch_size):
                batch = all_ids[i:i + batch_size]
                fetched = await index.fetch(ids=batch, namespace=namespace)
                for rid, vec in fetched.vectors.items():
                    meta = vec.to_dict().get("metadata", {}) or {}
                    meta["_id"] = rid
                    out.append(meta)

    return out


# ---------------------------------------------------------------------------
# Neo4j writer
# ---------------------------------------------------------------------------


async def connect_neo4j():
    from neo4j import AsyncGraphDatabase

    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USERNAME"]
    password = os.environ["NEO4J_PASSWORD"]
    return AsyncGraphDatabase.driver(uri, auth=(user, password))


async def smoke_test_embedding(driver) -> None:
    """Fail fast if ai.text.embed + OpenAI aren't reachable from Neo4j."""
    query = (
        "WITH ai.text.embed($t, 'OpenAI', $cfg) AS vec "
        "RETURN size(vec) AS dim"
    )
    async with driver.session() as session:
        result = await session.run(
            query,
            t="ping",
            cfg={"token": openai_key(), "model": "text-embedding-3-small"},
        )
        row = await result.single()
        dim = row["dim"] if row else 0
        if dim != 1536:
            raise RuntimeError(
                f"ai.text.embed returned {dim} dims (expected 1536). "
                "Check OPENAI_API_KEY and plugin version."
            )
    logger.info("Embedding smoke test OK (1536 dims).")


async def purge(driver) -> None:
    """Wipe all nodes, relationships, indexes, and constraints."""
    async with driver.session() as session:
        # Drop indexes first (some Neo4j versions require this before dropping
        # constraints they depend on).
        result = await session.run("SHOW INDEXES YIELD name")
        index_names = [r["name"] async for r in result]
        for name in index_names:
            try:
                await session.run(f"DROP INDEX {name} IF EXISTS")
            except Exception as e:
                logger.warning("Could not drop index %s: %s", name, e)

        result = await session.run("SHOW CONSTRAINTS YIELD name")
        constraint_names = [r["name"] async for r in result]
        for name in constraint_names:
            try:
                await session.run(f"DROP CONSTRAINT {name} IF EXISTS")
            except Exception as e:
                logger.warning("Could not drop constraint %s: %s", name, e)

        # Delete everything.
        await session.run("MATCH (n) DETACH DELETE n")
    logger.info("Neo4j purged (nodes, rels, indexes, constraints).")


async def upsert_author_person(driver, user_id: str) -> None:
    """Ensure a :Person node exists for an author's user_id.

    Idempotent via MERGE. Label picked by email-domain match. The sentinel
    legacy author gets :PersonalPerson by default — admins can reclassify
    post-migration.
    """
    scope_label = (
        _SCOPE_TO_PERSON_LABEL["professional"]
        if is_company_user(user_id)
        else _SCOPE_TO_PERSON_LABEL["personal"]
    )
    pid = auto_person_id(user_id)
    query = (
        "MERGE (p:Person {primary_user_id: $user_id}) "
        "ON CREATE SET "
        "    p.person_id = $pid, "
        "    p.full_name = $user_id, "
        "    p.aliases = [], "
        "    p.is_auto_provisioned = true, "
        "    p.created_at = datetime(), "
        "    p.updated_at = datetime() "
        f"SET p:{scope_label}"
    )
    async with driver.session() as session:
        await session.run(query, user_id=user_id, pid=pid)


async def migrate_memory(driver, namespace: str, record: dict, dry_run: bool) -> None:
    """Create one :Memory:<scope> node and wire :AUTHORED from its author."""
    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    record_id = record.get("_id") or record.get("id")
    text = record.get("text") or ""
    short_description = record.get("short_description") or ""
    category = record.get("category") or "knowledge"
    tags = record.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, ValueError):
            tags = [tags]

    # Pinecone stored created_at as unix seconds (int). Preserve it.
    created_at_ts = record.get("created_at")
    try:
        created_at_ts = int(created_at_ts) if created_at_ts is not None else int(time.time())
    except (TypeError, ValueError):
        created_at_ts = int(time.time())

    author_user_id = resolve_author_user_id(record.get("user_id"))
    if dry_run:
        logger.info("[dry-run] would migrate memory %s (author=%s)", record_id, author_user_id)
        return

    # Guarantee the author Person exists before the :AUTHORED edge.
    await upsert_author_person(driver, author_user_id)

    query = (
        "MATCH (author:Person {primary_user_id: $author_user_id}) "
        "WITH author, {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        f"CREATE (m:Memory:{label} {{ "
        "    record_id: $record_id, "
        "    text: $text, "
        "    embedding: ai.text.embed($text, 'OpenAI', cfg), "
        "    short_description: $short_description, "
        "    category: $category, "
        "    tags: $tags, "
        "    created_at: datetime({epochSeconds: $created_at_ts}), "
        "    updated_at: datetime({epochSeconds: $created_at_ts}) "
        "}) "
        "CREATE (author)-[:AUTHORED {via_bot: $bot_name, created_at: datetime(), migrated: true}]->(m) "
        "RETURN m.record_id AS record_id"
    )
    async with driver.session() as session:
        result = await session.run(
            query,
            author_user_id=author_user_id,
            openai_key=openai_key(),
            record_id=record_id,
            text=text,
            short_description=short_description,
            category=category,
            tags=tags,
            created_at_ts=created_at_ts,
            bot_name=bot_name(),
        )
        row = await result.single()
        if not row:
            logger.warning("Memory %s did not return a record_id", record_id)


async def migrate_person(
    driver, record: dict, scopes: list[str], dry_run: bool
) -> None:
    """Create one :Person:<scope(s)> node and wire :AUTHORED from its author."""
    person_id = record.get("_id") or record.get("id")
    first_name = record.get("first_name") or ""
    last_name = record.get("last_name") or ""
    full_name = f"{first_name} {last_name}".strip() or record.get("text", "").strip() or person_id
    role = record.get("role") or ""

    # user_ids is stored as a JSON string in Pinecone.
    raw_user_ids = record.get("user_ids") or "[]"
    if isinstance(raw_user_ids, list):
        user_ids_json = json.dumps(raw_user_ids)
    else:
        user_ids_json = raw_user_ids

    created_at_ts = record.get("created_at")
    try:
        created_at_ts = int(created_at_ts) if created_at_ts is not None else int(time.time())
    except (TypeError, ValueError):
        created_at_ts = int(time.time())

    author_user_id = resolve_author_user_id(record.get("user_id"))
    scope_labels = [_SCOPE_TO_PERSON_LABEL[s] for s in sorted(set(scopes))]
    extra_labels = "".join(f":{lbl}" for lbl in scope_labels)

    # Embedding text: name + role + identifiers
    try:
        parsed_ids = json.loads(user_ids_json)
    except (json.JSONDecodeError, TypeError):
        parsed_ids = []
    ids_summary = ", ".join(
        str(item.get("id_value", "")) for item in parsed_ids if isinstance(item, dict)
    )
    embed_text = f"{full_name}. Role: {role}. IDs: {ids_summary}".strip()

    if dry_run:
        logger.info(
            "[dry-run] would migrate person %s (%s) with scopes %s (author=%s)",
            person_id, full_name, scopes, author_user_id,
        )
        return

    await upsert_author_person(driver, author_user_id)

    query = (
        "MATCH (author:Person {primary_user_id: $author_user_id}) "
        "WITH author, {token: $openai_key, model: 'text-embedding-3-small'} AS cfg "
        f"CREATE (p:Person{extra_labels} {{ "
        "    person_id: $person_id, "
        "    first_name: $first_name, "
        "    last_name: $last_name, "
        "    full_name: $full_name, "
        "    role: $role, "
        "    user_ids: $user_ids, "
        "    embedding: ai.text.embed($embed_text, 'OpenAI', cfg), "
        "    aliases: [], "
        "    is_auto_provisioned: false, "
        "    created_at: datetime({epochSeconds: $created_at_ts}), "
        "    updated_at: datetime({epochSeconds: $created_at_ts}) "
        "}) "
        "CREATE (author)-[:AUTHORED {via_bot: $bot_name, created_at: datetime(), migrated: true}]->(p) "
        "RETURN p.person_id AS person_id"
    )
    async with driver.session() as session:
        result = await session.run(
            query,
            author_user_id=author_user_id,
            openai_key=openai_key(),
            person_id=person_id,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            role=role,
            user_ids=user_ids_json,
            embed_text=embed_text,
            created_at_ts=created_at_ts,
            bot_name=bot_name(),
        )
        row = await result.single()
        if not row:
            logger.warning("Person %s did not return a person_id", person_id)


# ---------------------------------------------------------------------------
# Interactive people classification
# ---------------------------------------------------------------------------


def classify_person_interactive(record: dict) -> list[str] | None:
    """Prompt the operator for a person's scope(s). Returns scope list or None to skip."""
    person_id = record.get("_id") or record.get("id")
    full_name = f"{record.get('first_name', '')} {record.get('last_name', '')}".strip()
    role = record.get("role", "")
    ids_blob = record.get("user_ids", "")
    print()
    print(f"  person_id:     {person_id}")
    print(f"  name:          {full_name or '(blank)'}")
    print(f"  role:          {role or '(blank)'}")
    print(f"  user_ids:      {ids_blob}")
    while True:
        ans = input("  Scope? [P]ersonal / [R] professional / [B]oth / [S]kip: ").strip().lower()
        if ans in ("p", "personal"):
            return ["personal"]
        if ans in ("r", "professional", "prof"):
            return ["professional"]
        if ans in ("b", "both"):
            return ["personal", "professional"]
        if ans in ("s", "skip"):
            return None
        print("  Please enter P, R, B, or S.")


# ---------------------------------------------------------------------------
# Relationship migration
# ---------------------------------------------------------------------------


async def migrate_memory_relationships(driver, record: dict) -> tuple[int, int]:
    """Create :INVOLVES and :RELATED_TO edges for one memory record.

    Returns (involves_created, related_to_created).
    """
    record_id = record.get("_id") or record.get("id")

    # related_people is a list of person_ids (string list)
    related_people = record.get("related_people") or []
    if isinstance(related_people, str):
        try:
            related_people = json.loads(related_people)
        except (json.JSONDecodeError, ValueError):
            related_people = []

    # related_memories was stored as a JSON string
    related_memories = record.get("related_memories") or []
    if isinstance(related_memories, str):
        try:
            related_memories = json.loads(related_memories)
        except (json.JSONDecodeError, ValueError):
            related_memories = []

    involves = 0
    related = 0
    author_user_id = resolve_author_user_id(record.get("user_id"))

    if related_people:
        query = (
            "MATCH (m:Memory {record_id: $record_id}) "
            "UNWIND $people AS pid "
            "MATCH (p:Person {person_id: pid}) "
            "MERGE (m)-[r:INVOLVES]->(p) "
            "ON CREATE SET r.created_at = datetime(), "
            "              r.link_author_user_id = $caller, "
            "              r.migrated = true "
            "RETURN count(r) AS n"
        )
        async with driver.session() as session:
            result = await session.run(
                query, record_id=record_id, people=related_people, caller=author_user_id
            )
            row = await result.single()
            involves = (row["n"] if row else 0) or 0

    if related_memories:
        query = (
            "MATCH (m:Memory {record_id: $record_id}) "
            "UNWIND $others AS oid "
            "MATCH (o:Memory {record_id: oid}) "
            "MERGE (m)-[r:RELATED_TO]->(o) "
            "ON CREATE SET r.created_at = datetime(), "
            "              r.link_author_user_id = $caller, "
            "              r.migrated = true "
            "RETURN count(r) AS n"
        )
        async with driver.session() as session:
            result = await session.run(
                query, record_id=record_id, others=related_memories, caller=author_user_id
            )
            row = await result.single()
            related = (row["n"] if row else 0) or 0

    return involves, related


async def migrate_person_relationships(driver, record: dict) -> int:
    """Create :Person->[:<TYPE>]->:Person edges from the 'relations' JSON field.

    Returns number of edges created. Relation type is sanitized to UPPER_SNAKE_CASE
    and inlined into the Cypher (no APOC).
    """
    person_id = record.get("_id") or record.get("id")
    raw_relations = record.get("relations") or "[]"
    if isinstance(raw_relations, str):
        try:
            relations = json.loads(raw_relations)
        except (json.JSONDecodeError, ValueError):
            return 0
    else:
        relations = raw_relations
    if not isinstance(relations, list):
        return 0

    author_user_id = resolve_author_user_id(record.get("user_id"))

    created = 0
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        target = rel.get("related_person_id")
        rel_type = rel.get("relation_type", "RELATED_TO")
        if not target:
            continue
        safe = "".join(
            c for c in rel_type.upper().replace(" ", "_") if c.isalnum() or c == "_"
        ) or "RELATED_TO"
        query = (
            "MATCH (a:Person {person_id: $from_id}), (b:Person {person_id: $to_id}) "
            f"MERGE (a)-[r:{safe}]->(b) "
            "ON CREATE SET r.created_at = datetime(), "
            "              r.link_author_user_id = $caller, "
            "              r.migrated = true "
            "RETURN count(r) AS n"
        )
        async with driver.session() as session:
            result = await session.run(
                query,
                from_id=person_id,
                to_id=target,
                caller=author_user_id,
            )
            row = await result.single()
            created += (row["n"] if row else 0) or 0

    return created


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


async def verify_counts(driver, expected_counts: dict[str, int]) -> dict:
    """Compare Neo4j label counts against Pinecone namespace counts."""
    label_queries = {
        "PersonalMemory": "MATCH (m:PersonalMemory) RETURN count(m) AS n",
        "ProfessionalMemory": "MATCH (m:ProfessionalMemory) RETURN count(m) AS n",
        "TechnicalMemory": "MATCH (m:TechnicalMemory) RETURN count(m) AS n",
        "PersonalPerson": "MATCH (p:PersonalPerson) WHERE p.is_auto_provisioned = false RETURN count(p) AS n",
        "ProfessionalPerson": "MATCH (p:ProfessionalPerson) WHERE p.is_auto_provisioned = false RETURN count(p) AS n",
    }
    counts = {}
    async with driver.session() as session:
        for label, q in label_queries.items():
            result = await session.run(q)
            row = await result.single()
            counts[label] = (row["n"] if row else 0) or 0
    return counts


async def sample_memory_texts(driver, namespace: str, n: int = 5) -> list[dict]:
    """Pull n random memory records per namespace for post-migration checksum."""
    label = _NAMESPACE_TO_MEMORY_LABEL[namespace]
    query = (
        f"MATCH (m:{label}) "
        "WITH m, rand() AS r "
        "ORDER BY r "
        "LIMIT $n "
        "RETURN m.record_id AS record_id, m.text AS text"
    )
    async with driver.session() as session:
        result = await session.run(query, n=n)
        return [dict(r) async for r in result]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main(args) -> None:
    assert_env()

    logger.info("Connecting to Neo4j...")
    driver = await connect_neo4j()
    try:
        await smoke_test_embedding(driver)

        if args.purge:
            if not args.yes:
                resp = input("--purge will DETACH DELETE every Neo4j node. Type 'yes' to proceed: ").strip().lower()
                if resp != "yes":
                    logger.info("Aborted by user.")
                    return
            await purge(driver)

        # Create schema.
        await ensure_schema(driver)

        # --- Step 1: read Pinecone ---
        logger.info("Reading Pinecone namespaces...")
        records: dict[str, list[dict]] = {}
        for ns in PINECONE_NAMESPACES:
            logger.info("  fetching '%s'...", ns)
            records[ns] = await pinecone_fetch_namespace(ns)
            logger.info("    %d records", len(records[ns]))

        # --- Step 2: interactive people classification ---
        classifications: dict[str, list[str] | None] = {}
        skipped_people: list[dict] = []
        if args.auto_scope:
            # Non-interactive mode (useful for dry-runs): assume 'professional' for everyone.
            for rec in records["people"]:
                classifications[rec.get("_id")] = ["professional"]
            logger.info("--auto-scope: defaulted %d people to ['professional']", len(records["people"]))
        else:
            print()
            print(f"Classifying {len(records['people'])} person record(s)...")
            for rec in records["people"]:
                choice = classify_person_interactive(rec)
                classifications[rec.get("_id")] = choice
                if choice is None:
                    skipped_people.append({"person_id": rec.get("_id"), "record": rec})

        # --- Step 3: migrate memories ---
        memory_migrated: dict[str, int] = {ns: 0 for ns in MEMORY_NAMESPACES}
        for ns in MEMORY_NAMESPACES:
            logger.info("Migrating %d memory records in '%s'...", len(records[ns]), ns)
            for rec in records[ns]:
                try:
                    await migrate_memory(driver, ns, rec, args.dry_run)
                    memory_migrated[ns] += 1
                except Exception as e:
                    logger.exception("Memory migration failed for %s: %s", rec.get("_id"), e)

        # --- Step 4: migrate people ---
        people_migrated = 0
        for rec in records["people"]:
            scopes = classifications.get(rec.get("_id"))
            if scopes is None:
                continue
            try:
                await migrate_person(driver, rec, scopes, args.dry_run)
                people_migrated += 1
            except Exception as e:
                logger.exception("Person migration failed for %s: %s", rec.get("_id"), e)

        # --- Step 5: migrate relationships ---
        involves_total = 0
        related_to_total = 0
        person_rel_total = 0
        if not args.dry_run:
            logger.info("Wiring memory relationships (INVOLVES + RELATED_TO)...")
            for ns in MEMORY_NAMESPACES:
                for rec in records[ns]:
                    involves, related = await migrate_memory_relationships(driver, rec)
                    involves_total += involves
                    related_to_total += related
            logger.info("Wiring person relationships...")
            for rec in records["people"]:
                if classifications.get(rec.get("_id")) is None:
                    continue
                person_rel_total += await migrate_person_relationships(driver, rec)

        # --- Step 6: verify + report ---
        expected = {
            "PersonalMemory": len(records["personal"]),
            "ProfessionalMemory": len(records["professional"]),
            "TechnicalMemory": len(records["technical"]),
            "PersonalPerson": sum(
                1 for rec in records["people"]
                if classifications.get(rec.get("_id")) and "personal" in classifications[rec.get("_id")]
            ),
            "ProfessionalPerson": sum(
                1 for rec in records["people"]
                if classifications.get(rec.get("_id")) and "professional" in classifications[rec.get("_id")]
            ),
        }
        observed = (
            await verify_counts(driver, expected) if not args.dry_run else dict.fromkeys(expected, 0)
        )

        samples: dict[str, list[dict]] = {}
        if not args.dry_run:
            for ns in MEMORY_NAMESPACES:
                samples[ns] = await sample_memory_texts(driver, ns, n=5)

        report = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "dry_run": args.dry_run,
            "source_counts_pinecone": {ns: len(records[ns]) for ns in PINECONE_NAMESPACES},
            "expected_neo4j_label_counts": expected,
            "observed_neo4j_label_counts": observed,
            "memories_migrated": memory_migrated,
            "people_migrated": people_migrated,
            "people_skipped": [
                {"person_id": s["person_id"], "full_name": f"{s['record'].get('first_name', '')} {s['record'].get('last_name', '')}".strip()}
                for s in skipped_people
            ],
            "edges_created": {
                "INVOLVES": involves_total,
                "RELATED_TO_memory": related_to_total,
                "person_relations": person_rel_total,
            },
            "samples": samples,
        }

        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info("Report written to %s", REPORT_PATH)

        # Exit code signals mismatch for CI/smoke.
        mismatches = [
            (k, expected[k], observed.get(k, 0))
            for k in expected
            if (not args.dry_run) and expected[k] != observed.get(k, 0)
        ]
        if mismatches:
            logger.error("COUNT MISMATCHES detected:")
            for label, exp, obs in mismatches:
                logger.error("  %s: expected=%d observed=%d", label, exp, obs)
            sys.exit(1)

        logger.info("Migration complete.")
    finally:
        await driver.close()


def parse_args():
    p = argparse.ArgumentParser(description="Migrate Pinecone knowledge base into Neo4j.")
    p.add_argument(
        "--purge",
        action="store_true",
        help="Drop all Neo4j nodes/rels/indexes/constraints before migrating.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the --purge confirmation prompt (for CI/automation).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Pinecone + classify people, but don't write to Neo4j.",
    )
    p.add_argument(
        "--auto-scope",
        action="store_true",
        help="Non-interactive: default all people to ['professional'] scope.",
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
