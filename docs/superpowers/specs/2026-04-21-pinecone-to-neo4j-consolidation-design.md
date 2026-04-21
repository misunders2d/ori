# Pinecone → Neo4j consolidation — amazon_manager

**Status:** design, awaiting user review before implementation planning.
**Date:** 2026-04-21.
**Target:** `/media/misunderstood/DATA/projects/amazon_manager/` (Python + Google ADK, production).

## Goal

Eliminate Pinecone from amazon_manager and consolidate all persistent memory + knowledge-graph storage into Neo4j alone, using Neo4j's native vector indexes for semantic search. Preserve the current four-namespace separation as tightened code-enforced access control, and replace the current dual-write pattern (Pinecone source of truth + Neo4j best-effort mirror) with a single write path.

## Non-goals

- Porting this change to ori or to amazon_manager2. amazon_manager's `app/tools/pinecone_tools.py` and `app/core/graph.py` are not part of the synced core between repos; changes here stay in this repo.
- Zero-downtime migration. Hard cutover is in-scope; dual-write transition is out-of-scope.
- Preserving the auto-extract (background entity extraction) feature. It is explicitly dropped — creation via the existing `remember_info` / `create_record` path remains the way to write memories.
- Rewriting the rest of the agent architecture (head agent, memory-agent tool routing, skills). Only the data-layer swap.
- New Neo4j features beyond what serves this consolidation (APOC graph algorithms, multi-DB setup, etc.).

## Locked architectural decisions

1. **Neo4j is the single source of truth** for all memory and person records. Pinecone is removed entirely — code, dependency, env vars, toolset.
2. **Node labels encode privacy-scope namespaces** (not just entity type). `:Memory:Personal`, `:Memory:Professional`, `:Memory:Technical` for memories. `:Person:Personal`, `:Person:Professional` for contacts. Dual-scope people supported via multi-label (`:Person:Personal:Professional`).
3. **Vector indexes per label.** One vector index per namespace label — index-level isolation complements the code-enforced ACL as defense in depth. A query targeting the `personal_memory_embedding` index cannot physically return `:Memory:Professional` nodes.
4. **Embedder: OpenAI `text-embedding-3-small`, 1536 dims, cosine.** Called inline via Cypher through Neo4j's GenAI plugin (`ai.text.embed(text, 'OpenAI', config)`). No Python embedding helper. Multilingual (Russian, Spanish, 100+ languages).
5. **Authorship as a graph edge.** `(:Person)-[:AUTHORED]->(record)` replaces the current `author_user_id` string property. Update/delete checks become Cypher MATCH queries on the edge.
6. **Auto-provisioned caller Person nodes.** First contact from a new caller lazily MERGEs a `:Person` node keyed on their platform `user_id`, with namespace label decided by `COMPANY_DOMAIN` email-domain match (existing env var — same one `clickup_agent.py` and `bigquery_agent.py` already use).
7. **Hard-cutover migration.** Stop bot → purge Neo4j → run one-shot migration script → deploy Pinecone-free code → restart bot.

## Node model

### Memory namespaces

| Label | Vector index | Read ACL | Create | Update/Delete |
|---|---|---|---|---|
| `:Memory:Personal` | `personal_memory_embedding` | admins only | any authenticated caller | creator (via `:AUTHORED`) or admin |
| `:Memory:Professional` | `professional_memory_embedding` | `is_admin` OR `user_id.endswith("@" + COMPANY_DOMAIN)` | any authenticated caller | creator or admin |
| `:Memory:Technical` | `technical_memory_embedding` | open | any authenticated caller | creator or admin |

All three vector indexes: `DIMENSIONS 1536`, `SIMILARITY_FUNCTION 'cosine'`.

`:Memory:Technical` is the shared bot-to-bot knowledge bucket — engineering gotchas, operational notes, cross-department playbooks.

### Person namespaces

| Label | Vector index | Read ACL | Create | Update/Delete |
|---|---|---|---|---|
| `:Person:Personal` | `personal_person_embedding` | admins only | any authenticated caller | creator or admin |
| `:Person:Professional` | `professional_person_embedding` | `is_admin` OR `user_id.endswith("@" + COMPANY_DOMAIN)` | any authenticated caller | creator or admin |

Dual-scope: a single `:Person` node may carry both `:Personal` and `:Professional` labels. It appears in both indexes; its read-visibility is the union of the two read ACLs.

### Full-text index

```cypher
CREATE FULLTEXT INDEX memory_fulltext FOR (m:Memory) ON EACH [m.text, m.short_description];
```

For free, fast keyword search from Neo4j Browser or `cypher-shell` — not exposed as an LLM-callable tool in v1.

## Properties

### `:Memory` nodes

- `record_id: str` — format `mem_YYYY_MM_DD_<hex8>`, unique constraint.
- `text: str` — source text, input to embedding.
- `embedding: list<float>` — 1536 floats, populated by `ai.text.embed` inline.
- `short_description: str`.
- `category: str` — one of the existing `MEMORY_CATEGORIES` values (idea, memory, knowledge, procedure, experiment, incident, project, technical, strategy, communication_style, policy, operational).
- `tags: list<str>`.
- `created_at: datetime`.
- `updated_at: datetime`.

### `:Person` nodes

- `person_id: str` — format `per_YYYY_MM_DD_<hex8>`, unique constraint.
- `first_name: str`, `last_name: str`, `full_name: str` (denormalized, source of embedding).
- `role: str`.
- `user_ids: list<map>` — shape preserved from current Pinecone: `[{id_type, id_value}, ...]`.
- `embedding: list<float>` — 1536 floats.
- `primary_user_id: str` — canonical lookup key for auto-provisioned caller Persons (Telegram ID, email, Slack ID). Unique constraint + index. Optional for curated Person records that represent contacts the bot has never met directly.
- `aliases: list<str>` — secondary identity strings, for future identity-merging. Not consulted for v1 lookup matching.
- `is_auto_provisioned: bool` — `true` if created lazily on first bot contact, `false` for explicit `create_person` calls. Admin cleanup surface.
- `created_at: datetime`, `updated_at: datetime`.

### `:AUTHORED` edges

```cypher
(caller:Person)-[:AUTHORED {via_bot: str, created_at: datetime}]->(record)
```

Created on every memory/person create. `via_bot` = value of `BOT_NAME` env at write time — cross-bot provenance for the multi-department deployment story.

`author_user_id` is NOT a property on memory or person nodes in the new model. Authorship lives exclusively on the edge. One source of truth, no desync risk.

### Other relationships (preserved)

- `(:Memory)-[:INVOLVES]->(:Person)` — memory references a person.
- `(:Memory)-[:RELATED_TO]->(:Memory)`.
- `(:Person)-[:RELATED_TO {relation_type}]->(:Person)`.

All three carry `created_at` + `link_author_user_id` edge property (author of the link, which may differ from the authors of the linked nodes).

## Access control enforcement

All ACL checks live in `app/tools/memory_tools.py`. No enforcement in Cypher, none in the LLM, none derived from tool descriptions. Every memory/person tool call:

1. `caller = tool_context.state['user_id']`.
2. `is_admin = caller in ADMIN_USER_IDS.split(',')`.
3. `is_company = bool(COMPANY_DOMAIN) and caller.lower().endswith('@' + COMPANY_DOMAIN.lower())`. Pattern identical to `clickup_agent.py:42-55` (single domain; empty env disables the gate). Extension to multiple domains is trivial if ever needed.
4. **Auto-provision caller's `:Person` node** if `primary_user_id = caller` does not already exist. Scope label = `:Person:Professional` if `is_company` else `:Person:Personal`. `is_auto_provisioned: true`.
5. **Read check (search / get / list):**
   - `personal` → require `is_admin`.
   - `professional` → require `is_admin or is_company`.
   - `technical` → no check.
6. **Create check:** any authenticated caller may create in any namespace.
7. **Update/delete check:** the tool runs a Cypher MATCH via the `:AUTHORED` edge. Zero rows → `{"status": "forbidden"}` unless `is_admin` (which uses a parallel `update_any_record` tool that skips the MATCH).

Rejections return `{"status": "forbidden", "message": "<reason>"}`. The LLM sees this and explains to the user. Rejections log to standard app logs at INFO level.

## Embedder

**Model:** OpenAI `text-embedding-3-small`, 1536 dimensions, cosine similarity.

**Config:** new env var `OPENAI_API_KEY`.

**Call pattern — create (inline in Cypher):**

```cypher
WITH {token: $openai_key, model: 'text-embedding-3-small'} AS cfg
MERGE (m:Memory:Professional {record_id: $record_id})
SET m.text = $text,
    m.embedding = ai.text.embed($text, 'OpenAI', cfg),
    m.short_description = $short_description,
    m.category = $category,
    m.tags = $tags,
    m.created_at = datetime(),
    m.updated_at = datetime()
WITH m
MATCH (caller:Person {primary_user_id: $caller_id})
MERGE (caller)-[r:AUTHORED]->(m)
ON CREATE SET r.via_bot = $bot_name, r.created_at = datetime()
RETURN m.record_id
```

**Call pattern — search (also inline):**

```cypher
WITH {token: $openai_key, model: 'text-embedding-3-small'} AS cfg,
     ai.text.embed($search_query, 'OpenAI', cfg) AS query_vec
CALL db.index.vector.queryNodes('professional_memory_embedding', $top_k, query_vec)
YIELD node, score
RETURN node.record_id AS record_id,
       node.short_description AS short_description,
       node.text AS text,
       node.category AS category,
       node.tags AS tags,
       score
```

**Cost sanity check:** OpenAI `text-embedding-3-small` is $0.02 per 1M tokens. Migrating the full Pinecone dataset (few hundred records, few hundred tokens each) runs under $0.01 one-time. Per-query/per-create runtime cost is roughly $10⁻⁶. Effectively free for this dataset.

## Tool surface

### File moves

- `app/tools/pinecone_tools.py` → `app/tools/memory_tools.py` (rename; full rewrite).
- `app/toolsets/pinecone.py` → deleted.
- `app/toolsets/graph.py` → updated (drop auto-extract tool registrations; keep graph-traversal).
- `app/callbacks/entity_extraction.py` → deleted.
- `app/core/extraction_config.py` → deleted.

### Tool signatures

Memory tools (`app/tools/memory_tools.py`):

- `search_knowledge(search_query, namespace, top_k, tool_context) -> dict` — signature unchanged; `namespace` now accepts only `{"personal", "professional", "technical"}`.
- `get_records(record_ids, namespace, tool_context) -> dict` — unchanged.
- `list_records(namespace, tool_context) -> dict` — unchanged.
- `create_record(namespace, text, short_description, category, tags, related_people, related_memories, author, tool_context) -> dict` — signature unchanged. Note `author` arg stays but is advisory; the actual authorship edge is written from the resolved caller `:Person`.
- `update_record(record_id, namespace, updates, tool_context) -> dict` — unchanged; ACL moves to Cypher `:AUTHORED` MATCH.
- `delete_record(record_id, namespace, tool_context) -> dict` — unchanged; ACL moves to Cypher `:AUTHORED` MATCH.

People tools (`app/tools/memory_tools.py`):

- `create_person(first_name, last_name, role, user_ids, relations, scopes, author, tool_context) -> dict` — **new** `scopes: list[str]` arg, defaults to `["professional"]`. Values in `{"personal", "professional"}`.
- `search_people(search_query, scope, top_k, tool_context) -> dict` — **new** tool replacing the removed `namespace="people"` path in `search_knowledge`. `scope` in `{"personal", "professional"}`. Admins invoke twice if they want both.
- `update_person(person_id, updates, tool_context) -> dict` — **new** (equivalent behavior to `update_record` but for `:Person`).
- `promote_person(person_id, add_scope, tool_context) -> dict` — **new**, admin-only; adds `:Personal` or `:Professional` label to an existing person.

Graph tools (`app/tools/graph_tools.py`):

- Preserved unchanged: `search_entities`, `get_connections`, `find_path`, `get_entity_history`, `add_relationship`, `remove_relationship`.
- Deleted: `enable_auto_extraction`, `disable_auto_extraction`, `list_auto_extraction_sessions`.

Admin-only additions:

- `update_any_record(record_id, namespace, updates, tool_context)` — mirrors `update_record` but skips the `:AUTHORED` MATCH.
- `update_any_person(person_id, updates, tool_context)` — same shape, admin-only override.

## Auto-extract removal

Delete:

- `app/callbacks/entity_extraction.py`.
- `app/core/extraction_config.py`.
- Functions `enable_auto_extraction`, `disable_auto_extraction`, `list_auto_extraction_sessions` in `app/tools/graph_tools.py`.
- Their registrations in `app/toolsets/graph.py`.
- Their imports in `app/sub_agents/amazon_memory_agent.py`.
- Corresponding test classes in `tests/test_graph.py`.
- Auto-extract sections of `skills/knowledge-graph-skill/SKILL.md` and `skills/knowledge-graph-skill/references/entity-relationship-guide.md`.

Wherever the callback is wired into the agent-response lifecycle, remove the `asyncio.create_task(extract_entities_background(...))` call.

## Migration

One-shot script: `scripts/migrate_pinecone_to_neo4j.py`.

### Pre-flight

1. Assert all env vars present: `PINECONE_API_KEY`, `PINECONE_INDEX_NAME`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `OPENAI_API_KEY`, `COMPANY_DOMAIN`, `ADMIN_USER_IDS`, `BOT_NAME`.
2. Require `--purge` flag for destructive-op confirmation.
3. Test `ai.text.embed('ping', 'OpenAI', {...})` round-trips one embedding. Fail fast on broken OpenAI key or plugin access.

### Step 1 — purge Neo4j

With `--purge`:

```cypher
MATCH (n) DETACH DELETE n;
```

Plus drop all existing indexes and constraints (enumerated and dropped one-by-one to avoid ordering issues).

### Step 2 — create schema

Idempotent with `CREATE ... IF NOT EXISTS`:

- Uniqueness constraints: `record_id` on `:Memory`, `person_id` on `:Person`, `primary_user_id` on `:Person`.
- Five vector indexes per the tables above.
- One full-text index on `:Memory`.

### Step 3 — migrate memories

For each Pinecone namespace in `{personal, professional, technical}`:

1. Fetch all records via `list_paginated` + `fetch`.
2. For each record: run the create-memory Cypher pattern, mapping `namespace → :Memory:Personal / :Memory:Professional / :Memory:Technical`.
3. Preserve original `record_id`, `text`, `short_description`, `category`, `tags`, `created_at`, `updated_at`.
4. Map the stored `user_id` string → auto-provision a `:Person` node (`primary_user_id = user_id`, `is_auto_provisioned = true`, scope label from email-domain match). Create `:AUTHORED` edge.

### Step 4 — migrate people (interactive)

For each record in Pinecone `people` namespace:

1. Print summary: `first_name`, `last_name`, `role`, `user_ids`, `short_description`.
2. Prompt: `Scope? [P]ersonal / [R] professional / [B]oth / [S]kip: `.
3. Create `:Person` node with preserved `person_id`, name, role, `user_ids`. Apply chosen label(s). `is_auto_provisioned = false`.
4. Map stored `author` → `:Person` via `primary_user_id` lookup + create `:AUTHORED` edge.
5. `S` (skip) records are logged to the report but not migrated.

### Step 5 — migrate relationships

After all nodes exist:

1. Memory's `related_people` → `:INVOLVES` edges.
2. Memory's `related_memories` → `:RELATED_TO` edges.
3. Person's `relations` → `:RELATED_TO {relation_type}` edges.

### Step 6 — verify + report

- Count nodes per label; compare to Pinecone namespace counts. Expected: memory counts match; person count may differ by the number of `S`-skipped.
- Sample five random memory records per namespace: assert `text` matches the Pinecone source byte-for-byte.
- Run one semantic search per namespace and confirm at least one result returns.
- Write JSON report to `data/migration_report.json`: counts, skipped records, sample checksums, timestamps.

### Step 7 — cutover

1. Operator stops bot (`systemctl stop amazon-manager` or equivalent).
2. Run migration script.
3. Review report.
4. Deploy new code (prepared on a feature branch, merged to master before this step).
5. Start bot.

Migration is idempotent (MERGE-based Cypher, re-runnable). Re-running after a partial failure is safe.

## Environment

### New
- `OPENAI_API_KEY` — for inline `ai.text.embed`.
- `COMPANY_DOMAIN` — **reused** (already present in `ALLOWED_CONFIG_KEYS`; used by `clickup_agent.py` and `bigquery_agent.py`). Single domain (e.g. `mellanni.com`). A caller is a "company user" when their `user_id` ends with `@<COMPANY_DOMAIN>` (case-insensitive).

### Removed
- `PINECONE_API_KEY`, `PINECONE_INDEX_NAME`.

### Unchanged
- `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `ADMIN_USER_IDS`, `BOT_NAME`.

## Testing

New: `tests/test_memory_tools.py`.

Coverage:

- **ACL matrix.** Parametrized across (caller: admin / company-user / outsider) × (namespace: personal / professional / technical) × (op: read / create / update-own / update-other). ~36 rows.
- **Auto-provisioning.** First call from a fresh `user_id` creates `:Person:Professional` or `:Person:Personal` based on domain.
- **Multi-label person.** `create_person(scopes=["personal", "professional"])` produces a node in both indexes.
- **`:AUTHORED` update gate.** Non-author update → forbidden; admin → allowed; author → allowed.
- **Full-text search round-trip.** Create record → `db.index.fulltext.queryNodes` returns it.
- **Embedding round-trip.** Create record → `embedding` property is a list of 1536 floats.
- **Relationship preservation.** `related_people` → `:INVOLVES` edges created correctly on create + updated correctly on update.

Existing `tests/test_graph.py`:
- Keep tests for `upsert_entity`, `search_entities`, `get_connections`, `find_path`, relationships — they operate on the underlying Neo4j driver layer and remain valid.
- Delete test classes covering `extract_entities_background` and extraction-config.

Migration-script smoke test: `tests/test_migration_smoke.py` runs the script against an ephemeral Neo4j + mocked Pinecone with three fixture records per namespace, asserts the post-migration report matches expected counts.

## Acceptance criteria

Consolidation is complete when:

1. `pinecone` package is removed from `pyproject.toml`; `uv sync` succeeds; no remaining Pinecone imports anywhere.
2. `app/tools/memory_tools.py` exists; `app/tools/pinecone_tools.py`, `app/toolsets/pinecone.py`, `app/callbacks/entity_extraction.py`, `app/core/extraction_config.py` no longer exist.
3. All five vector indexes + one full-text index + three uniqueness constraints exist in Neo4j on bot startup (idempotent creation on first connect).
4. Full ACL matrix in `tests/test_memory_tools.py` passes green.
5. Migration smoke test passes; a dry-run against the real staging Pinecone yields expected per-namespace counts with zero unmatched records.
6. Live bot (post-cutover) can: create a memory, semantically search it, update it as creator, fail to update it as non-author, delete it; create a person with dual scope; search people in each scope independently.
7. `skills/knowledge-graph-skill/SKILL.md` and its references reflect the removed auto-extract tools and the new scoped search tools.

## Open items parked for implementation planning

- **Vector-index creation syntax** — Neo4j's `CREATE VECTOR INDEX` / `SEARCH`-clause syntax varies by version. Confirm against the running Aura target during sprint 0.
- **Scheduled-task caller identity.** `tool_context.state['user_id']` is populated for scheduled tasks via the `actual_caller_id` plumbing recently synced to ori; verify it threads correctly into auto-provisioning for scheduled runs. Non-blocking — the sync work laid the groundwork.
- **`BOT_NAME` fallback.** If the env var isn't reliably set on every deployment path, fall back to `socket.gethostname()` for `via_bot`. Trivial fix if needed.
