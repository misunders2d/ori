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

Relationships created by the memory tools:

- `(:Memory)-[:INVOLVES]->(:Person)` — from `related_people=[...]` on `create_record`.
- `(:Memory)-[:RELATED_TO]->(:Memory)` — from `related_memories=[...]`.
- `(:Person)-[:<RELATION_TYPE>]->(:Person)` — from the `relations=[...]` field on `create_person` (relation type sanitized to UPPER_SNAKE_CASE).
- `(:Person)-[:AUTHORED]->(:Memory | :Person)` — created on every write; drives update/delete gating.

You do not call Neo4j directly. The memory tools manage all of this; you just supply the right arguments.

## Workflow: Storing Connected Memories

When a user shares something that involves people or prior memories:

1. Call `create_record` (or `create_person`) with `related_people=[person_ids]` and/or `related_memories=[memory_ids]` populated. Graph edges are created in the same call.
2. If later details change, call `update_record` — it enforces creator-only via the `:AUTHORED` edge. Non-authors get `{"status": "forbidden"}`.

### Example

User says: *"Remember that Alice from SupplierCo switched us to DHL for returns."*

1. Find Alice's person record — try `search_people("Alice SupplierCo", scope="professional")`.
2. If no match, call `create_person("Alice", "", "supplier contact at SupplierCo", user_ids='[{"id_type":"email","id_value":"alice@supplierco.com"}]', scopes=["professional"])`.
3. Call `create_record("professional", text="Alice from SupplierCo switched returns shipping to DHL.", short_description="SupplierCo returns now via DHL", category="operational", tags=["shipping","supplierco","returns"], related_people=["per_<alice_id>"])`.

The `:INVOLVES` edge between the memory and Alice is created in the same call. The `:AUTHORED` edge back to the caller's `:Person` is also wired automatically.

## ACL Notes

- A user who is an admin always passes every read check — admins see all three memory namespaces and both person scopes.
- A company-domain user (email matches `COMPANY_DOMAIN`) sees `professional` and `technical` memories and `:Person:Professional` people.
- Everyone else (e.g. a Telegram-only identifier like `tg_330959414` with no email) can still write in any namespace but can only read `:Memory:Technical`.
- The creator of a record can always update or delete it. A non-creator trying to update gets a `forbidden` response. Admins can override via `update_any_record` / `update_any_person`.

## Admin-Only Tools

- `update_any_record` / `update_any_person` — force-update a record regardless of authorship. Use sparingly, only when the creator cannot update themselves.
- `promote_person` — add a scope label to an existing person (e.g., promoting a `:PersonalPerson` to also be `:ProfessionalPerson` when a friend joins the company).
