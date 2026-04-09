# Entity & Relationship Types

## Entity Types

Use these standard types when creating entities with `add_entity`:
- `person` — individuals (colleagues, contacts, managers)
- `company` — organizations, suppliers, partners
- `project` — initiatives, campaigns, plans
- `product` — physical products, ASINs, SKUs
- `concept` — ideas, strategies, policies, procedures
- `event` — incidents, meetings, milestones

## Relationship Types

Use UPPER_SNAKE_CASE for relationship labels with `link_entities`. Common patterns:
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

### Example

User says "Remember that Alice from SupplierCo switched us to DHL for returns":

1. `create_record(namespace="professional", text="Alice from SupplierCo switched returns shipping to DHL", related_people=["per_alice_id"], ...)`
2. `link_entities(from_entity="SupplierCo", to_entity="DHL", relation_type="USES", properties='{"context": "returns shipping"}')`

## Importing Existing Records

Pinecone records created before the graph was enabled have no Neo4j nodes. Use `import_pinecone_record` to backfill:

```
import_pinecone_record(record_id="mem_2025_04_07_abc123", namespace="professional")
```

This fetches the Pinecone metadata and creates the corresponding entity + relationship edges in Neo4j. You can do this one at a time or as a bulk operation via a plan.

## Background Auto-Extraction

A background process automatically extracts named entities and relationships from conversation turns and writes them to Neo4j with `author="agent:auto"`. This happens silently. The graph builds itself over time from natural conversations.

Control auto-extraction per session:
- `enable_auto_extraction(session_id)` — turn on for a session
- `disable_auto_extraction(session_id)` — turn off
- `list_auto_extraction_sessions()` — see which sessions have it enabled
