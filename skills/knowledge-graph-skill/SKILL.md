---
name: knowledge-graph-skill
description: "How to use the professional memory system — Pinecone for semantic search, with an automatically-mirrored Neo4j knowledge graph behind the scenes. Use this skill when storing or retrieving knowledge, people, products, or concepts, when deciding what to put in Pinecone vs scratchpad, and when the user asks to turn background entity auto-extraction on or off for a chat."
---

# Professional Memory

Long-term memory lives in **Pinecone** (semantic content search). A Neo4j knowledge graph is kept in sync automatically as a side-effect of every Pinecone write — you do **not** call Neo4j directly.

## Architecture (what happens when you write)

```
You → Pinecone tool → Pinecone record (source of truth)
                    → Neo4j entity + edges (auto-mirrored)
```

- `create_record` → Pinecone record + Neo4j entity + `INVOLVES`/`RELATED_TO` edges to related people/memories.
- `create_person` → Pinecone person + Neo4j person node + relationship edges parsed from the `relations` field.
- `update_record` → Pinecone metadata updated **and** the mirrored Neo4j node/edges are refreshed (old outbound edges are removed and re-created from the new state, so updates never produce duplicates).
- `delete_record` → Pinecone record deleted and the mirrored Neo4j entity removed.

You have no tools for editing Neo4j directly. That's intentional — the graph is a derived view of Pinecone.

## Pinecone Tools

| Tool | Purpose |
|------|---------|
| `search_knowledge` | Semantic search across namespaces |
| `get_records` | Fetch specific records by ID |
| `list_records` | List records (paginated) |
| `create_record` | Store a memory, idea, knowledge item, incident, etc. |
| `create_person` | Store a person profile with relations |
| `update_record` | Update a record (creator/admin only — graph mirror updates automatically) |
| `delete_record` | Delete a record (creator/admin only — graph mirror cleans up automatically) |

Namespaces: `personal`, `professional`, `people`, `technical`.

For categories, schema fields, and workflow examples, read `references/entity-relationship-guide.md`.

## Authorship Rules

Every memory record has an `author` field, which is critical for audit and update/delete permission checks:

| Scenario | Author Value |
|----------|-------------|
| User explicitly says "remember this" | Leave `author` empty — defaults to the user's ID |
| You decide to store something on your own | `author="agent"` |
| Background auto-extraction | `"agent:auto"` (set automatically by the extraction process) |

Only the creator (or an admin) can update or delete a record.

## Auto-Extraction (Background Entity Extraction)

A background process can analyze chat turns and extract entities/relationships into the knowledge graph silently, with `author="agent:auto"`. This is **off by default for every chat** — no session gets auto-extraction until it is explicitly enabled.

Control tools (only call these when the user explicitly asks):

- `enable_auto_extraction(session_id)` — turn extraction on for a session
- `disable_auto_extraction(session_id)` — turn it off
- `list_auto_extraction_sessions()` — show which sessions currently have it enabled

When the user says something like "enable auto-extraction here" or "turn on entity extraction for this chat," use the **current session's ID** (e.g. `sl_C01ABC`, `tg_-100123`). If you don't know the session ID, ask the user or check session state before calling.

## Important

- Pinecone is the source of truth. Neo4j is a derived mirror — never assume you can "correct" the graph by writing to it; correct the Pinecone record and the mirror updates itself.
- If the Neo4j mirror fails on any write, the Pinecone write still succeeds (best-effort mirroring). Report the error to the user if you see one.
- Don't duplicate data — store full text in Pinecone only. The graph stores only lightweight names/types/relationships.

## Live References

- [Pinecone Documentation](https://docs.pinecone.io/)
- [Neo4j Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/) (for reference — not directly callable)
