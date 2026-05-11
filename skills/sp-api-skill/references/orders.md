# Orders Reference

## Tools

> The previous `sp_list_orders` tool was **removed** — it returned only the first ~50 orders and produced misleading partial sales totals. Use the report path below for any "how many orders / units / dollars" question.

### `sp_get_order_items(order_id)`
Drill into one specific order. Returns the line items (ASIN, SKU, title, quantity ordered/shipped, item price). The `order_id` comes from a downloaded report row.

## "Today's sales" / any order-count question → report path

```
sp_request_report(report_type="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL", days=1)
# → returns report_id; processing takes 1-15 minutes on Amazon's side

sp_check_report(report_id="...")
# → poll until processing_status == "DONE"; returns report_document_id

sp_download_report(report_document_id="...")
# → returns file_path to a TSV with ALL orders for the window (pending + shipped + canceled),
#   including totals for pending orders that the live OrdersV0 endpoint omits.
```

Or use the one-shot wrapper `export_report_to_csv(report_type, days)` which does the full pipeline + saves a CSV. Then route the CSV path to AmazonDataAnalystAgent for analysis (totals, hourly chart, etc.).

**Why reports, not list-orders pagination:**
- OrdersV0's GetOrders has a strict rate limit (~1 call/min sustained) — paginating 900+ orders would take 18+ minutes even if you wanted to.
- Pending orders return `total: null` in OrdersV0 — your sum is always undercount.
- The report includes everything in one file, authoritatively.

**Timezone:** "today" means Pacific (`America/Los_Angeles`) for this user unless they specify otherwise. Call `get_current_time(timezone="America/Los_Angeles")` first, compute your day window in Pacific, then pass that window to the report request. Reports honour the `data_start_time` / `data_end_time` you pass.

## What's NOT in here

- **Buyer names / shipping addresses / buyer email.** These are PII and require the Restricted Data Token (RDT) flow via the Tokens API. This tool intentionally doesn't expose them. If the user needs buyer info, tell them it requires elevated permissions we haven't wired up.
- **Settlement / fee detail per order.** That's the Finances API (not yet exposed) or the settlement report `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE`.

## Date formats

ISO 8601 is safest: `2026-04-01T00:00:00Z`. Plain `YYYY-MM-DD` also works and is interpreted as midnight UTC on that date.

## Pagination

The `has_more` flag in the response indicates more pages exist. This tool returns only the first page to keep responses compact. If the user needs everything, narrow the window instead of paginating — or fall back to a report.

## Live references

- [Orders API v0](https://developer-docs.amazon.com/sp-api/docs/orders-api-v0-reference)
- [Official model: orders-api](https://github.com/amzn/selling-partner-api-models/tree/main/models/orders-api-model)
- [Tokens API (for RDT / buyer info — not yet exposed)](https://developer-docs.amazon.com/sp-api/docs/tokens-api-v2021-03-01-reference)
