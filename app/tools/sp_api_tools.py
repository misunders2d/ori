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
# PRODUCT FEES — fee estimate for a given ASIN + price (profitability math)
# ---------------------------------------------------------------------------

async def sp_get_fees_estimate(
    asin: str,
    price: float,
    is_fba: bool = True,
    shipping_price: float = 0.0,
    currency: str = "USD",
    tool_context: ToolContext = None,
) -> dict:
    """Estimate Amazon's fees for a given ASIN at a proposed sale price.

    Returns a fee breakdown: referral fee, FBA fulfillment fee (if is_fba),
    variable closing fee, per-item fee, and a total. Use this for profitability
    math and competitive pricing decisions.

    Args:
        asin: ASIN to estimate fees for.
        price: Proposed item (listing) price in the marketplace currency.
        is_fba: If True, estimate includes FBA fulfillment fees. If False,
            only the referral side of the fees is returned (merchant-fulfilled).
        shipping_price: Optional shipping price the buyer pays — only relevant
            for merchant-fulfilled listings, typically left at 0 for FBA.
        currency: ISO currency code, defaults to USD.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}
    if price <= 0:
        return {"status": "error", "message": "price must be > 0."}

    from sp_api.api import ProductFees
    fees = ProductFees(credentials=creds)

    kwargs = dict(
        asin=asin,
        price=price,
        currency=currency,
        is_fba=is_fba,
        marketplace_id=_US_MARKETPLACE,
    )
    if shipping_price:
        kwargs["shipping_price"] = shipping_price

    result = await _call_with_retry(fees.get_product_fees_estimate_for_asin, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    payload = result["payload"]
    # The payload wraps a FeesEstimateResult — pull out the structured bit if present.
    estimate_result = payload.get("FeesEstimateResult") if isinstance(payload, dict) else None
    estimate = (estimate_result or {}).get("FeesEstimate") or payload

    return {"status": "success", "asin": asin, "price": price, "is_fba": is_fba, "estimate": estimate}


# ---------------------------------------------------------------------------
# ORDERS — list recent orders, drill into items on a specific order
# ---------------------------------------------------------------------------

async def sp_list_orders(
    created_after: str = "",
    created_before: str = "",
    order_statuses: str = "",
    days: int = 7,
    max_results: int = 50,
    tool_context: ToolContext = None,
) -> dict:
    """List recent orders in the US marketplace.

    Either pass `created_after` (ISO 8601 timestamp or YYYY-MM-DD) explicitly,
    or leave it empty and use `days` for a rolling window ending now. Returns
    a compact list of order summaries — for full item-level detail, call
    sp_get_order_items with a specific order_id.

    Args:
        created_after: Lower bound on order creation time (ISO 8601 / YYYY-MM-DD).
            If empty, defaults to now - `days`.
        created_before: Upper bound on order creation time. If empty, no upper bound.
        order_statuses: Comma-separated list of statuses to filter by, e.g.
            "Shipped,Unshipped,Pending,Canceled". Empty = all statuses.
        days: Rolling window size when `created_after` is not provided (default 7).
        max_results: How many results to include in the response (1-100, default 50).
            Note: the API paginates — we return only the first page here to keep
            results tight. For larger pulls, use sp_list_orders on a narrower
            window or fall back to GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    # Resolve lower bound
    if not created_after:
        days = min(max(days, 1), 90)
        created_after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    from sp_api.api import OrdersV0
    orders = OrdersV0(credentials=creds)

    kwargs: dict = {
        "MarketplaceIds": [_US_MARKETPLACE],
        "CreatedAfter": created_after,
        "MaxResultsPerPage": min(max(max_results, 1), 100),
    }
    if created_before:
        kwargs["CreatedBefore"] = created_before
    if order_statuses:
        kwargs["OrderStatuses"] = [s.strip() for s in order_statuses.split(",") if s.strip()]

    result = await _call_with_retry(orders.get_orders, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    payload = result["payload"] or {}
    order_list = payload.get("Orders") or []
    summaries = []
    for o in order_list:
        total = o.get("OrderTotal") or {}
        summaries.append({
            "order_id": o.get("AmazonOrderId"),
            "purchase_date": o.get("PurchaseDate"),
            "status": o.get("OrderStatus"),
            "fulfillment_channel": o.get("FulfillmentChannel"),
            "items": o.get("NumberOfItemsShipped", 0) + o.get("NumberOfItemsUnshipped", 0),
            "total": total.get("Amount"),
            "currency": total.get("CurrencyCode"),
            "is_prime": o.get("IsPrime"),
            "is_business": o.get("IsBusinessOrder"),
        })

    return {
        "status": "success",
        "count": len(summaries),
        "orders": summaries,
        "has_more": bool(payload.get("NextToken")),
    }


async def sp_get_order_items(
    order_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Get line items for a specific order.

    Args:
        order_id: The Amazon order ID (e.g. 111-1234567-1234567) from sp_list_orders.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}
    if not order_id:
        return {"status": "error", "message": "order_id is required."}

    from sp_api.api import OrdersV0
    orders = OrdersV0(credentials=creds)
    result = await _call_with_retry(orders.get_order_items, order_id=order_id)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    payload = result["payload"] or {}
    items_raw = payload.get("OrderItems") or []
    items = []
    for item in items_raw:
        item_price = item.get("ItemPrice") or {}
        items.append({
            "asin": item.get("ASIN"),
            "sku": item.get("SellerSKU"),
            "title": item.get("Title"),
            "quantity_ordered": item.get("QuantityOrdered"),
            "quantity_shipped": item.get("QuantityShipped"),
            "item_price": item_price.get("Amount"),
            "currency": item_price.get("CurrencyCode"),
            "condition": item.get("ConditionId"),
        })

    return {"status": "success", "order_id": order_id, "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# FBA INVENTORY — live stock snapshot by marketplace
# ---------------------------------------------------------------------------

async def sp_get_inventory_summaries(
    skus: str = "",
    details: bool = True,
    tool_context: ToolContext = None,
) -> dict:
    """Get live FBA inventory summaries (fulfillable, inbound, reserved).

    Much faster than waiting on GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA for
    ad-hoc "how much do we have in stock?" questions. Filter by SKUs when you
    know which ones you care about — unfiltered calls paginate heavily.

    Args:
        skus: Optional comma-separated seller SKUs to filter by. If empty,
            returns a page of all inventory (capped by Amazon's page size).
        details: If True, returns detailed availability breakdown
            (inboundWorking, inboundShipped, inboundReceiving, fulfillable,
            reservedQuantity, researchingQuantity, unfulfillableQuantity).
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    from sp_api.api import Inventories
    inv = Inventories(credentials=creds)

    kwargs: dict = {
        "granularityType": "Marketplace",
        "granularityId": _US_MARKETPLACE,
        "marketplaceIds": [_US_MARKETPLACE],
        "details": details,
    }
    if skus:
        kwargs["sellerSkus"] = [s.strip() for s in skus.split(",") if s.strip()]

    result = await _call_with_retry(inv.get_inventory_summary_marketplace, **kwargs)
    if not result["ok"]:
        return {"status": "error", "message": result["error"]}

    payload = result["payload"] or {}
    summaries_raw = payload.get("inventorySummaries") or []
    summaries = []
    for s in summaries_raw:
        detail = s.get("inventoryDetails") or {}
        summaries.append({
            "asin": s.get("asin"),
            "sku": s.get("sellerSku"),
            "fn_sku": s.get("fnSku"),
            "condition": s.get("condition"),
            "total": s.get("totalQuantity"),
            "fulfillable": detail.get("fulfillableQuantity"),
            "inbound_working": (detail.get("inboundWorkingQuantity")
                                 if detail else None),
            "inbound_shipped": (detail.get("inboundShippedQuantity")
                                 if detail else None),
            "inbound_receiving": (detail.get("inboundReceivingQuantity")
                                   if detail else None),
            "reserved": (detail.get("reservedQuantity") or {}).get("totalReservedQuantity"),
            "unfulfillable": (detail.get("unfulfillableQuantity") or {}).get("totalUnfulfillableQuantity"),
        })

    return {
        "status": "success",
        "count": len(summaries),
        "items": summaries,
        "has_more": bool(payload.get("nextToken")),
    }


# ---------------------------------------------------------------------------
# REPORTS
# ---------------------------------------------------------------------------

async def sp_request_report(
    report_type: str,
    days: int = 30,
    report_options: str = "{}",
    tool_context: ToolContext = None,
) -> dict:
    """Submit an Amazon report request. **Long-running** — returns a
    report_id; the actual data is NOT ready when this call returns.

    ⚠ Reports take 30 seconds to 15+ minutes on Amazon's side. The tool
    returns immediately with ``{status: "success", report_id: "..."}``;
    the agent fire ends right after that response. There is NO automatic
    push from Amazon when the report finishes.

    Therefore, after calling this tool, BEFORE you respond to the user
    with any "I'll get back to you" / "will update once ready" language,
    you MUST call ``schedule_one_off_task`` to wire a self-check that
    polls ``sp_check_report`` and posts the result to the user's channel
    when it flips to ``DONE``. Without that scheduled follow-up the user
    will never get the report.

    The ``pending_followup_guard`` after-tool callback annotates the
    response with a ``__followup_required__`` block and a
    ``suggested_steps`` template — use those as the ``steps=`` list for
    your scheduled task. The contract pipeline (``docs/CONTRACTS.md``)
    is the long-term home for recurring polled-reports; this skill-rule
    mechanism covers ad-hoc one-shots.

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
        "data_start_time": payload.get("dataStartTime"),
        "data_end_time": payload.get("dataEndTime"),
        "marketplace_ids": payload.get("marketplaceIds"),
        "report_options": payload.get("reportOptions"),
    }

    if payload.get("processingStatus") == "DONE":
        response["report_document_id"] = payload.get("reportDocumentId")
        response["message"] = f"Report ready. Use sp_download_report(report_document_id='{payload.get('reportDocumentId')}') to download."
    elif payload.get("processingStatus") in ("IN_PROGRESS", "IN_QUEUE"):
        response["message"] = "Report still processing. Check again in 30-60 seconds."
    elif payload.get("processingStatus") in ("CANCELLED", "FATAL"):
        response["raw_report"] = payload
        response["message"] = (
            f"Report ended with status {payload.get('processingStatus')}. "
            "Raw report metadata is included in raw_report. If it contains no "
            "error detail, Amazon did not expose a specific failure reason via get_report."
        )
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


async def sp_get_account_health(
    days: int = 30,
    max_wait_seconds: int = 90,
    tool_context: ToolContext = None,
) -> dict:
    """Fetch the Seller Central Account Health digest — AHR, policy compliance,
    product authenticity, shipping performance, and other performance metrics.

    Runs the full pipeline for GET_V2_SELLER_PERFORMANCE_REPORT
    (request → poll → download → parse) and returns a compact summary: the
    overall account status plus any metrics that are not in GOOD standing.

    If the report is still processing after max_wait_seconds, returns the
    report_id so you can check later with sp_check_report.

    Args:
        days: Performance window in days (default 30, max 90).
        max_wait_seconds: How long to poll before giving up and returning the
            report_id (default 90). Account health reports are typically small
            and complete within a minute.
    """
    creds = _get_credentials()
    if not creds:
        return {"status": "error", "message": "SP-API credentials not configured."}

    # 1. Request
    req = await sp_request_report(
        report_type="GET_V2_SELLER_PERFORMANCE_REPORT",
        days=days,
        tool_context=tool_context,
    )
    if req.get("status") != "success":
        return req
    report_id = req.get("report_id", "")
    if not report_id:
        return {"status": "error", "message": "Request succeeded but no report_id returned."}

    # 2. Poll — short poll with linear backoff
    document_id = ""
    waited = 0
    poll_interval = 5
    while waited < max_wait_seconds:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        check = await sp_check_report(report_id=report_id, tool_context=tool_context)
        proc = check.get("processing_status")
        if proc == "DONE":
            document_id = check.get("report_document_id", "")
            break
        if proc in ("CANCELLED", "FATAL"):
            return {
                "status": "error",
                "message": f"Account health report {report_id} ended with status {proc}.",
            }
        poll_interval = min(poll_interval + 2, 15)

    if not document_id:
        return {
            "status": "pending",
            "report_id": report_id,
            "message": (
                f"Account health report still processing after {max_wait_seconds}s. "
                f"Check later with sp_check_report(report_id='{report_id}')."
            ),
        }

    # 3. Download
    download = await sp_download_report(
        report_document_id=document_id, tool_context=tool_context
    )
    if download.get("status") != "success":
        return download

    raw = download.get("data") or download.get("preview") or ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Account health report downloaded but could not be parsed as JSON.",
            "raw_preview": str(raw)[:500],
        }

    # 4. Digest — surface overall status and any non-GOOD metrics
    digest = _summarize_account_health(data)
    digest["report_id"] = report_id
    return digest


def _summarize_account_health(data: dict) -> dict:
    """Walk a GET_V2_SELLER_PERFORMANCE_REPORT payload and return a compact digest.

    The payload has a performanceMetrics object with per-section status. Any
    section whose status is not GOOD is surfaced in the digest so the agent
    can explain what's wrong without flooding the context with the full report.
    """
    if not isinstance(data, dict):
        return {"status": "success", "summary": "Report payload was not a dict.", "raw": data}

    overall = data.get("accountStatus") or data.get("status") or "UNKNOWN"
    metrics = data.get("performanceMetrics") or {}
    issues: list[dict] = []
    good_sections: list[str] = []

    # performanceMetrics is a nested dict of named sections. Each section usually
    # has a "status" key at its root. We walk one level deep.
    for section_name, section in metrics.items():
        if not isinstance(section, dict):
            continue
        section_status = section.get("status") or section.get("accountHealthStatus")
        if section_status and section_status != "GOOD":
            issues.append({
                "section": section_name,
                "status": section_status,
                "details": {k: v for k, v in section.items() if k != "status"},
            })
        elif section_status == "GOOD":
            good_sections.append(section_name)

    return {
        "status": "success",
        "overall_status": overall,
        "healthy_sections": good_sections,
        "issues": issues,
        "issue_count": len(issues),
        "message": (
            f"Account status: {overall}. {len(issues)} non-GOOD section(s)."
            if issues else f"Account status: {overall}. All tracked sections GOOD."
        ),
    }


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
