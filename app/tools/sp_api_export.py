"""SP-API direct report-to-CSV pipeline — request, poll, download, convert in one call.

Bypasses LLM round-trips entirely. The agent calls one tool, the tool handles
the full lifecycle: request report → poll until ready → download → parse → CSV.
Zero tokens wasted on data transformation.
"""

import asyncio
import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_US_MARKETPLACE = "ATVPDKIKX0DER"
_EXPORTS_DIR = os.path.abspath("./tmp/exports")
_MAX_RETRIES = 3
_POLL_INTERVAL = 15  # seconds between status checks
_MAX_POLL_ATTEMPTS = 40  # 40 * 15s = 10 minutes max wait


def _get_credentials() -> dict | None:
    client_id = os.environ.get("SP_API_CLIENT_ID", "")
    client_secret = os.environ.get("SP_API_CLIENT_SECRET", "")
    refresh_token = os.environ.get("SP_API_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        return None
    return dict(
        lwa_app_id=client_id,
        lwa_client_secret=client_secret,
        refresh_token=refresh_token,
    )


async def _sp_call(fn, *args, **kwargs):
    """Execute SP-API call with retry on throttling."""
    from sp_api.base import SellingApiRequestThrottledException, SellingApiException

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = fn(*args, **kwargs)
            return response.payload
        except SellingApiRequestThrottledException:
            wait = min(2 ** attempt, 30)
            logger.warning("SP-API throttled (attempt %d), waiting %ds", attempt, wait)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(wait)
            else:
                raise
        except SellingApiException:
            raise


def _parse_flat_file(text: str) -> list[dict]:
    """Parse a tab-delimited flat file report into a list of dicts."""
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    return list(reader)


def _parse_json_report(text: str) -> list[dict]:
    """Parse a JSON report into a flat list of dicts."""
    data = json.loads(text)

    # Brand analytics reports have nested structures — try to flatten
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Try common keys
        for key in ("reportData", "dataByAsin", "dataByDepartment", "records", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
        # Single-level dict → wrap
        return [data]
    return []


def _save_csv(records: list[dict], filename: str) -> str:
    """Write records to a CSV file and return the path."""
    os.makedirs(_EXPORTS_DIR, exist_ok=True)
    path = os.path.join(_EXPORTS_DIR, filename)

    if not records:
        with open(path, "w") as f:
            f.write("")
        return path

    # Collect all keys across all records for consistent columns
    all_keys = []
    seen = set()
    for record in records:
        for key in record:
            if key not in seen:
                all_keys.append(key)
                seen.add(key)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    return path


async def export_report_to_csv(
    report_type: str,
    days: int = 30,
    filename: str = "",
    report_options: str = "{}",
    tool_context: ToolContext = None,
) -> dict:
    """Request an Amazon report and export it directly as a CSV file.

    This is a one-shot pipeline: request → wait → download → CSV. No intermediate
    LLM calls needed. The data never passes through the conversation — only the
    file path is returned.

    Args:
        report_type: SP-API report type. Common types:
            - GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL (all orders)
            - GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA (FBA inventory)
            - GET_FLAT_FILE_OPEN_LISTINGS_DATA (active listings)
            - GET_FBA_INVENTORY_AGED_DATA (inventory aging)
            - GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA (FBA fee estimates)
            - GET_EXCESS_INVENTORY_DATA (excess inventory)
            - GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA (customer returns)
            - GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE (settlements)
            - GET_SALES_AND_TRAFFIC_REPORT (business reports)
            - GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT (brand analytics)
        days: Number of days of data (default 30, max 90).
        filename: Output filename (default: auto-generated from report type and date).
        report_options: Optional JSON string for report-specific options.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured. Set via /init."}

    try:
        options = json.loads(report_options) if report_options and report_options != "{}" else None
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON in report_options."}

    days = min(max(days, 1), 90)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    from sp_api.api import Reports
    reports = Reports(credentials=creds)

    # Step 1: Request the report
    try:
        kwargs = {
            "reportType": report_type,
            "marketplaceIds": [_US_MARKETPLACE],
            "dataStartTime": start,
            "dataEndTime": end,
        }
        if options:
            kwargs["reportOptions"] = options

        payload = await _sp_call(reports.create_report, **kwargs)
        report_id = payload.get("reportId", "")
        if not report_id:
            return {"status": "error", "message": "No report ID returned from Amazon."}
    except Exception as e:
        return {"status": "error", "message": f"Failed to request report: {e}"}

    # Step 2: Poll until ready
    document_id = None
    for poll in range(1, _MAX_POLL_ATTEMPTS + 1):
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            payload = await _sp_call(reports.get_report, reportId=report_id)
            status = payload.get("processingStatus", "")

            if status == "DONE":
                document_id = payload.get("reportDocumentId")
                break
            elif status in ("CANCELLED", "FATAL"):
                return {"status": "error", "message": f"Report failed with status: {status}"}
            # IN_PROGRESS / IN_QUEUE — keep polling
        except Exception as e:
            return {"status": "error", "message": f"Failed to check report status: {e}"}

    if not document_id:
        return {"status": "error", "message": f"Report not ready after {_MAX_POLL_ATTEMPTS * _POLL_INTERVAL}s. Try sp_check_report('{report_id}') later."}

    # Step 3: Download
    try:
        payload = await _sp_call(
            reports.get_report_document, reportDocumentId=document_id, download=True
        )
        raw_document = payload.get("document", "")
    except Exception as e:
        return {"status": "error", "message": f"Failed to download report: {e}"}

    if not raw_document:
        return {"status": "error", "message": "Report document is empty."}

    # Step 4: Parse into records
    try:
        records = json.loads(raw_document)
        if isinstance(records, dict):
            records = _parse_json_report(json.dumps(records))
        elif not isinstance(records, list):
            records = _parse_json_report(raw_document)
    except (json.JSONDecodeError, TypeError):
        # Most reports are tab-delimited flat files
        records = _parse_flat_file(str(raw_document))

    if not records:
        return {"status": "error", "message": "Report parsed but contained no data rows."}

    # Step 5: Write CSV
    if not filename:
        short_type = report_type.replace("GET_", "").replace("_DATA", "").lower()[:40]
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        filename = f"{short_type}_{date_str}.csv"

    if not filename.endswith(".csv"):
        filename += ".csv"

    path = _save_csv(records, filename)
    file_size = os.path.getsize(path)

    return {
        "status": "success",
        "file_path": path,
        "filename": filename,
        "rows": len(records),
        "columns": len(records[0]) if records else 0,
        "size_bytes": file_size,
        "message": f"Exported {len(records)} rows to {filename} ({file_size:,} bytes). "
                   f"Use analyze_data or H10 tools to inspect the file.",
    }


async def data_to_csv(
    data: str,
    filename: str = "export.csv",
    tool_context: ToolContext = None,
) -> dict:
    """Convert structured data directly to a CSV file without code execution.

    Use this when you already have data (from a tool response, API result, etc.)
    and just need to save it as CSV. No pandas code needed.

    Args:
        data: JSON string of the data. Must be one of:
            - A JSON array of objects: [{"col1": "val1", "col2": "val2"}, ...]
            - A JSON object with a key containing an array: {"items": [...]}
        filename: Output filename (default: export.csv).
    """
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": "Invalid JSON data. Provide a JSON array of objects."}

    # Unwrap if it's a dict containing a list
    if isinstance(parsed, dict):
        for key in ("items", "data", "records", "results", "rows", "reports", "payload"):
            if key in parsed and isinstance(parsed[key], list):
                parsed = parsed[key]
                break
        else:
            # Single record → wrap
            parsed = [parsed]

    if not isinstance(parsed, list):
        return {"status": "error", "message": "Data must be a JSON array of objects."}

    if not parsed:
        return {"status": "error", "message": "Data array is empty."}

    # Flatten nested dicts one level deep
    flat_records = []
    for record in parsed:
        if not isinstance(record, dict):
            continue
        flat = {}
        for k, v in record.items():
            if isinstance(v, (dict, list)):
                flat[k] = json.dumps(v)
            else:
                flat[k] = v
        flat_records.append(flat)

    if not flat_records:
        return {"status": "error", "message": "No valid records found in data."}

    if not filename.endswith(".csv"):
        filename += ".csv"

    path = _save_csv(flat_records, filename)
    file_size = os.path.getsize(path)

    return {
        "status": "success",
        "file_path": path,
        "filename": filename,
        "rows": len(flat_records),
        "columns": len(flat_records[0]) if flat_records else 0,
        "size_bytes": file_size,
        "message": f"Saved {len(flat_records)} rows to {filename}.",
    }
