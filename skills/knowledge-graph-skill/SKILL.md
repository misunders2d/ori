---
name: knowledge-graph-skill
description: "How to use the knowledge base — a single Neo4j store with native vector search for memories, people, and their relationships. Use this skill when storing or retrieving knowledge, people, products, concepts, or when deciding what to put in the knowledge base vs the scratchpad."
---

# Knowledge Base

Long-term memory lives in **Neo4j** alone. Every memory, person, and entity record is stored as a graph node with a 1536-dim OpenAI embedding attached for semantic search, plus relationship edges between related records.

Three node kinds, three distinct roles:

| Label | What it represents | Examples |
|-------|--------------------|----------|
| `:Memory` | **Observations** — things that happened, were claimed, or were decided | Meetings, incidents, ideas, procedures, best-practices |
| `:Person` | **Actors** — humans the system tracks | Team members, clients, vendors, the user themselves |
| `:Entity` | **Referable things** — stable items other records point at | Brands, companies, departments, products, projects, locations, tools, marketplaces |

When in doubt: if you'd say "we had a meeting about X" — X is probably an `:Entity`, and the meeting is a `:Memory` that links to it.

## Namespaces and access control

Memories live in three namespaces, each with its own read rule:

| Namespace | Who can read |
|-----------|--------------|
| `personal` | Admins only |
| `professional` | Admins and users whose identifier ends with `@<COMPANY_DOMAIN>` |
| `technical` | Everyone (shared bot-to-bot knowledge — cross-department gotchas, operational notes) |

People live in two scopes (each person can be tagged with one or both):

| Scope | Who can read |
|-------|--------------|
| `personal` | Admins only (family, friends, private contacts) |
| `professional` | Admins and `@<COMPANY_DOMAIN>` users (colleagues, clients, vendors) |

**Creation is allowed for any authenticated and identified caller** in any namespace. **Updates and deletes are restricted to the record's creator** (or an admin). The gate is enforced in code — if a non-author tries to update, the tool returns `{"status": "forbidden"}`. Relay the message to the user unchanged; do NOT retry.

**Identity requirement before writing (code-enforced):** before `create_record` or `create_person` runs, the code checks whether the caller's `:Person` has a real name on file. If the caller is still a bare auto-provisioned stub (just a platform ID like `tg_330959414` with no `first_name`), the write is refused with `{"status": "needs_identity"}`. This prevents anonymous memories from accumulating. There is **no bypass flag** — the caller must be named before any write succeeds.

Handling a `needs_identity` response:
1. Ask the user: "Before I save that, what's your first and last name?"
2. Parse the answer and call `update_person(person_id="<from the response>", updates='{"first_name": "...", "last_name": "..."}')`.
3. Retry the original write. It will now succeed.

## Tools

### Memory tools

| Tool | Purpose |
|------|---------|
| `search_knowledge(search_query, namespace, top_k)` | Semantic vector search in one namespace |
| `get_records(record_ids, namespace)` | Fetch specific records by ID |
| `list_records(namespace)` | Enumerate records in a namespace |
| `create_record(namespace, text, short_description, category, tags, related_people?, related_memories?, related_entities?, force_create?)` | Store a memory, idea, incident, etc. `related_entities=["ent_..."]` creates `(:Memory)-[:ABOUT]->(:Entity)` edges — use when the memory references a brand/company/department/product/tool. Returns `{"status": "possible_duplicate"}` if a semantically similar record already exists (cosine ≥ 0.92). Pass `force_create=true` only after the user confirms it's a different record. |
| `update_record(record_id, namespace, updates)` | Creator-only update |
| `delete_record(record_id, namespace)` | Creator-only delete |
| `update_any_record(record_id, namespace, updates)` | **Admin-only** override — bypasses the creator gate |

### People tools

| Tool | Purpose |
|------|---------|
| `create_person(first_name, last_name, role, user_ids, relations?, scopes?, force_create?)` | Store a person. `scopes` defaults to `["professional"]`; pass `["personal", "professional"]` for dual-scope people. Returns `{"status": "possible_duplicate"}` if a matching record exists — pass `force_create=true` only after user confirms it's a different person. |
| `search_people(search_query, scope, top_k)` | Semantic search within one scope |
| `update_person(person_id, updates)` | Creator-only |
| `update_any_person(person_id, updates)` | **Admin-only** override |
| `delete_person(person_id)` | Creator-only delete |
| `delete_any_person(person_id)` | **Admin-only** override |
| `promote_person(person_id, add_scope)` | **Admin-only** — add a scope label to an existing person (e.g. a friend becomes a colleague) |
| `merge_persons(canonical_id, alias_id)` | **Admin-only** — merge a duplicate `:Person` record into a canonical one. Reassigns `:INVOLVES` edges and rewrites `author_user_id` on every record the alias authored to the canonical's `primary_user_id`; also adds the alias's identifier to the canonical's aliases list. Use when you discover two records represent the same real human. |
| `relate_persons(from_person_id, to_person_id, relation_type)` | Add or reaffirm a typed edge `(:Person {from})-[:<REL>]->(:Person {to})` between two existing people. Idempotent (MERGE). ACL: admin or author of `from`. Use when a relationship is described after both people already exist; for brand-new people, prefer `create_person(relations=[...])`. |

### Entity tools

Use `:Entity` for *referable things*: brands, companies, departments, products, projects, tools, marketplaces, locations — anything other records talk *about*. Do NOT use `:Entity` for observations (those are `:Memory`) or humans (those are `:Person`).

| Tool | Purpose |
|------|---------|
| `create_entity(entity_type, name, description?, tags?, related_entities?, related_people?, force_create?)` | Create a referable entity. `entity_type` is canonicalized to snake_case (`Brand` → `brand`); prefer re-using types you've seen via `search_entities` over inventing new ones. Returns `{"status": "possible_duplicate"}` at cosine ≥ 0.92; pass `force_create=true` only after user confirmation. |
| `search_entities(search_query, entity_type?, top_k)` | Semantic search. `entity_type` filter is optional — omit to search across all types. |
| `update_entity(entity_id, updates)` | Creator-only (admin can also modify their own). Allowed fields: `name`, `entity_type`, `description`, `tags`. Embedding refreshes automatically if any of those three change. |
| `delete_entity(entity_id)` | Creator-only / admin. DETACH DELETEs the entity and all its edges. |
| `relate_entities(from_entity_id, to_entity_id, relation_type)` | Add or reaffirm a typed edge between two entities. Idempotent. ACL: admin or author of `from`. Typical relations: `part_of`, `owns`, `uses`, `located_in`, `supplies`. |

Categories (for `:Memory`): `idea`, `memory`, `knowledge`, `procedure`, `experiment`, `incident`, `project`, `technical`, `strategy`, `communication_style`, `policy`, `operational`.

Entity types are **free-form** but should reuse common forms. Expect to see: `brand`, `company`, `department`, `team`, `product`, `project`, `tool`, `marketplace`, `location`, `warehouse`, `channel`. Before inventing a new `entity_type`, run `search_entities` for existing entities of similar kind and prefer their type if one fits.

For categories with examples, schema fields, and workflow walkthroughs, read `references/entity-relationship-guide.md`.

## Authorship

Every write stores the caller's `primary_user_id` as `author_user_id` on the record, alongside `via_bot` and `created_at`. Authorship is metadata on the node (indexed), not a graph edge — the `:Person` node remains the canonical identity for the author and is resolvable by `primary_user_id` or name. The first time a caller invokes any memory tool, their `:Person` node is auto-provisioned (scope decided by email-domain match). Authorship is therefore always concrete — there is no `author="agent"` placeholder. If a scheduled task or system job writes a memory, `author_user_id` is the admin that owns the task.

## Relationships

At creation time:
- `related_people=["per_..."]` on `create_record` → `(:Memory)-[:INVOLVES]->(:Person)`.
- `related_memories=["mem_..."]` on `create_record` → `(:Memory)-[:RELATED_TO]->(:Memory)`.
- `related_entities=["ent_..."]` on `create_record` → `(:Memory)-[:ABOUT]->(:Entity)` — use this to say "this memory is about Mellanni / Amazon Dept / Helium 10 / etc.".
- `relations=[{"related_person_id": "per_...", "relation_type": "colleague"}]` on `create_person` → `(:Person)-[:COLLEAGUE]->(:Person)` (relation type sanitized to UPPER_SNAKE_CASE).
- `related_entities=["ent_..."]` on `create_entity` → `(:Entity)-[:RELATED_TO]->(:Entity)`.
- `related_people=["per_..."]` on `create_entity` → `(:Entity)-[:INVOLVES]->(:Person)`.

After the fact (nodes already exist):
- `relate_persons(from, to, relation_type)` — typed edge between two people (e.g. `manages`, `reports_to`).
- `relate_entities(from, to, relation_type)` — typed edge between two entities (e.g. `part_of`, `owns`).

These are all managed by the tools — you do not call Neo4j directly for relationships.

### Linking follow-up and related memories (IMPORTANT)

A knowledge graph with no relationships is just a list. Whenever you create a memory that **builds on, corrects, supersedes, or references prior knowledge**, link it — don't let related records drift apart.

Proactive workflow when saving a memory that seems to relate to something existing:

1. Before calling `create_record`, use `search_knowledge(search_query=<key terms from the new content>, namespace=<same>, top_k=3)` to find plausibly-related prior records.
2. Review the results. For each match that is genuinely related (a follow-up, an update about the same topic, a clarification, a reference), collect its `record_id`.
3. Pass the collected IDs as `related_memories=["mem_...", "mem_..."]` on `create_record`.

Common trigger phrases from the user that imply linkage:
- "update to [topic]" / "new info about [topic]" → link to the prior record on that topic
- "correction to [previous statement]" → link to what's being corrected
- "follow-up on [incident/decision]" → link to the origin record
- "related to [project/person]" → link appropriately (`related_memories` for memory-to-memory, `related_people` for memory-to-person)

Edge cases:
- If the only "related" matches are the dedup-gate kind (cosine ≥ 0.92), don't create a new linked record — `update_record` the existing one instead. You'd be solving the wrong problem by linking two duplicates.
- If `related_memories` references a `record_id` that doesn't exist, the link is silently skipped — harmless but wasteful. Verify via `search_knowledge` or `get_records` if unsure.
- Explicit user instructions override this heuristic: if they say "don't link to anything," respect it.

Retrieving related memories later: when you `get_records` or `search_knowledge` a memory, graph tools like `query_connections(entity_id)` or `find_connection_path(from_id, to_id)` from the graph toolset expose the full linkage web. Use them when asked "what do you know about X?" to surface the connected knowledge rather than just the direct hit.

## Preventing duplicate `:Memory` records

Before writing a new memory, the code runs a **semantic dedup check**: embed the proposed `text`, vector-search the target namespace for top 3 matches with cosine ≥ 0.92, and if any hit, return `{"status": "possible_duplicate", "matches": [...], "threshold": 0.92}`. The create is **not** performed.

Handling a `possible_duplicate` response on `create_record`:

1. Show the match(es) to the user — each item includes `record_id`, `short_description`, `text_preview` (first 200 chars), `created_at`, and `score`.
2. Ask: "I already have `<short_description>` from `<created_at>` (similarity `<score>`). Is this the same knowledge, or a different record?"
3. Based on the answer:
   - **Same knowledge** → call `update_record(record_id, namespace, updates='{"text": "...merged...", "short_description": "...", "tags": [...]}')` on the existing record. Don't create a duplicate.
   - **Different record** (user explicitly confirms) → retry `create_record` with `force_create=true`. Only after explicit user confirmation — don't pre-emptively set it.

**Fail-open on infrastructure hiccups.** If the dedup query itself errors (plugin outage, empty index edge case), the helper returns no matches and the create proceeds. The gate is best-effort; it never blocks a legitimate write because the check machinery is down.

**Tuning note.** 0.92 is a starting threshold. If you find it too aggressive (frequent false positives on legitimate follow-up records), or too permissive (actual duplicates slipping through), adjust `_MEMORY_DUPLICATE_THRESHOLD` in `app/tools/memory_tools.py`.

## Preventing duplicate `:Person` records

Duplicates happen when the same real human is stored twice under slightly different names or IDs (e.g. "Igor" and "Igor Poluyko", or "Telegram: 123" and "tg_123"). They cost you silently — search results become fragmented, and relationship queries miss connections.

**Code-level dedup gate (enforced on every `create_person` call):** before writing, the code searches existing `:Person` records for matches on `first_name` (case-insensitive) OR any overlapping `user_ids` value. If any match is found, the tool returns `{"status": "possible_duplicate", "matches": [...]}` with up to 5 candidates. The create is **not** performed.

Handling a `possible_duplicate` response:
1. Show the match(es) to the user with their `person_id`, `full_name`, `role`, and any relevant identifiers.
2. Ask: "I already have `<existing full_name>` (`<person_id>`). Is this the same person you're describing, or a different one?"
3. Based on the answer:
   - **Same person** → call `update_person` on the existing `person_id` to enrich missing fields, or `promote_person` if a scope needs adding. Do **not** create a duplicate.
   - **Different person** (user explicitly confirms) → retry `create_person` with `force_create=true`. Only use this flag after the user's explicit confirmation; do not pre-emptively set it to skip the check.

Good practice before calling `create_person` in the first place (proactive, before the code gate fires): use `search_people(search_query="<proposed name + role/context>", scope=..., top_k=5)` yourself. If you can already see a likely match, raise it with the user before attempting the create — saves a round-trip and gives the user more context.

Edge cases:
- **Partial name only** ("Igor"): the dedup gate will surface "Igor Poluyko" and whoever else has `first_name: "Igor"`. Ask the user before doing anything. Never guess.
- **Same name, clearly different people** (e.g. two colleagues both named Alex): after the user confirms, retry with `force_create=true`.
- **Duplicate discovered after the fact**: admins merge with `merge_persons(canonical_id, alias_id)`. Pick whichever node has richer content as canonical.

The caller's own `:Person` uses alias-aware lookup — Telegram/Email/Slack identifiers for the same human resolve to the same node once an admin has merged the legacy duplicates.

## Preventing duplicate `:Entity` records

Same pattern as memories: before writing, the code embeds `name + entity_type + description`, vector-searches the `entity_embedding` index, and if any result has cosine ≥ 0.92 returns `{"status": "possible_duplicate", "matches": [...]}` without creating.

Handling a `possible_duplicate` on `create_entity`:
1. Show the match(es) — each item includes `entity_id`, `name`, `entity_type`, `description_preview`, `score`.
2. Ask the user: "I already have `<name>` (type: `<entity_type>`, similarity `<score>`). Is this the same thing, or something different?"
3. Based on the answer:
   - **Same thing** → use `update_entity` to enrich (better description, new tags), or `relate_entities` to link the new concept to the existing one. Don't create a duplicate.
   - **Different thing** (user explicitly confirms) → retry `create_entity` with `force_create=true`.

Good practice before `create_entity`: run `search_entities(search_query="<proposed name + context>")` yourself to see what's already there. Prefer linking to existing entities over creating new ones — the graph gets stronger with each reused reference.

## When to use `:Memory` vs `:Entity` vs `:Person`

The most common mistake is shoving something into `:Memory` when it should be an `:Entity`. Decision test:

- **"Is this a thing other records will point at over time?"** → `:Entity`.
  - "Mellanni brand" — yes. "Amazon Department" — yes. "Helium 10 tool" — yes. "Lightning Deals feature" — yes.
- **"Is this an observation — something that happened, a decision, a fact I'm recording right now?"** → `:Memory`.
  - "We decided to switch to DHL last week" — observation. "How LD scheduling works" — procedure/knowledge. "Igor raised a concern about X" — incident/communication.
- **"Is this a human I'd want to reach for or reference by name?"** → `:Person`.
  - "Sergey (department head)" — person. "Alice at SupplierCo" — person.

When a user describes a new concept, ask yourself: "Will other memories or entities point at this over time?" If yes, it's an `:Entity`. If it's a standalone fact / event / procedure that might reference entities but won't be referenced itself, it's a `:Memory`.

Typical linking patterns:
- `(:Memory "Q2 review meeting about Mellanni")-[:ABOUT]->(:Entity {entity_type: "brand"})` — a meeting about a brand.
- `(:Entity {entity_type: "department"})-[:PART_OF]->(:Entity {entity_type: "brand"})` — department belongs to a brand.
- `(:Person)-[:WORKS_AT]->(:Entity {entity_type: "company"})` — employment.
- `(:Person)-[:MANAGES]->(:Person)` — reporting chain.

## Namespace selection heuristics

- The user's family, friends, private life → `personal`.
- Business contacts, work decisions, client notes, internal processes → `professional`.
- Engineering gotchas, deploy procedures, integration quirks, SP-API oddities, things another agent in another department would benefit from seeing → `technical`.

## Important

- Neo4j is the single source of truth. There is no dual-write or mirror any more.
- If a tool returns `{"status": "forbidden"}`, the gate blocked the caller. Report to the user verbatim; don't retry.
- If a tool returns `{"status": "error"}`, pass through the exact error text — never fabricate a success.
- Text stays in the knowledge base. Lightweight structural state (agent notes, short-lived context) goes in the scratchpad, not here.

## Live References

- [Neo4j Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/) (for reference — you do not call Cypher directly)
