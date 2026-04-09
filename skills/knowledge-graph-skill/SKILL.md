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
Pinecone (vector DB)              Neo4j (graph DB)
- Full text content               - Entity nodes (lightweight)
- Semantic search                 - Relationship edges (temporal)
- Source of truth                 - Cross-references via pinecone_id
```

When you create a Pinecone record, a corresponding Neo4j entity and relationship edges are auto-created (dual-write). Deletion cleans up both sides.

## Important

- Pinecone is source of truth for content. Neo4j is best-effort for relationships.
- Don't duplicate — full text in Pinecone, only names/types/relationships in Neo4j.
- Entity resolution: `link_entities` and `query_connections` accept either IDs or names.

## Live References

- [Neo4j Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/)
- [Pinecone Documentation](https://docs.pinecone.io/)
- [Neo4j Aura (managed cloud)](https://neo4j.com/cloud/platform/aura-graph-database/)
