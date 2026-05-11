---
name: amazon-routing-skill
description: "How to route Amazon business requests to the right specialist agent. Use this skill when you need to decide which agent handles a request — product research, knowledge/memory, BigQuery analytics, Google Workspace, or data analysis. Also use when coordinating multi-agent workflows where data passes between agents via the scratchpad."
---

# Amazon Operations Routing

You coordinate specialist agents, each callable as a tool. Route the request to the right one, or coordinate multiple agents for complex tasks.

## Routing Decision Tree

| User wants... | Route to |
|--------------|----------|
| Product data, pricing, sales, competitors, BSR, Keepa, SP-API, H10 keywords | **AmazonAgent** |
| Search knowledge, "what do we know about X?" | **AmazonMemoryAgent** |
| Relationships, "how is X connected to Y?", entity graph | **AmazonMemoryAgent** |
| Remember a person, supplier, product | **AmazonMemoryAgent** |
| Sales/inventory data, business metrics (SQL) | **BigQueryAgent** |
| Google Drive, Sheets, Calendar | **AmazonWorkspaceAgent** |
| Charts, plots, CSV exports, statistical analysis, large file analysis | **AmazonDataAnalystAgent** |
| AI image generation / editing | **NOT your team** — handled by coordinator directly |

When the request is ambiguous (e.g., "what do you know about ASIN X?"), check memory first, then fetch fresh data if nothing found. Read `references/coordination-patterns.md` for detailed resolution rules and worked examples.

## Multi-Agent Tasks

For tasks spanning multiple agents, use the scratchpad as shared state — one agent writes, the next reads. Read `references/coordination-patterns.md` for the handoff pattern and worked examples like "chart the sales trend" or "export inventory to Sheets."

## Rules

- Simple tasks → route directly, don't over-coordinate.
- Always include relevant details from agent responses in your final answer.
- If an agent returns an error, report it. Never fabricate data.
- If unsure: AmazonMemoryAgent for knowledge queries, AmazonAgent for fresh product data.
- Never paste a message intended for another agent into the user reply. If you need another agent, call it.
- If a report-ready notification contains a CSV file path, route that existing path to AmazonDataAnalystAgent. Do not request the same report again.
- "Today's sales", "sales today", "what sold today", and other current-day Amazon seller sales questions MUST route to AmazonAgent/SP-API, not BigQuery, unless the user explicitly asks for BigQuery, SQL, warehouse data, or historical BI tables. **For ANY "today's sales / units / revenue" question, use the report path: `sp_request_report(report_type='GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL', days=1)` → poll `sp_check_report` → `sp_download_report` → parse the file.** Never use a live-orders listing endpoint for sales totals — that returns only the first page (~50 orders) which becomes a misleading partial slice. Reporting "50 orders today" when actual is 900+ is the failure mode this rule prevents.

- **"Today" / "yesterday" / any naked day reference defaults to Pacific Time** (`America/Los_Angeles`) for this user, unless they explicitly name a different timezone in the same message. When calling `get_current_time`, pass `timezone="America/Los_Angeles"`. Report dates in Pacific. Sales queries pull the Pacific-time day, not UTC. This rule is non-negotiable — the user should never have to repeat their timezone.

## Presentations (.pptx)

Any user request mentioning "deck", "slides", "presentation", "PowerPoint", or "PPTX" routes to **AmazonDataAnalystAgent**, which owns the `generate_presentation(title, slides, filename?, template_name?)` tool. Slide layouts: `title`, `bullets`, `chart`, `kpi_grid`, `table`, `two_column`, `image`, `text`. Brand template at `data/presentations/templates/default.pptx` is applied automatically when present; otherwise blank canvas is used. Output is a `.pptx` file path returned to the caller — the file_attachment_capture/inject callback pair handles delivery to Slack/Telegram/A2A. Do NOT upload to Google Drive unless the user explicitly asks. Read `skills/presentation-skill/SKILL.md` for the full vocabulary.

When the user supplies analysis data (CSV/TSV/scratchpad), the typical chain is: AmazonAgent or BigQueryAgent produces the data file → AmazonDataAnalystAgent analyses + builds chart PNGs + assembles the deck in one pass.

## Live References

- [Google ADK Multi-Agent Systems](https://google.github.io/adk-docs/agents/multi-agents/)
- [ADK AgentTool Pattern](https://google.github.io/adk-docs/tools/)
- [ADK Callbacks & Design Patterns](https://google.github.io/adk-docs/callbacks/design-patterns-and-best-practices/)
