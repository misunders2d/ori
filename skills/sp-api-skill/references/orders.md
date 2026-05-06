# Orders Reference

## Tools

### `sp_list_orders(created_after="", created_before="", order_statuses="", days=7, max_results=50)`
List recent orders in the US marketplace. Either pass `created_after` (ISO 8601 timestamp or `YYYY-MM-DD`) or leave it empty and set `days` for a rolling window ending now.

Common `order_statuses` filters (comma-separated): `Shipped, Unshipped, Pending, Canceled, PartiallyShipped, InvoiceUnconfirmed, Unfulfillable`. Omit to include all.

Returns a compact list — one row per order with ID, date, status, fulfillment channel, total, and item count. **Not** the line items.

### `sp_get_order_items(order_id)`
Drill into one specific order. Returns the line items (ASIN, SKU, title, quantity ordered/shipped, item price).

## Pattern: list → drill

```
sp_list_orders(days=1, order_statuses="Unshipped")
# → [{"order_id": "111-1234567-1234567", ...}, ...]

sp_get_order_items(order_id="111-1234567-1234567")
# → [{"asin": "B01EXAMPLE", "sku": "SKU-FOO", "quantity_ordered": 2, ...}]
```

Use this flow for ad-hoc "what sold today" or "what's stuck unshipped" questions. For anything that needs thousands of orders or deep historical analysis, don't loop — request `GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL` instead (see `reports.md`). The API is rate-limited; reports are the efficient path for bulk work.

## Today's Sales

For "today's sales" or "what sold today", use SP-API orders first:
1. `sp_list_orders(days=1)` for recent orders.
2. `sp_get_order_items(order_id=...)` only when line-item detail is needed.
3. Do not route to BigQuery unless the user explicitly asks for BigQuery/SQL/warehouse data.

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
