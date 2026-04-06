"""Amazon Selling Partner API tools — product catalog, listings, pricing, reports.

Phase 1: Read-only operations.
Phase 2 (future): Write operations (update listings, images, etc.).
Uses python-amazon-sp-api SDK with rate limit handling.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_US_MARKETPLACE = "ATVPDKIKX0DER"
_MAX_RETRIES = 3


def _get_credentials() -> dict | None:
    """Build SP-API credentials dict from vault."""
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


def _get_seller_id() -> str:
    return os.environ.get("SP_API_SELLER_ID", "")


async def _call_with_retry(fn, *args, **kwargs) -> dict:
    """Execute an SP-API call with progressive backoff on throttling."""
    from sp_api.base import SellingApiRequestThrottledException, SellingApiException

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = fn(*args, **kwargs)
            return {"ok": True, "payload": response.payload}
        except SellingApiRequestThrottledException as e:
            wait = min(2 ** attempt, 30)
            logger.warning("SP-API throttled (attempt %d/%d), waiting %ds", attempt, _MAX_RETRIES, wait)
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(wait)
            else:
                return {"ok": False, "error": f"Rate limited after {_MAX_RETRIES} retries. Amazon's token bucket is exhausted — wait a minute and try again."}
        except SellingApiException as e:
            return {"ok": False, "error": f"SP-API error: {e}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "Max retries exceeded."}


# ---------------------------------------------------------------------------
# CATALOG
# ---------------------------------------------------------------------------

async def sp_get_catalog_item(
    asin: str,
    included_data: str = "summaries,attributes,images,identifiers,salesRanks",
    tool_context: ToolContext = None,
) -> dict:
    """Get detailed product information from the Amazon catalog by ASIN.

    Args:
        asin: Amazon Standard Identification Number (e.g., B0123456789).
        included_data: Comma-separated data to include. Options: summaries, attributes,
                       identifiers, images, productTypes, salesRanks, relationships,
                       classifications, dimensions, vendorDetails. Default includes the
                       most useful fields.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured. Set via /init."}

    from sp_api.api import CatalogItems
    catalog = CatalogItems(credentials=creds)
    result = await _call_with_retry(
        catalog.get_catalog_item,
        asin=asin,
        marketplaceIds=[_US_MARKETPLACE],
        includedData=included_data.split(","),
    )
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}
    return {"status": "success", "data": result["payload"]}


async def sp_search_catalog(
    keywords: str = "",
    identifiers: str = "",
    identifier_type: str = "ASIN",
    included_data: str = "summaries,images,identifiers",
    page_size: int = 10,
    tool_context: ToolContext = None,
) -> dict:
    """Search the Amazon catalog by keywords or identifiers (ASIN, UPC, EAN).

    Args:
        keywords: Search keywords (e.g., "bed sheets queen size").
        identifiers: Comma-separated identifiers to look up (e.g., "B0123456789,B0987654321").
        identifier_type: Type of identifiers. One of: ASIN, EAN, GTIN, ISBN, JAN, MINSAN, SKU, UPC.
        included_data: Comma-separated data to include. Default: summaries,images,identifiers.
        page_size: Number of results (1-20, default 10).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}
    if not keywords and not identifiers:
        return {"status": "error", "message": "Either 'keywords' or 'identifiers' is required."}

    from sp_api.api import CatalogItems
    catalog = CatalogItems(credentials=creds)

    kwargs = {
        "marketplaceIds": [_US_MARKETPLACE],
        "includedData": included_data.split(","),
        "pageSize": min(max(page_size, 1), 20),
    }
    if keywords:
        kwargs["keywords"] = keywords
    if identifiers:
        kwargs["identifiers"] = identifiers.split(",")
        kwargs["identifiersType"] = identifier_type

    result = await _call_with_retry(catalog.search_catalog_items, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    items = result["payload"].get("items", [])
    return {
        "status": "success",
        "total": result["payload"].get("numberOfResults", len(items)),
        "items": items,
    }


# ---------------------------------------------------------------------------
# LISTINGS
# ---------------------------------------------------------------------------

async def sp_get_listing(
    sku: str,
    included_data: str = "summaries,attributes,issues,offers,fulfillmentAvailability,productTypes",
    tool_context: ToolContext = None,
) -> dict:
    """Get listing details for one of your SKUs.

    Args:
        sku: Your seller SKU.
        included_data: Comma-separated data to include. Options: summaries, attributes,
                       issues, offers, fulfillmentAvailability, procurement, relationships,
                       productTypes.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}
    seller_id = _get_seller_id()
    if not seller_id:
        return {"status": "error", "message": "SP_API_SELLER_ID not configured."}

    from sp_api.api import ListingsItems
    listings = ListingsItems(credentials=creds)
    result = await _call_with_retry(
        listings.get_listings_item,
        sellerId=seller_id,
        sku=sku,
        marketplaceIds=[_US_MARKETPLACE],
        includedData=included_data.split(","),
    )
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}
    return {"status": "success", "data": result["payload"]}


# ---------------------------------------------------------------------------
# PRICING
# ---------------------------------------------------------------------------

async def sp_get_competitive_pricing(
    asins: str = "",
    skus: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Get competitive pricing data for products (your price vs competitors).

    Provide either ASINs or SKUs, not both.

    Args:
        asins: Comma-separated ASINs (max 20).
        skus: Comma-separated SKUs (max 20).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}
    if not asins and not skus:
        return {"status": "error", "message": "Either 'asins' or 'skus' is required."}

    from sp_api.api import Products
    products = Products(credentials=creds)

    if asins:
        asin_list = [a.strip() for a in asins.split(",")][:20]
        result = await _call_with_retry(
            products.get_competitive_pricing_for_asins,
            asin_list=asin_list,
            MarketplaceId=_US_MARKETPLACE,
        )
    else:
        sku_list = [s.strip() for s in skus.split(",")][:20]
        result = await _call_with_retry(
            products.get_competitive_pricing_for_skus,
            seller_sku_list=sku_list,
            MarketplaceId=_US_MARKETPLACE,
        )

    if not result["ok"]:
        return {"status": "error", "message": result["error"]}
    return {"status": "success", "data": result["payload"]}


# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------

async def sp_request_report(
    report_type: str,
    days: int = 30,
    report_options: str = "{}",
    tool_context: ToolContext = None,
) -> dict:
    """Request an Amazon report. Returns a report ID to check status with sp_check_report.

    Args:
        report_type: The report type identifier. Common types:
            - GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL (all orders)
            - GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT (SQP/brand analytics)
            - GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA (removal orders)
            - GET_EXCESS_INVENTORY_DATA (excess inventory)
            - GET_FLAT_FILE_OPEN_LISTINGS_DATA (active listings)
            - GET_FBA_INVENTORY_AGED_DATA (inventory aging)
            - GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA (FBA fees)
            - GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE (settlements)
            - GET_SALES_AND_TRAFFIC_REPORT (business reports)
            - GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA (returns)
            Use any valid SP-API ReportType string.
        days: Number of days of data to request (default 30, max 90).
        report_options: Optional JSON string of report-specific options
            (e.g., {"reportPeriod": "WEEK"} for brand analytics).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    try:
        options = json.loads(report_options) if report_options and report_options != "{}" else None
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON in report_options."}

    days = min(max(days, 1), 90)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    from sp_api.api import Reports
    reports = Reports(credentials=creds)

    kwargs = {
        "reportType": report_type,
        "marketplaceIds": [_US_MARKETPLACE],
        "dataStartTime": start,
        "dataEndTime": end,
    }
    if options:
        kwargs["reportOptions"] = options

    result = await _call_with_retry(reports.create_report, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    report_id = result["payload"].get("reportId", "")
    return {
        "status": "success",
        "report_id": report_id,
        "message": f"Report requested. Use sp_check_report(report_id='{report_id}') to check status.",
    }


async def sp_check_report(
    report_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Check the status of a requested report.

    Args:
        report_id: The report ID returned by sp_request_report.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    from sp_api.api import Reports
    reports = Reports(credentials=creds)
    result = await _call_with_retry(reports.get_report, reportId=report_id)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    payload = result["payload"]
    response = {
        "status": "success",
        "processing_status": payload.get("processingStatus"),
        "report_type": payload.get("reportType"),
        "created_time": payload.get("createdTime"),
    }

    if payload.get("processingStatus") == "DONE":
        response["report_document_id"] = payload.get("reportDocumentId")
        response["message"] = f"Report ready. Use sp_download_report(report_document_id='{payload.get('reportDocumentId')}') to download."
    elif payload.get("processingStatus") in ("IN_PROGRESS", "IN_QUEUE"):
        response["message"] = "Report still processing. Check again in 30-60 seconds."
    else:
        response["message"] = f"Report status: {payload.get('processingStatus')}"

    return response


async def sp_download_report(
    report_document_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Download a completed report by its document ID.

    Args:
        report_document_id: The document ID from sp_check_report (when status is DONE).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    from sp_api.api import Reports
    reports = Reports(credentials=creds)
    result = await _call_with_retry(
        reports.get_report_document,
        reportDocumentId=report_document_id,
        download=True,
    )
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    document = result["payload"].get("document", "")

    # Try to parse as JSON (brand analytics reports)
    try:
        parsed = json.loads(document)
        text = json.dumps(parsed, indent=2)
    except (JSONDecodeError, TypeError):
        text = str(document)

    # Save to file if large
    if len(text) > 15000:
        os.makedirs("./tmp/reports", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = f"./tmp/reports/report_{timestamp}.txt"
        with open(filepath, "w") as f:
            f.write(text)

        return {
            "status": "success",
            "message": f"Report is large ({len(text)} chars). Saved to {filepath}. Use analyze_data tool to inspect.",
            "file_path": filepath,
            "preview": text[:3000] + f"\n... ({len(text) - 3000} more chars, see file)",
        }

    return {"status": "success", "data": text}


async def sp_list_reports(
    report_type: str = "",
    processing_status: str = "DONE",
    days: int = 7,
    tool_context: ToolContext = None,
) -> dict:
    """List previously requested reports.

    Args:
        report_type: Filter by report type (optional). If empty, lists all types.
        processing_status: Filter by status. One of: CANCELLED, DONE, FATAL, IN_PROGRESS, IN_QUEUE.
            Default: DONE.
        days: How far back to look (default 7, max 90).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    days = min(max(days, 1), 90)

    from sp_api.api import Reports
    reports = Reports(credentials=creds)

    kwargs = {
        "createdSince": datetime.now(timezone.utc) - timedelta(days=days),
        "pageSize": 100,
    }
    if report_type:
        kwargs["reportTypes"] = [report_type]
    if processing_status:
        kwargs["processingStatuses"] = [processing_status]

    result = await _call_with_retry(reports.get_reports, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    report_list = result["payload"].get("reports", [])
    summaries = []
    for r in report_list:
        summaries.append({
            "report_id": r.get("reportId"),
            "type": r.get("reportType"),
            "status": r.get("processingStatus"),
            "created": r.get("createdTime"),
            "document_id": r.get("reportDocumentId", ""),
        })

    return {"status": "success", "reports": summaries, "total": len(summaries)}
