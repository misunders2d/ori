# Memory Schema & Workflow Reference

## Pinecone Namespaces

- `personal` — private notes, preferences, personal context for a specific user
- `professional` — business knowledge, incidents, ideas, projects, strategies
- `people` — person profiles (colleagues, contacts, suppliers)
- `technical` — engineering notes, debug logs, platform/system specifics

## Memory Categories (for `create_record` and `update_record`)

Use `category` on non-person records to classify the entry:

- `idea` — opportunities, proposals, brainstorms
- `memory` — time-stamped events and decisions
- `knowledge` — reference material, rules, pro-tips, best practices
- `procedure` — step-by-step operational playbooks / SOPs
- `experiment` — tests and experimental runs with results
- `incident` — problems and complaints requiring follow-up
- `project` — initiatives, plans, or requests requiring tracking
- `technical` — engineering changes, debug notes, platform specifics
- `strategy` — high-level plans, positioning, promotional strategy
- `communication_style` — templates, tone and style examples
- `policy` — compliance and legal risks and guidance
- `operational` — short operational updates, inventory, event-day notes

## Graph Mirror — What Gets Created Automatically

When you create or update a Pinecone record, a matching Neo4j entity is created/updated behind the scenes, along with outbound edges to related entities.

- **Record nodes:** Category maps to a graph entity type — `project` → project, `incident` / `memory` / `experiment` / `operational` → event, everything else → concept.
- **Record edges:**
  - `INVOLVES` → every ID in `related_people`
  - `RELATED_TO` → every ID in `related_memories`
- **Person nodes:** Created from `create_person` with type `person`.
- **Person edges:** Created from the `relations` field (a list of `{related_person_id, relation_type}` objects); each entry becomes a typed edge.

You don't need to think about any of this day-to-day — just use the Pinecone tools correctly and the graph will reflect the current state.

## Workflow: Storing Connected Memories

When a user shares something that involves people or prior memories:

1. Call `create_record` (or `create_person`) with `related_people` and/or `related_memories` populated with the appropriate IDs.
2. The graph mirror is created in the same call — no extra step.
3. If later details change, call `update_record` with the new fields. The mirror is refreshed automatically and old outbound relationships are cleaned up first, so you never get duplicated or stale edges.

### Example

User says: *"Remember that Alice from SupplierCo switched us to DHL for returns."*

1. Make sure Alice has a person record — search with `search_knowledge` in the `people` namespace, or `create_person` if she's new.
2. Call `create_record` in the `professional` namespace with:
   - `text="Alice from SupplierCo switched returns shipping to DHL"`
   - `category="operational"` (or `memory` — a time-stamped decision)
   - `related_people=["per_<alice_id>"]`
3. That's it. The graph now has an event node linked to Alice via `INVOLVES`, created automatically.

## Auto-Extraction Controls

A background process can extract entities and relationships from conversation turns and write them to the graph with `author="agent:auto"`. This is **disabled by default** for every chat — no session receives auto-extraction until it is explicitly enabled.

Tools (call only when the user explicitly asks):

- `enable_auto_extraction(session_id)` — turn on for a session
- `disable_auto_extraction(session_id)` — turn off
- `list_auto_extraction_sessions()` — see which sessions currently have it enabled

The `session_id` looks like `sl_C01ABC` (Slack) or `tg_-100123` (Telegram). When the user says "turn it on here" or "enable for this chat," use the current session's ID. If it isn't clear from context, check session state or ask the user to confirm.
