# Routing & Fallback

How requests find the right agent in Ori's tree, and what every agent does when a request lands in the wrong inbox.

> Source: every `app/sub_agents/*.py` `instruction=` block contains a "ROUTING FALLBACK" section. Coordinator owns top-level routing; mid-tier routers (currently only `AmazonHeadAgent`) own intra-domain routing.

---

## 1. Tree

```
CoordinatorAgent (root, fallback terminus)
├── DeveloperAgent           (self-evolution, model swaps, GitHub, integrations)
├── KnowledgeAgent           (A2A communication, DNA exchange, friend management)
├── AmazonHeadAgent          (Amazon domain router)
│   ├── AmazonAgent          (Keepa / SP-API / Helium10)
│   ├── AmazonMemoryAgent    (Neo4j knowledge graph)
│   ├── AmazonWorkspaceAgent (Google Drive / Sheets / Calendar)
│   ├── AmazonDataAnalystAgent (statistics, charts, .pptx decks)
│   └── BigQueryAgent        (SQL on the BI warehouse)
└── ClickUpAgent             (task management, conditionally enabled)
```

Coordinator is the **only** terminus. Every other agent must know how to escape its inbox when the request is outside its bounded scope.

## 2. Universal routing fallback

Every leaf agent's `instruction=` ends with a block of this shape:

```
ROUTING FALLBACK: If the user's request is outside <BOUNDED DOMAIN>,
call `transfer_to_agent(agent_name='<PARENT>')` so it can re-route.
Do not refuse, guess, or answer outside your domain.
```

**Bounce-to-parent**, not jump-to-root. Each leaf hops one level up; the parent decides whether to re-route to a sibling, escalate to the coordinator, or pass through. This preserves hierarchy and gives mid-tier routers (`AmazonHeadAgent`) the chance to apply their own routing intelligence.

| Agent | Parent (fallback target) |
|---|---|
| `AmazonAgent` | `AmazonHeadAgent` |
| `AmazonMemoryAgent` | `AmazonHeadAgent` |
| `AmazonWorkspaceAgent` | `AmazonHeadAgent` |
| `AmazonDataAnalystAgent` | `AmazonHeadAgent` |
| `BigQueryAgent` | `AmazonHeadAgent` |
| `AmazonHeadAgent` | `CoordinatorAgent` |
| `DeveloperAgent` | `CoordinatorAgent` |
| `KnowledgeAgent` | `CoordinatorAgent` |
| `ClickUpAgent` | `CoordinatorAgent` |
| `CoordinatorAgent` | (terminus — handles directly or politely refuses) |

## 3. Why bounce-to-parent and not jump-to-root

1. **Mid-tier intelligence stays useful.** `AmazonHeadAgent` knows that "build a chart of yesterday's sales" needs `AmazonAgent` (data) then `AmazonDataAnalystAgent` (chart) — it can chain. If `AmazonAgent` jumped straight to Coordinator, Coordinator would have to re-derive that routing.
2. **ADK transfer scope is implicit.** ADK 1.x allows `transfer_to_agent` to parent and to sub-agents by default. Sideways/cross-tree transfer works via name lookup, but bounce-to-parent is the simplest cross-version-safe pattern.
3. **Loop prevention.** Coordinator is the only terminus. Every fallback eventually arrives there. No mid-tier loops are possible because each agent only ever bounces UP.

## 3b. Amazon Ads constraint (mid-tier hard rule)

`AmazonHeadAgent` MUST route ad spend / ACOS / ROAS / campaign / ad-group /
target / sponsored-ads-performance / ads-account / ad-eligibility / ad-report
questions to **`AmazonAgent`** (Amazon Ads MCP). These MUST NOT go to
`BigQueryAgent` — BigQuery holds warehouse/BI tables, not live Amazon Ads.
Route to BigQuery only when the user explicitly names BigQuery / SQL /
warehouse / historical BI; never infer it from the words "account",
"spend", or "metrics". Mirrored in `skills/amazon-routing-skill/SKILL.md`
and the `AmazonHeadAgent` instruction ROUTING HINTS.

## 4. When adding a NEW agent

Per `docs/AI_EDITS.md` AGENT/DOC SYNC rule (TIER 3 in `app/sub_agents/developer_agent.py`):

1. Append a ROUTING FALLBACK block to the new agent's `instruction=`.
2. Decide the parent fallback target. Direct children of Coordinator fall back to `CoordinatorAgent`. Children of a mid-tier router fall back to that mid-tier router (currently only `AmazonHeadAgent`).
3. Update the parent's `description=` and `instruction=` to advertise the new child's capability (so the parent knows when to delegate TO the new agent).
4. Update `docs/ROUTING.md` (this file) — add a row to §2.
5. Update `skills/amazon-routing-skill/SKILL.md` if the new agent is under `AmazonHeadAgent`.

A new agent without a routing fallback is a black hole: any user message that lands there outside its domain gets a refusal instead of a useful redirect.

## 5. Coordinator-only behaviours

These remain on `CoordinatorAgent` and are NOT delegated:

- Web research (`google_search`, `web_fetch`, `youtube_summary`)
- Scheduling (`SchedulingToolset`, `ContractToolset`)
- Quick interaction-style memory (`MemoryToolset` / LanceDB)
- AI image generation (`CreativesToolset`)
- System operations (`SystemToolset` — `update_self`, `session_refresh`, etc)
- Access control (`whitelist_chat`, `blacklist_chat`)
- A2A meta-tools (`get_agent_identity`, `get_my_a2a_key`)
- Transport-direct messaging (`telegram_send_dm`, `slack_*`)

If a user request maps to any of these, Coordinator handles directly — it does NOT delegate. See `app/sub_agents/coordinator_agent.py:80-99` for the explicit "Handle directly" list.

## 6. Anti-patterns

- **Refusing instead of bouncing.** "Sorry, I only handle X" is a UX failure. The fallback exists so users never hit this.
- **Guessing answers outside domain.** A model with no domain knowledge invents plausible-but-wrong responses. The fallback is the explicit instruction NOT to do this.
- **Jump-to-root.** Bypassing the parent's routing intelligence costs one extra hop and breaks the hierarchy invariant.
- **Duplicate routing prose.** Each routing decision should live in exactly one place. Coordinator owns "Amazon → AmazonHead", AmazonHead's amazon-routing-skill owns "deck → DataAnalyst". Repeating either in the other location creates drift.
