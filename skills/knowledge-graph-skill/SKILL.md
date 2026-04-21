---
name: knowledge-graph-skill
description: "How to use the knowledge base — a single Neo4j store with native vector search for memories, people, and their relationships. Use this skill when storing or retrieving knowledge, people, products, concepts, or when deciding what to put in the knowledge base vs the scratchpad."
---

# Knowledge Base

Long-term memory lives in **Neo4j** alone. Every memory and person record is stored as a graph node with a 1536-dim OpenAI embedding attached for semantic search, plus relationship edges between related records. Pinecone is no longer in use — one store, one credential, one write path.

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

**Creation is allowed for any authenticated caller** in any namespace. **Updates and deletes are restricted to the record's creator** (or an admin). The gate is enforced in code — if a non-author tries to update, the tool returns `{"status": "forbidden"}`. Relay the message to the user unchanged; do NOT retry.

## Tools

### Memory tools

| Tool | Purpose |
|------|---------|
| `search_knowledge(search_query, namespace, top_k)` | Semantic vector search in one namespace |
| `get_records(record_ids, namespace)` | Fetch specific records by ID |
| `list_records(namespace)` | Enumerate records in a namespace |
| `create_record(namespace, text, short_description, category, tags, related_people?, related_memories?)` | Store a memory, idea, incident, etc. |
| `update_record(record_id, namespace, updates)` | Creator-only update |
| `delete_record(record_id, namespace)` | Creator-only delete |
| `update_any_record(record_id, namespace, updates)` | **Admin-only** override — bypasses the creator gate |

### People tools

| Tool | Purpose |
|------|---------|
| `create_person(first_name, last_name, role, user_ids, relations?, scopes?)` | Store a person. `scopes` defaults to `["professional"]`; pass `["personal", "professional"]` for dual-scope people. |
| `search_people(search_query, scope, top_k)` | Semantic search within one scope |
| `update_person(person_id, updates)` | Creator-only |
| `update_any_person(person_id, updates)` | **Admin-only** override |
| `delete_person(person_id)` | Creator-only delete |
| `delete_any_person(person_id)` | **Admin-only** override |
| `promote_person(person_id, add_scope)` | **Admin-only** — add a scope label to an existing person (e.g. a friend becomes a colleague) |
| `merge_persons(canonical_id, alias_id)` | **Admin-only** — merge a duplicate `:Person` record into a canonical one. Reassigns `:AUTHORED` + `:INVOLVES` edges and adds the alias's identifier to the canonical's aliases list. Use when you discover two records represent the same real human. |

Categories: `idea`, `memory`, `knowledge`, `procedure`, `experiment`, `incident`, `project`, `technical`, `strategy`, `communication_style`, `policy`, `operational`.

For categories with examples, schema fields, and workflow walkthroughs, read `references/entity-relationship-guide.md`.

## Authorship

Every write records who made it via a `:AUTHORED` graph edge from the caller's `:Person` node to the record. The first time a caller invokes any memory tool, their `:Person` node is auto-provisioned (scope decided by email-domain match). Authorship is therefore always concrete — there is no `author="agent"` placeholder. If a scheduled task or system job writes a memory, the edge points to the admin that owns the task.

## Relationships

- `related_people=["per_..."]` on `create_record` creates `(:Memory)-[:INVOLVES]->(:Person)` edges.
- `related_memories=["mem_..."]` creates `(:Memory)-[:RELATED_TO]->(:Memory)`.
- `relations=[{"related_person_id": "per_...", "relation_type": "colleague"}]` on `create_person` creates `(:Person)-[:COLLEAGUE]->(:Person)` (relation type sanitized to UPPER_SNAKE_CASE).

These are managed by the memory tools — you do not call Neo4j directly for relationships.

## Preventing duplicate `:Person` records

Duplicates happen when the same real human is stored twice under slightly different names or IDs (e.g. "Igor" and "Igor Poluyko", or "Telegram: 123" and "tg_123"). They cost you silently — search results become fragmented, and relationship queries miss connections.

**Before calling `create_person`, always search first.** Disambiguation flow:

1. Call `search_people(search_query="<proposed name + role/context>", scope=<intended scope>, top_k=5)`.
2. Eyeball the top result(s). If any look like plausibly the same person — same first name with different last name, same role, same company, same domain in `user_ids` — do **not** create. Instead:
   - **Ask the user to confirm**: "I already have a person named `<full_name>` (`<person_id>`) with role `<role>`. Is this the same person you're describing, or a different one?"
   - If same → use `update_person` to enrich the existing record (add missing fields, extra `user_ids`, or new relations), or `promote_person` if a scope needs adding. Do not create a duplicate.
   - If different → proceed with `create_person`.
3. If no near-matches → `create_person` safely.

Edge cases:
- **Partial name only**: "Igor" alone is ambiguous — if `search_people` returns "Igor Poluyko", ask the user before doing anything. Never guess.
- **Same name, clearly different people** (e.g. two colleagues both named Alex): proceed with `create_person`, ideally with distinguishing role/context in the text.
- **Duplicate discovered after the fact**: admins merge with `merge_persons(canonical_id, alias_id)`. Pick whichever node has richer content or the clearer primary identifier as canonical.

The caller's own `:Person` is auto-provisioned on first tool call and uses alias-aware lookup, so Telegram/Email/Slack identifiers for the same caller don't re-duplicate silently as long as admins have merged the legacy duplicates.

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
