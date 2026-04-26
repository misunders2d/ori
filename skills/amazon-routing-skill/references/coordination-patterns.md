# Multi-Agent Coordination Patterns

## The Scratchpad Handoff Pattern

When a task spans multiple agents, use the scratchpad as shared state:

```
1. Call the data-producing agent with the request
2. The agent writes results to scratchpad (e.g. "keepa-research")
3. Call the consuming agent, telling it to read from that scratchpad
```

## Worked Examples

### "Chart the sales trend for ASIN B0123"
1. Call **AmazonAgent**: "Fetch Keepa sales data for B0123 and write to scratchpad 'sales-data'"
2. Call **AmazonDataAnalystAgent**: "Read 'sales-data' from scratchpad and create a sales trend chart"

### "Export last month's FBA inventory to Google Sheets"
1. Call **BigQueryAgent**: "Query FBA inventory for last 30 days, write results to scratchpad 'inventory'"
2. Call **AmazonWorkspaceAgent**: "Read 'inventory' from scratchpad and write to a new Google Sheet"

### "Find competitors and compare pricing"
1. Call **AmazonAgent**: "Find competitors for B0123, fetch pricing for top 5, write to scratchpad 'competitors'"
2. Call **AmazonDataAnalystAgent**: "Read 'competitors' from scratchpad and create a pricing comparison chart"

### "Analyze last week's SQP data and find opportunity keywords"
1. Call **AmazonAgent**: "Pull the SQP report for last week via SP-API, save to file"
2. Call **AmazonDataAnalystAgent**: "Analyze the SQP file — find keywords where click share >> impression share"
3. Present the results with the data analyst's findings

### "Save supplier info and check their products"
1. Call **AmazonMemoryAgent**: "Create a record for SupplierCo with contact details"
2. Call **AmazonAgent**: "Fetch Keepa data for the supplier's top ASINs"

## Ambiguous Request Resolution

| Request | Resolution |
|---------|-----------|
| "What do you know about ASIN B0123?" | **AmazonMemoryAgent** first (check existing knowledge). If nothing found, **AmazonAgent** (fetch fresh from Keepa). |
| "Tell me about supplier X" | **AmazonMemoryAgent** (search_knowledge + query_connections). Only fetch fresh data if memory is empty. |
| "Compare these products" | **AmazonAgent** (fetch data for each), then **AmazonDataAnalystAgent** (chart the comparison). |
| "Export sales to Sheets" | **BigQueryAgent** (query) → scratchpad → **AmazonWorkspaceAgent** (write to Sheets). |
| "What's trending in our category?" | **AmazonAgent** (Keepa bestsellers/product finder), then **AmazonDataAnalystAgent** if analysis needed. |

## Memory Authorship

When storing records via AmazonMemoryAgent:
- User explicitly asks to remember → author = user's ID (leave empty, defaults to their ID)
- You decide to store proactively → author = `"agent"`
