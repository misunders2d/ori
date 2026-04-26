# FBA Inventory Reference

## Tool

### `sp_get_inventory_summaries(skus="", details=True)`
Live FBA inventory snapshot. Filter to specific SKUs (comma-separated) when you know what you're looking at — unfiltered calls paginate heavily and cost more quota.

## What each quantity means

When `details=True` (default), each item includes a breakdown. Understanding what's what matters because "in stock" has many flavors in FBA land:

| Field | What it means |
|---|---|
| `fulfillable` | Units Amazon will actually sell right now |
| `inbound_working` | You've created a shipment plan but haven't shipped yet |
| `inbound_shipped` | In transit to Amazon's warehouses |
| `inbound_receiving` | Arrived, being checked in |
| `reserved` | Allocated to pending customer orders / FC transfers (not available to sell yet) |
| `unfulfillable` | Damaged, defective, or otherwise unsellable — may need a removal order |
| `total` | Sum of all the above |

**"Out of stock" in practice means `fulfillable == 0`, not `total == 0`.** A SKU can have plenty in the inbound pipeline and still show as unavailable on the listing because fulfillable is empty.

## When to use this vs. a report

Use `sp_get_inventory_summaries` when:
- You want a **live snapshot** for 1-50 specific SKUs
- The question is "do we have it in stock right now?"

Use `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` (via `export_report_to_csv`) when:
- You need **every** SKU, not a filtered subset
- Analysis is downstream (spreadsheets, BI, etc.)
- You're willing to wait 1–3 minutes for fresher-than-cache but not-live data

The report takes minutes but gives you everything in one CSV. The API call is instant but paginated.

## Example

```
# Check live stock for a handful of SKUs
sp_get_inventory_summaries(skus="SKU-FOO,SKU-BAR,SKU-BAZ")

# All inventory (first page only — will paginate)
sp_get_inventory_summaries()
```

## Live references

- [FBA Inventory API v1](https://developer-docs.amazon.com/sp-api/docs/fba-inventory-api-v1-reference)
- [Official model: fba-inventory](https://github.com/amzn/selling-partner-api-models/tree/main/models/fba-inventory-api-model)
