# SP-API Reports — Worked Examples

## Example 1: One-Shot CSV Export (Preferred)

```
User: "Export my FBA inventory to CSV"

1. export_report_to_csv(
     report_type="GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
     days=30
   )
   → Returns: {"file_path": "./tmp/exports/fba_inventory_20260409.csv", "rows": 1523}
   → File is auto-attached and delivered to user

Done. One tool call. No polling needed.
```

## Example 2: Manual Report Flow (When You Need to Inspect First)

```
User: "Pull the sales and traffic report for the last 7 days, but only show ASINs with >100 sessions"

1. sp_request_report(
     report_type="GET_SALES_AND_TRAFFIC_REPORT",
     days=7
   )
   → Returns: {"report_id": "ABC123"}

2. (Wait 30 seconds)
   sp_check_report(report_id="ABC123")
   → Returns: {"status": "IN_PROGRESS"}

3. (Wait 60 seconds)
   sp_check_report(report_id="ABC123")
   → Returns: {"status": "DONE", "report_document_id": "DOC456"}

4. sp_download_report(report_document_id="DOC456")
   → Returns data (or file path if large)

5. If large, pass file_path to AmazonDataAnalystAgent:
   "Analyze this file. Filter to ASINs with >100 sessions. Show units, revenue, conversion rate."
```

## Example 3: Competitive Pricing (Batch)

```
User: "Compare our prices against competitors for these 10 ASINs"

1. sp_get_competitive_pricing(
     asins="B0CJ5G3GGN,B08Q2BQXX1,B09K3LMXYZ,..."
   )
   → Returns per-ASIN: your price, lowest competitor, buy box price, number of offers

2. Write to scratchpad for analysis:
   scratchpad_write("pricing-comparison", formatted_results)
```

## Example 4: Brand Analytics / SQP via Reports

```
User: "Get last week's Search Query Performance data"

1. sp_request_report(
     report_type="GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT",
     days=7,
     report_options={"reportPeriod": "WEEK"}
   )
   → Note: reportPeriod is REQUIRED for brand analytics reports

2. Poll → Download → Large file → Pass to DataAnalyst

3. For multi-week analysis, request each week separately, then have
   DataAnalyst combine with proper weighted aggregation (see data-analysis-skill)
```

## Example 5: Check Existing Reports Before Requesting

```
User: "Get my inventory report"

1. FIRST check if a recent one exists:
   sp_list_reports(
     report_type="GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA",
     status="DONE",
     days=1
   )
   → If recent report found, download it directly — don't request a new one

2. Only sp_request_report if no recent one exists
```

## Large File Handoff Pattern

When a report is too large to process in conversation:
1. Download returns a file_path (e.g. `./tmp/exports/report_20260409.csv`)
2. Pass the file_path to AmazonDataAnalystAgent via the head agent
3. DataAnalyst runs `analyze_data(file_path, code)` with pandas
4. DataAnalyst writes summary to scratchpad
5. You read scratchpad and present to user

This avoids hitting token limits on large reports (10K+ rows).
