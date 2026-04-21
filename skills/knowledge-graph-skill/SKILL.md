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
| `promote_person(person_id, add_scope)` | **Admin-only** — add a scope label to an existing person (e.g. a friend becomes a colleague) |

Categories: `idea`, `memory`, `knowledge`, `procedure`, `experiment`, `incident`, `project`, `technical`, `strategy`, `communication_style`, `policy`, `operational`.

For categories with examples, schema fields, and workflow walkthroughs, read `references/entity-relationship-guide.md`.

## Authorship

Every write records who made it via a `:AUTHORED` graph edge from the caller's `:Person` node to the record. The first time a caller invokes any memory tool, their `:Person` node is auto-provisioned (scope decided by email-domain match). Authorship is therefore always concrete — there is no `author="agent"` placeholder. If a scheduled task or system job writes a memory, the edge points to the admin that owns the task.

## Relationships

- `related_people=["per_..."]` on `create_record` creates `(:Memory)-[:INVOLVES]->(:Person)` edges.
- `related_memories=["mem_..."]` creates `(:Memory)-[:RELATED_TO]->(:Memory)`.
- `relations=[{"related_person_id": "per_...", "relation_type": "colleague"}]` on `create_person` creates `(:Person)-[:COLLEAGUE]->(:Person)` (relation type sanitized to UPPER_SNAKE_CASE).

These are managed by the memory tools — you do not call Neo4j directly for relationships.

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
