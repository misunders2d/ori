---
name: knowledge-graph-skill
description: "Knowledge graph memory protocol — entity tracking, relationships, graph queries, and unified memory architecture."
---

# Knowledge Graph Memory

You have a dual-layer memory system: **Pinecone** for semantic content search, **Neo4j** for entity relationships and graph traversal. They work together — Pinecone stores the full text, Neo4j stores who/what connects to who/what.

## Memory Architecture

```
Pinecone (vector DB, cloud)          Neo4j (graph DB, cloud)
  - Full text content                  - Entity nodes (lightweight)
  - Semantic search                    - Relationship edges (temporal)
  - Source of truth                    - Cross-references via pinecone_id
  - Namespaces: personal,             - Entity types: person, company,
    professional, people, technical       project, product, concept, event
```

When you create a Pinecone record, a corresponding Neo4j entity and relationship edges are automatically created (dual-write). When you delete a Pinecone record, the Neo4j entity is cleaned up too.

## When to Use Which

| Question Type | Use | Tool |
|--------------|-----|------|
| "What do I know about X?" | Pinecone | `search_knowledge` |
| "How is X related to Y?" | Neo4j | `find_connection_path` |
| "Who/what is connected to X?" | Neo4j | `query_connections` |
| "When did X's relationships change?" | Neo4j | `entity_timeline` |
| "Find records about topic Z" | Pinecone | `search_knowledge` |
| "Show me the network around Alice" | Neo4j | `query_connections` |
| "What's the full text of record mem_123?" | Pinecone | `get_records` |

## Authorship Rules

Every memory record has an `author` field. This is critical for audit:

| Scenario | Author Value |
|----------|-------------|
| User explicitly says "remember this" | User's ID (e.g. `valerii@mellanni.com`) |
| You decide to store something on your own | `"agent"` |
| Background auto-extraction | `"agent:auto"` |

Always set `author` correctly when calling `create_record` or `create_person`:
- If the user asked you to remember it: leave `author` empty (defaults to their ID)
- If you're storing something proactively: set `author="agent"`

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

## Entity Types

Use these standard types when creating entities:
- `person` — individuals (colleagues, contacts, managers)
- `company` — organizations, suppliers, partners
- `project` — initiatives, campaigns, plans
- `product` — physical products, ASINs, SKUs
- `concept` — ideas, strategies, policies, procedures
- `event` — incidents, meetings, milestones

## Relationship Types

Use UPPER_SNAKE_CASE for relationship labels. Common patterns:
- `WORKS_WITH`, `MANAGES`, `REPORTS_TO` — people relationships
- `SUPPLIES`, `PARTNERS_WITH`, `COMPETES_WITH` — company relationships
- `INVOLVED_IN`, `LEADS`, `CONTRIBUTES_TO` — person-to-project
- `RELATED_TO` — general association
- `CAUSED`, `RESOLVED`, `BLOCKED_BY` — event/incident chains
- `USES`, `CONTAINS`, `DEPENDS_ON` — structural relationships

## Workflow: Creating Connected Memories

When storing something that involves relationships:

1. Create the Pinecone record first (via `create_record` or `create_person`) — this auto-creates the Neo4j entity
2. If additional relationships exist that weren't captured by `related_people`/`related_memories`, use `link_entities` to add them
3. Use `query_connections` to verify the graph looks right

Example — user says "Remember that Alice from SupplierCo switched us to DHL for returns":
1. `create_record(namespace="professional", text="Alice from SupplierCo switched returns shipping to DHL", related_people=["per_alice_id"], ...)` — auto-creates Neo4j entity
2. `link_entities(from_entity="SupplierCo", to_entity="DHL", relation_type="USES", properties='{"context": "returns shipping"}')` — adds the business relationship

## Importing Existing Records

Pinecone records created before the graph was enabled have no Neo4j nodes. Use `import_pinecone_record` to backfill:

```
import_pinecone_record(record_id="mem_2025_04_07_abc123", namespace="professional")
```

This fetches the Pinecone metadata and creates the corresponding entity + relationship edges in Neo4j. You can do this one at a time or as a bulk operation via a plan.

## Background Auto-Extraction

After every conversation turn, a background process automatically extracts named entities and relationships from the exchange and writes them to Neo4j with `author="agent:auto"`. This happens silently — you don't need to do anything. The graph builds itself over time from natural conversations.

## Live References

- [Neo4j Cypher Query Language](https://neo4j.com/docs/cypher-manual/current/)
- [Pinecone Documentation](https://docs.pinecone.io/)
- [Neo4j Aura (managed cloud)](https://neo4j.com/cloud/platform/aura-graph-database/)

## Important

- Pinecone is the source of truth for content. Neo4j is best-effort for relationships.
- If a Neo4j write fails, the Pinecone write still succeeds — relationships can be added later.
- Don't duplicate data — store full text in Pinecone, store only names/types/relationships in Neo4j.
- Entity resolution: `link_entities` and `query_connections` accept either entity IDs or names. Names are resolved by searching the graph.
- Cross-reference: Every Neo4j entity created from a Pinecone record stores the `pinecone_id` for lookup.
