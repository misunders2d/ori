# Memory Schema & Workflow Reference

## Namespaces

Memories:

- `personal` — private notes, preferences, personal context. Read-gated to admins.
- `professional` — business knowledge, incidents, ideas, projects, strategies. Read-gated to admins + company-domain users.
- `technical` — engineering notes, debug logs, platform specifics, bot-to-bot knowledge sharing. Open to everyone.

People:

- `:Person:Personal` — family, friends, private contacts. Admin-only read.
- `:Person:Professional` — colleagues, clients, vendors. Admin + company-domain read.
- A person can carry both labels (`create_person(scopes=["personal", "professional"])`) — dual-scope is supported.

## Memory Categories (for `create_record` and `update_record`)

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

## Node Model and Edges

Every memory node carries:
- `:Memory` (generic kind-label, used for full-text and cross-scope queries)
- One scope label: `:PersonalMemory`, `:ProfessionalMemory`, or `:TechnicalMemory` (drives the per-namespace vector index)

Every person node carries:
- `:Person` (generic kind-label)
- One or both scope labels: `:PersonalPerson`, `:ProfessionalPerson`

Every entity node carries:
- `:Entity` (single label — no per-type sublabels; `entity_type` is a canonicalized property used as a filter)
- Example: `(:Entity {entity_type: "brand", name: "Mellanni"})`, `(:Entity {entity_type: "department", name: "Amazon Department"})`

Relationships created by the tools:

- `(:Memory)-[:INVOLVES]->(:Person)` — from `related_people=[...]` on `create_record`.
- `(:Memory)-[:RELATED_TO]->(:Memory)` — from `related_memories=[...]`.
- `(:Memory)-[:ABOUT]->(:Entity)` — from `related_entities=[...]` on `create_record`.
- `(:Person)-[:<RELATION_TYPE>]->(:Person)` — from `relations=[...]` on `create_person`, or `relate_persons(from, to, relation_type)` after creation (relation type sanitized to UPPER_SNAKE_CASE).
- `(:Entity)-[:RELATED_TO]->(:Entity)` — from `related_entities=[...]` on `create_entity`.
- `(:Entity)-[:INVOLVES]->(:Person)` — from `related_people=[...]` on `create_entity`.
- `(:Entity)-[:<RELATION_TYPE>]->(:Entity)` — from `relate_entities(from, to, relation_type)` after creation.
- `(:Person)-[:<RELATION_TYPE>]->(:Entity)` — from `relate_person_to_entity(from_person_id, to_entity_id, relation_type)` after both nodes exist. Use when the person is the grammatical subject: `OWNS`, `WORKS_AT`, `MANAGES`, `USES`, `RUNS`, `LEADS`.
- `(:Entity)-[:<RELATION_TYPE>]->(:Person)` — from `relate_entity_to_person(from_entity_id, to_person_id, relation_type)` after both nodes exist. Use when the entity is the grammatical subject: `LED_BY`, `EMPLOYS`, `OWNED_BY`.
- `(:Memory)-[:<RELATION_TYPE>]->(:Person)` — typed form via `relate_memory_to_person` post-hoc or via `related_people=[{"person_id": ..., "relation_type": ...}]` at create time. Default is `INVOLVES`; typed examples: `RAISED_BY`, `DECIDED_BY`, `REPORTED_BY`, `ASSIGNED_TO`, `ATTENDED_BY`, `MENTIONED`.
- `(:Memory)-[:<RELATION_TYPE>]->(:Entity)` — typed form via `relate_memory_to_entity` post-hoc or via `related_entities=[{"entity_id": ..., "relation_type": ...}]` at create time. Default is `ABOUT`; typed examples: `AFFECTED`, `CONTRADICTS`, `IMPLEMENTS`.
- `(:Memory)-[:<RELATION_TYPE>]->(:Memory)` — typed form via `relate_memories` post-hoc or via `related_memories=[{"memory_id": ..., "relation_type": ...}]` at create time. Default is `RELATED_TO`; typed examples: `SUPERSEDES`, `FOLLOWS_UP`, `CORRECTS`, `REFERENCES`.

Authorship is not a graph edge — it's stored as `author_user_id`, `via_bot`, and `created_at` properties on each `:Memory` / `:Person` / `:Entity` node. The author's `:Person` node is resolvable by `primary_user_id` when you need name lookups.

You do not call Neo4j directly. The memory tools manage all of this; you just supply the right arguments.

## Workflow: Storing Connected Memories

When a user shares something that involves people or prior memories:

1. Call `create_record` (or `create_person`) with `related_people=[person_ids]` and/or `related_memories=[memory_ids]` populated. Graph edges are created in the same call.
2. If later details change, call `update_record` — it enforces creator-only via the `author_user_id` property. Non-authors get `{"status": "forbidden"}`.

### Example

User says: *"Remember that Alice from SupplierCo switched us to DHL for returns."*

1. Find Alice's person record — `search_people("Alice SupplierCo", scope="professional")`.
2. If no match, call `create_person("Alice", "", "supplier contact at SupplierCo", user_ids='[{"id_type":"email","id_value":"alice@supplierco.com"}]', scopes=["professional"])`.
3. Find SupplierCo and DHL as entities — `search_entities("SupplierCo", entity_type="company")` and `search_entities("DHL", entity_type="company")`. Create them via `create_entity(entity_type="company", name="SupplierCo", description="...")` if missing.
4. Call `create_record("professional", text="Alice from SupplierCo switched returns shipping to DHL.", short_description="SupplierCo returns now via DHL", category="operational", tags=["shipping","supplierco","returns"], related_people=["per_<alice_id>"], related_entities=["ent_<supplierco_id>", "ent_<dhl_id>"])`.

Now the graph has Alice linked to the memory via `:INVOLVES`, and both SupplierCo and DHL linked via `:ABOUT`. Later searches like "what do we know about SupplierCo?" traverse those edges instead of only returning semantic-vector-match memories.

### Workflow: Adding team structure (post-hoc relations)

User says: *"Ruslan, Bohdan, and Vitalii are my Amazon managers. I'm the head of the Amazon department at Mellanni."*

1. Find/create the people — `search_people` for each, `create_person` where missing.
2. Find/create the entities — `search_entities` for "Mellanni" (type: brand) and "Amazon Department" (type: department). Create via `create_entity` where missing.
3. Wire department-to-brand — `relate_entities(from=<amazon_dept_id>, to=<mellanni_id>, relation_type="part_of")`.
4. Wire each person's employment — `relate_persons(from=<ruslan_id>, to=<sergey_id>, relation_type="reports_to")`, and so on for Bohdan and Vitalii.
5. Optionally record the fact as a `:Memory` for later retrieval: `create_record(namespace="professional", text="...", category="knowledge", related_people=[...], related_entities=[<dept_id>, <mellanni_id>])`.

This is the correct shape for "organizational structure" — entities for the org/department, people for the humans, typed edges for the reporting/ownership, and a memory that notes when/how the structure was established. No more shoving the brand/department into `:Memory`.

## ACL Notes

- A user who is an admin always passes every read check — admins see all three memory namespaces and both person scopes.
- A company-domain user (email matches `COMPANY_DOMAIN`) sees `professional` and `technical` memories and `:Person:Professional` people.
- Everyone else (e.g. a Telegram-only identifier like `tg_330959414` with no email) can still write in any namespace but can only read `:Memory:Technical`.
- The creator of a record can always update or delete it. A non-creator trying to update gets a `forbidden` response. Admins can override via `update_any_record` / `update_any_person`.

## Admin-Only Tools

- `update_any_record` / `update_any_person` — force-update a record regardless of authorship. Use sparingly, only when the creator cannot update themselves.
- `promote_person` — add a scope label to an existing person (e.g., promoting a `:PersonalPerson` to also be `:ProfessionalPerson` when a friend joins the company).
