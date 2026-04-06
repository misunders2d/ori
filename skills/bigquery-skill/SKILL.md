---
name: bigquery-skill
description: "Domain knowledge for querying Mellanni BigQuery datasets. Table catalog, access control, query patterns, and aggregation rules for the BigQueryAgent."
---

# BigQuery Data Science Skill

You have read-only access to BigQuery via the ADK BigQueryToolset. Your job is to answer business questions by writing correct, efficient SQL.

## Procedure: Answering a Data Question

- [ ] Step 1: **Discover** — call `get_table_data` to see available datasets and tables. Read descriptions.
- [ ] Step 2: **Schema check** — use `get_table_info` on every table you plan to query. Read column descriptions. Obey them.
- [ ] Step 3: **Time awareness** — call `get_current_time` to know today's date before writing any date filter.
- [ ] Step 4: **Write query** — follow the aggregation rules and precautions below. Use fully qualified table names (`project.dataset.table`).
- [ ] Step 5: **Validate** — if the result looks wrong (missing data, unexpected nulls, inflated numbers), check for JOIN duplication or missing filters. Run a verification query.

## Key Datasets

- `mellanni-project-da.reports` — primary Amazon reporting (business reports, inventory, shipments, advertising)
- `mellanni-project-da.auxillary_development` — supplementary data (product dictionaries, changelogs, keywords)

Load `references/table_data.json` to see the full catalog with per-table descriptions and access control.

## The Dictionary Table

`mellanni-project-da.auxillary_development.dictionary` is the **master product mapping table** for USA.
- Contains SKU, ASIN, Collection, and product parameters.
- "Product" or "collection" in user requests → `Collection` column.
- **ALWAYS** include this table when querying collection or product performance.
- **WARNING**: ASINs have duplicates. Aggregate or deduplicate before joining.
- Country-specific variants: `dictionary_ca` (Canada), `dictionary_eu` (Europe), `dictionary_uk` (UK), `dictionary_wm` (Walmart), `dictionary_shp` (Shopify).

## Aggregation Rules

1. **No QUALIFY for "latest"** — use the "max date" method: `WHERE date = (SELECT MAX(date) FROM ...)`.
2. **Time-period metrics** — SUM() each metric independently over the full period. Never sum pre-aggregated totals.
3. **Distinct counts** — COUNT(DISTINCT ...) over the full period. Never sum daily distinct counts.
4. **JOIN inflation** — aggregate each side independently before joining. Verify row counts after joins.

## Calculation Precautions

- **Averages**: include zero-sales days. Use `SUM(units) / COUNT(DISTINCT date)`, not `AVG(units)`. Confirm with the user how they want averages calculated.
- **Duplicates**: always check for duplicates in join columns before joining. Aggregate first.
- **Country default**: if the user doesn't specify, assume USA. Check relevant columns for marketplace/country filters.
- **Table existence**: verify a table exists before querying it.

## Access Control

Some tables in `references/table_data.json` have an `authorized_users` field — a list of email addresses allowed to query that table. The `before_tool_callback` enforces this automatically. Admin users bypass all restrictions. If a user is denied, tell them to contact an admin.

## Gotchas

- **Read-only**: WriteMode is BLOCKED. Never attempt INSERT, UPDATE, DELETE, CREATE, or DROP. The toolset will reject it.
- **Result limit**: max 10,000 rows per query. If you need more, use aggregation or pagination.
- **SAFE_DIVIDE**: use `SAFE_DIVIDE(a, b)` instead of `a / b` to avoid division-by-zero errors.
- **Date columns vary by table**: some use `date`, others `report_date`, `order_date`, etc. Always check schema first.
- **Prime Day exclusions**: when calculating normal averages, consider excluding Prime Day event dates (check `sku_changelog` for event periods).
- **Simulated data**: never output made-up numbers. If you can't find the data, say so.
