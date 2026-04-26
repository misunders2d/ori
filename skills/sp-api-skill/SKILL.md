---
name: sp-api-skill
description: "Amazon Selling Partner API workflow — product catalog, listings, competitive pricing, fee estimates, orders, FBA inventory, reports, and account health monitoring. Use this skill whenever the user asks anything about their Amazon seller account, ASINs, SKUs, inventory levels, order details, policy violations, account health, margin calculations, or any SP-API data — even if they don't name the API explicitly."
---

# Amazon SP-API

You have read-only access to the Amazon Selling Partner API for product research, listing management, competitive pricing, order retrieval, FBA inventory, bulk reports, and account health monitoring. SP-API has strict rate limits — reach for the right tool for the job and avoid making individual calls in loops when a report could give you the same data in one shot.

## How to navigate this skill

SKILL.md is the routing doc. Each API domain has its own reference file — read the one matching the question rather than trying to hold everything in your head.

| User is asking about... | Read |
|---|---|
| ASIN/product details, search, SKU listing state | [`references/catalog-and-listings.md`](references/catalog-and-listings.md) |
| Price vs. competitors, Buy Box, offer landscape | [`references/pricing.md`](references/pricing.md) |
| Fee math, profitability, "what's my take-home at $X?" | [`references/product-fees.md`](references/product-fees.md) |
| Recent orders, line items on a specific order | [`references/orders.md`](references/orders.md) |
| Live FBA stock, "do we have it in stock?" | [`references/inventory.md`](references/inventory.md) |
| AHR, policy violations, suspension risk, is my account healthy? | [`references/account-health.md`](references/account-health.md) |
| Bulk data exports, any report-based flow | [`references/reports.md`](references/reports.md) + [`references/report-examples.md`](references/report-examples.md) |

## Tool inventory (quick reference)

| Tool | Purpose |
|------|---------|
| `sp_get_catalog_item(asin)` | Product details by ASIN |
| `sp_search_catalog(keywords/identifiers)` | Catalog search |
| `sp_get_listing(sku)` | Your listing for a SKU |
| `sp_get_competitive_pricing(asins/skus)` | Price vs. competitors, up to 20 items |
| `sp_get_fees_estimate(asin, price, is_fba)` | Fee breakdown for profitability math |
| `sp_list_orders(days, order_statuses)` | Recent orders (compact summaries) |
| `sp_get_order_items(order_id)` | Line items for one order |
| `sp_get_inventory_summaries(skus)` | Live FBA stock (fulfillable / inbound / reserved) |
| `sp_get_account_health(days)` | **One-shot account health digest** |
| `export_report_to_csv(report_type, days)` | **One-shot report → CSV pipeline** |
| `sp_request_report` / `sp_check_report` / `sp_download_report` / `sp_list_reports` | Manual report lifecycle |
| `data_to_csv(data, filename)` | Convert any JSON you already have into CSV |

## Cross-cutting rules

### Author identifier for records

Every SP-API call runs as the configured seller. Marketplace is US-only (`ATVPDKIKX0DER`). If the user needs a non-US marketplace, tell them the tools aren't wired for that yet — don't try to pass a different marketplace ID.

### Rate limits

SP-API uses a token-bucket limiter per endpoint. Tools here retry throttling (429) with exponential backoff up to 3 attempts. Beyond that:

- **Prefer reports over loops.** 100 individual `sp_get_listing` calls = 100 API calls. One `GET_MERCHANT_LISTINGS_ALL_DATA` report = 3 calls for everything.
- **Batch competitive pricing** — up to 20 ASINs/SKUs per call.
- **Minimize `included_data`** — only request sections you need.
- **Never retry 4xx (except 429).** 400 = bad input, 403 = permissions, 404 = ASIN/SKU doesn't exist. Report to the user, don't hammer.

### Error reporting

If any tool returns a non-success status, report the error immediately. Never fabricate data to paper over a failure.

### Reports take time

Reports are asynchronous — most take 1–30 minutes. Use `export_report_to_csv` or `sp_get_account_health` for fire-and-forget one-shot flows; only use the manual request/check/download trio when you specifically need to inspect raw data before processing.

### What's NOT available

- **Write operations.** Updating listings, prices, inventory, images — none of that is exposed (intentional, this integration is read-only).
- **Seller-support cases.** Case creation, listing, or reading via API is not supported by Amazon — it's Seller Central UI-only. If the user asks about opening or reading a support case, explain this; don't pretend there's a tool.
- **Buyer PII (names, addresses, email).** Requires the Restricted Data Token (RDT) flow via the Tokens API — not wired up here. Order summaries include buyer-flagged booleans (IsPrime, IsBusinessOrder) but not personal data.
- **Non-US marketplaces.** Tools are hardcoded to the US marketplace. EU/JP/MX would require work.

## Live references

- [SP-API Documentation](https://developer-docs.amazon.com/sp-api/)
- [SP-API Report Types](https://developer-docs.amazon.com/sp-api/docs/report-type-values)
- [SP-API Rate Limits](https://developer-docs.amazon.com/sp-api/docs/usage-plans-and-rate-limits)
- [Official SP-API models (Amazon)](https://github.com/amzn/selling-partner-api-models/tree/main/models)
