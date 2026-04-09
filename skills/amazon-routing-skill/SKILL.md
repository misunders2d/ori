---
name: amazon-routing-skill
description: "Routing guide for AmazonHeadAgent — maps user requests to the right specialist agent, with multi-agent coordination patterns."
---

# Amazon Operations Routing

You coordinate a team of specialist agents. Each is called as a tool. This skill helps you route requests and coordinate multi-agent workflows.

## Routing Decision Tree

| User wants... | Route to | Tool(s) used |
|--------------|----------|-------------|
| Product data, pricing, sales, competitors, BSR | **AmazonAgent** | Keepa, SP-API, Helium10 |
| "What do we know about X?" / search knowledge | **AmazonMemoryAgent** | `search_knowledge` (Pinecone) |
| "How is X connected to Y?" / relationships | **AmazonMemoryAgent** | `find_connection_path`, `query_connections` (Neo4j) |
| Remember a person, supplier, product, entity | **AmazonMemoryAgent** | `create_record`, `create_person`, `add_entity` |
| Entity timeline, graph exploration | **AmazonMemoryAgent** | `entity_timeline`, `search_graph` |
| Auto-extraction settings | **AmazonMemoryAgent** | `enable/disable_auto_extraction` |
| Sales data, inventory, business metrics (SQL) | **BigQueryAgent** | BigQuery tools |
| Google Drive files, download, search | **AmazonWorkspaceAgent** | Drive tools |
| Read/write Google Sheets | **AmazonWorkspaceAgent** | Sheets tools |
| Calendar events, scheduling meetings | **AmazonWorkspaceAgent** | Calendar tools |
| Charts, plots, data visualization | **AmazonDataAnalystAgent** | `generate_chart` |
| CSV/Excel file exports | **AmazonDataAnalystAgent** | `generate_file` |
| AI image generation / editing | **AmazonDataAnalystAgent** | `generate_image` |
| Statistical analysis of data | **AmazonDataAnalystAgent** | `analyze_data` |

## Ambiguous Requests

| Request | Resolution |
|---------|-----------|
| "What do you know about ASIN B0123?" | **AmazonMemoryAgent** first (check existing knowledge). If nothing found, **AmazonAgent** (fetch fresh from Keepa). |
| "Tell me about supplier X" | **AmazonMemoryAgent** (search_knowledge + query_connections). Only fetch fresh data if memory is empty. |
| "Compare these products" | **AmazonAgent** (fetch data for each), then **AmazonDataAnalystAgent** (chart the comparison). |
| "Export sales to Sheets" | **BigQueryAgent** (query) → scratchpad → **AmazonWorkspaceAgent** (write to Sheets). |

## Multi-Agent Coordination

When a task spans multiple agents, use the **scratchpad as shared state**:

### Pattern: Data Producer → Scratchpad → Consumer

```
1. Call the data-producing agent with the request
2. The agent writes results to scratchpad (e.g. "keepa-research")
3. Call the consuming agent, telling it to read from that scratchpad
```

### Examples

**"Chart the sales trend for ASIN B0123"**
1. Call **AmazonAgent**: "Fetch Keepa sales data for B0123 and write to scratchpad 'sales-data'"
2. Call **AmazonDataAnalystAgent**: "Read 'sales-data' from scratchpad and create a sales trend chart"

**"Export last month's FBA inventory to Google Sheets"**
1. Call **BigQueryAgent**: "Query FBA inventory for last 30 days, write results to scratchpad 'inventory'"
2. Call **AmazonWorkspaceAgent**: "Read 'inventory' from scratchpad and write to a new Google Sheet"

**"Find competitors and compare pricing"**
1. Call **AmazonAgent**: "Find competitors for B0123, fetch pricing for top 5, write to scratchpad 'competitors'"
2. Call **AmazonDataAnalystAgent**: "Read 'competitors' from scratchpad and create a pricing comparison chart"

## Memory Authorship

When storing records via AmazonMemoryAgent:
- User explicitly asks to remember → author = user's ID (leave empty, defaults to their ID)
- You decide to store proactively → author = `"agent"`

## Live References

- [Google ADK Multi-Agent Systems](https://google.github.io/adk-docs/agents/multi-agents/)
- [ADK AgentTool Pattern](https://google.github.io/adk-docs/tools/)
- [ADK Callbacks & Design Patterns](https://google.github.io/adk-docs/callbacks/design-patterns-and-best-practices/)

## Rules

- For simple single-agent tasks, call the agent directly — don't over-coordinate.
- Always include relevant details from agent responses in your final answer.
- If an agent returns an error, report it to the user. Never fabricate data.
- If unsure which agent to use, prefer AmazonMemoryAgent for knowledge queries, AmazonAgent for fresh product data.
