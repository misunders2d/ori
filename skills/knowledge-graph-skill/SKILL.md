---
name: knowledge-graph-skill
description: "How to use the dual-layer memory system — Pinecone for semantic search, Neo4j for entity relationships. Use this skill when storing or retrieving professional knowledge, tracking people/companies/products and their relationships, exploring connection paths, or managing auto-extraction settings. Also use when deciding whether to store something in Pinecone vs Neo4j vs scratchpad."
---

# Knowledge Graph Memory

You have a dual-layer memory system: **Pinecone** for semantic content search, **Neo4j** for entity relationships and graph traversal. They work together — Pinecone stores the full text, Neo4j stores who/what connects to who/what.

## When to Use Which

| Question Type | Use | Tool |
|--------------|-----|------|
| "What do I know about X?" | Pinecone | `search_knowledge` |
| "How is X related to Y?" | Neo4j | `find_connection_path` |
| "Who/what is connected to X?" | Neo4j | `query_connections` |
| "When did relationships change?" | Neo4j | `entity_timeline` |
| "Find entities named X" | Neo4j | `search_graph` |
| "Get full text of record X" | Pinecone | `get_records` |

For entity types, relationship types, creation workflow, and auto-extraction details, read `references/entity-relationship-guide.md`.

## Authorship Rules

Every memory record has an `author` field — critical for audit:

| Scenario | Author Value |
|----------|-------------|
| User explicitly asks to remember | User's ID (leave `author` empty — defaults to their ID) |
| You store something proactively | `"agent"` |
| Background auto-extraction | `"agent:auto"` |

## Architecture

```
Pinecone (vector DB, cloud)          Neo4j (graph DB, cloud)
  - Full text content                  - Entity nodes (lightweight)
  - Semantic search                    - Relationship edges (temporal)
  - Source of truth                    - Cross-references via pinecone_id
  - Namespaces: personal,             - Entity types: person, company,
    professional, people, technical       project, product, concept, event
```

When you create a Pinecone record, a corresponding Neo4j entity and relationship edges are automatically created (dual-write). When you delete a Pinecone record, the Neo4j entity is cleaned up too.

## Graph Tools Reference

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `add_entity` | Create/update an entity node | When you learn about a new person, company, project, etc. |
| `link_entities` | Create a relationship between entities | When you learn how two entities are connected |
| `query_connections` | Find all connected entities (1-4 hops) | "Who/what is connected to X?" |
| `find_connection_path` | Find shortest path between two entities | "How is X connected to Y?" |
| `entity_timeline` | Get relationship history with timestamps | "When did X start working with Y?" |
| `search_graph` | Search entities by name | "Find entities named Alice" |
| `import_pinecone_record` | Import an existing Pinecone record to graph | Backfill older records into the graph |

For entity types, relationship types, workflow examples, and auto-extraction details, read `references/entity-relationship-guide.md`.

## Important

- Pinecone is the source of truth for content. Neo4j is best-effort for relationships.
- If a Neo4j write fails, the Pinecone write still succeeds — relationships can be added later.
- Don't duplicate data — store full text in Pinecone, store only names/types/relationships in Neo4j.
- Entity resolution: `link_entities` and `query_connections` accept either entity IDs or names. Names are resolved by searching the graph.
- Cross-reference: Every Neo4j entity created from a Pinecone record stores the `pinecone_id` for lookup.

## Live References

- [Neo4j Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/)
- [Pinecone Documentation](https://docs.pinecone.io/)
- [Neo4j Aura (managed cloud)](https://neo4j.com/cloud/platform/aura-graph-database/)
