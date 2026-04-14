# Reports Reference

Amazon reports are the workhorse for bulk data. Most questions that would otherwise require thousands of API calls can be answered with one report. They're also asynchronous — you request, Amazon processes (seconds to tens of minutes), then you download.

## Two flows

### Preferred: one-shot CSV export

For anything that ends in "export this as a CSV" or "pull this data for analysis":

```
export_report_to_csv(report_type="GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA", days=30)
# → {"file_path": "./tmp/exports/fba_myi_unsuppressed_inventory_20260408_1430.csv", "rows": 1234}
```

This runs request → poll → download → parse → CSV in one call. No data passes through the conversation. Use this 90% of the time.

### Manual: when you need to inspect raw data

```
sp_request_report(report_type=..., days=30)      # returns report_id
sp_check_report(report_id=...)                    # poll every 30-60s
sp_download_report(report_document_id=...)        # once status = DONE
```

Use this when you need to peek at the first few rows before deciding how to process the data, or when the report is in a JSON shape (some Brand Analytics reports) that you want to parse structurally rather than CSV-ify.

**Check `sp_list_reports()` before requesting a new one** — a recent one may already exist you can reuse.

### Account health is its own wrapper

For `GET_V2_SELLER_PERFORMANCE_REPORT`, use `sp_get_account_health` instead of the manual flow. See `account-health.md`.

## Common report types

| Report Type | Use Case |
|-------------|----------|
| `GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL` | All orders in date range |
| `GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT` | SQP / brand analytics — needs `report_options: {"reportPeriod": "WEEK"}` |
| `GET_FLAT_FILE_OPEN_LISTINGS_DATA` | Active listings (SKU, price, quantity) |
| `GET_MERCHANT_LISTINGS_ALL_DATA` | All listings with full details |
| `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` | FBA inventory levels (all SKUs) |
| `GET_FBA_INVENTORY_AGED_DATA` | Inventory aging |
| `GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA` | FBA fee estimates (batch) |
| `GET_EXCESS_INVENTORY_DATA` | Excess/stranded inventory |
| `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE` | Financial settlements |
| `GET_SALES_AND_TRAFFIC_REPORT` | Business reports (sessions, conversions) |
| `GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA` | Customer returns |
| `GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA` | Removal orders |
| `GET_SELLER_FEEDBACK_DATA` | Customer feedback |
| `GET_V2_SELLER_PERFORMANCE_REPORT` | Account health (prefer `sp_get_account_health`) |

## Worked examples

See `report-examples.md` for concrete recipes (one-shot export, manual polling, batch pricing runs, brand analytics with `reportPeriod`).

## Live references

- [Reports API v2021-06-30](https://developer-docs.amazon.com/sp-api/docs/reports-api-v2021-06-30-reference)
- [Report type values (full list)](https://developer-docs.amazon.com/sp-api/docs/report-type-values)
- [Official model: reports-api](https://github.com/amzn/selling-partner-api-models/tree/main/models/reports-api-model)
