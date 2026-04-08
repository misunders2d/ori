---
name: sp-api-skill
description: "Amazon Selling Partner API workflow — catalog, listings, competitive pricing, and reports. Covers rate limits, report lifecycle, and data interpretation."
---

# Amazon SP-API Skill

You have access to Amazon Selling Partner API tools for product data, listing details, competitive pricing, and reports. SP-API has strict rate limits — always be efficient with API calls.

## Tools Overview

| Tool | Purpose | Rate Impact |
|------|---------|-------------|
| `sp_get_catalog_item(asin)` | Product details by ASIN (title, bullets, images, attributes) | 1 call |
| `sp_search_catalog(keywords/identifiers)` | Search catalog by keywords or identifiers (ASIN, UPC, EAN) | 1 call |
| `sp_get_listing(sku)` | Your seller-specific listing details (price, inventory, status) | 1 call |
| `sp_get_competitive_pricing(asins/skus)` | Your price vs competitors, buy box info | 1 call for up to 20 items |
| `sp_request_report(report_type, days)` | Request any Amazon report | 1 call |
| `sp_check_report(report_id)` | Check report processing status | 1 call |
| `sp_download_report(report_document_id)` | Download completed report data | 1 call |
| `sp_list_reports(report_type, status, days)` | List existing reports | 1 call |
| `export_report_to_csv(report_type, days)` | **One-shot report→CSV pipeline** (preferred for exports) | 3 calls (auto) |
| `data_to_csv(data, filename)` | Convert any JSON data to CSV directly | 0 calls |

## Key Concepts

### ASIN vs SKU
- **ASIN**: Amazon's product identifier, shared across all sellers. Use with `sp_get_catalog_item` and `sp_get_competitive_pricing`.
- **SKU**: Your seller-specific identifier. Use with `sp_get_listing`.
- If the user gives you an ASIN and you need listing data, you may need to look up the SKU first via a listings report.

### Catalog vs Listing
- **Catalog** = shared product data (title, images, description) — same for all sellers of that ASIN.
- **Listing** = your seller-specific data (your price, quantity, fulfillment channel, listing issues).
- Use catalog for product research, listing for inventory/pricing management.

## Report Workflow

### Preferred: Direct CSV Export (one tool call, zero LLM overhead)

For CSV exports, **ALWAYS use `export_report_to_csv`**. It handles the entire pipeline in one call:
request → poll → download → parse → CSV. No data passes through the conversation.

```
export_report_to_csv(report_type="GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA", days=30)
→ returns: {"file_path": "./tmp/exports/fba_myi_unsuppressed_inventory_20260408_1430.csv", "rows": 1234}
```

Use `data_to_csv` to convert any JSON data you already have into a CSV file directly.

### Manual flow (only when you need to inspect raw data first)

- [ ] Step 1: `sp_request_report(report_type, days)` — returns a `report_id`
- [ ] Step 2: Wait 30-60 seconds, then `sp_check_report(report_id)`
- [ ] Step 3: If status is `IN_PROGRESS` or `IN_QUEUE`, wait and check again
- [ ] Step 4: When status is `DONE`, use `sp_download_report(report_document_id)`
- [ ] Step 5: If the report is large, it's saved to a file — use `analyze_data` to inspect

**Before requesting a new report**, check `sp_list_reports()` — a recent one may already exist.

### Common Report Types

| Report Type | Use Case |
|-------------|----------|
| `GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL` | All orders |
| `GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT` | SQP / brand analytics (needs `report_options: {"reportPeriod": "WEEK"}`) |
| `GET_FLAT_FILE_OPEN_LISTINGS_DATA` | Active listings (SKU, price, quantity) |
| `GET_MERCHANT_LISTINGS_ALL_DATA` | All listings with full details |
| `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` | FBA inventory levels |
| `GET_FBA_INVENTORY_AGED_DATA` | Inventory aging |
| `GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA` | FBA fee estimates |
| `GET_EXCESS_INVENTORY_DATA` | Excess/stranded inventory |
| `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE` | Financial settlements |
| `GET_SALES_AND_TRAFFIC_REPORT` | Business reports (sessions, conversions) |
| `GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA` | Customer returns |
| `GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA` | Removal orders |
| `GET_SELLER_FEEDBACK_DATA` | Customer feedback |

## Rate Limit Strategy

SP-API uses a **token bucket algorithm**. Tokens regenerate per second, and each call consumes one.

### Rules
1. **Prefer reports over repeated individual calls.** 1 report = bulk data for 2-3 API calls. 100 `sp_get_listing` calls = 100 API calls for the same data.
2. **Batch competitive pricing.** `sp_get_competitive_pricing` accepts up to 20 ASINs/SKUs per call.
3. **Minimize `included_data`.** Only request the data sections you need.
4. **If throttled, the tool auto-retries** with exponential backoff (up to 3 attempts). If all retries fail, report the error to the user — do NOT keep hammering.
5. **Never retry 400/403 errors** — those indicate bad input or missing permissions, not rate limits.

## Competitive Pricing Interpretation

`sp_get_competitive_pricing` returns pricing structures including:
- **Your price** vs **lowest competitor price**
- **Buy Box price** (the price shown on the product page)
- **Number of offers** from other sellers
- **Landed price** (item price + shipping)

When reporting prices, always specify whether you're quoting the landed price or just the item price.

## Included Data Options

### For `sp_get_catalog_item`:
`summaries, attributes, identifiers, images, productTypes, salesRanks, relationships, classifications, dimensions, vendorDetails`

### For `sp_get_listing`:
`summaries, attributes, issues, offers, fulfillmentAvailability, procurement, relationships, productTypes`

### For `sp_search_catalog`:
`summaries, images, identifiers, attributes, productTypes, salesRanks, relationships, classifications, dimensions`

## Gotchas

- **Reports are NOT instant.** Always poll status before downloading. Most take 1-30 minutes.
- **Brand Analytics reports require `reportPeriod` in options.** Use `{"reportPeriod": "WEEK"}` and set `days` to cover the target week.
- **SKU and ASIN are NOT interchangeable.** Catalog tools use ASIN, listing tools use SKU.
- **Competitive pricing max batch = 20.** Split larger lists into chunks.
- **Large reports are saved to file.** The tool returns a file path and preview. Use `analyze_data` for the full data.
- **Marketplace is US-only** (`ATVPDKIKX0DER`). All tools are pre-configured for the US marketplace.
- **If any tool returns an error, report it immediately.** Never fabricate data or silently fall back.
- **4xx errors (except 429) = don't retry.** 400 = bad input, 403 = permissions issue, 404 = ASIN/SKU doesn't exist.
