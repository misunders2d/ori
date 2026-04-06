"""Keepa API tools for Amazon product research.

All operations use httpx + Keepa API. Auth via KEEPA_API_KEY
from vault/os.environ.
"""

import logging
import os
import json
from typing import Optional, List, Dict, Any

import httpx
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_API_BASE = "https://api.keepa.com"


def _get_api_key() -> str:
    """Retrieve Keepa API key from environment."""
    return os.environ.get("KEEPA_API_KEY", "")


def get_latest_price_from_csv(csv: Optional[List[int]], items_per_row: int) -> Optional[float]:
    """Helper to extract the latest valid price from a Keepa CSV array."""
    if not csv or len(csv) < items_per_row:
        return None
    # Keepa CSVs are flattened arrays of [time, price, ...]
    # We iterate backwards to find the latest non-negative price
    for i in range(len(csv) - items_per_row, -1, -items_per_row):
        p = csv[i + 1]
        if p > 0:
            return p / 100.0
    return None


async def keepa_get_product_data(
    asin: str,
    domain: int = 1,
    stats: Optional[int] = None,
    update: Optional[int] = None,
    offers: Optional[int] = 1,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves detailed product data from Keepa, including real selling price analysis.

    Args:
        asin: Amazon Standard Identification Number.
        domain: Amazon locale (1: US, 2: GB, 3: DE, 4: FR, 5: JP, 6: CA, 7: CN, 8: IT, 9: ES, 10: IN, 11: MX).
        stats: If set, returns statistics for the specified period (days).
        update: If set (0 to 24), forces an update of the product data if older than X hours.
        offers: If set to 1, retrieves live offer data including Prime Exclusive discounts.

    Returns:
        dict: Product data and pricing analysis.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {
        "key": api_key,
        "asin": asin,
        "domain": domain,
    }
    if stats is not None:
        params["stats"] = stats
    if update is not None:
        params["update"] = update
    if offers is not None:
        params["offers"] = offers

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{_API_BASE}/product", params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("products"):
                return {"status": "error", "message": "No product data found for ASIN."}

            product = data["products"][0]
            
            # Pricing Analysis
            pricing = {}
            csv_data = product.get("csv", [])
            
            # Extract standard prices from history CSV
            # Indices: 0: Amazon, 1: New, 18: Buy Box, 33: Prime Exclusive
            amazon_price = None
            new_price = None
            buy_box_price = None
            prime_excl_hist_price = None
            
            if csv_data:
                # Keepa CSV indices for the 'csv' object:
                # 0: AMAZON, 1: NEW, 18: BUY_BOX, 33: PRIME_EXCLUSIVE
                if len(csv_data) > 0: amazon_price = get_latest_price_from_csv(csv_data[0], 2)
                if len(csv_data) > 1: new_price = get_latest_price_from_csv(csv_data[1], 2)
                if len(csv_data) > 18: buy_box_price = get_latest_price_from_csv(csv_data[18], 2)
                if len(csv_data) > 33: prime_excl_hist_price = get_latest_price_from_csv(csv_data[33], 2)

            pricing["amazon_price"] = amazon_price
            pricing["new_price"] = new_price
            pricing["buy_box_price"] = buy_box_price
            pricing["prime_excl_hist_price"] = prime_excl_hist_price

            # Extract Prime Exclusive from live offers if available
            prime_exclusive_price = None
            live_offers = product.get("offers", [])
            if live_offers:
                pe_prices = []
                for o in live_offers:
                    if o.get("isPrimeExcl"):
                        p = get_latest_price_from_csv(o.get("primeExclCSV"), 2)
                        if not p:
                            p = get_latest_price_from_csv(o.get("offerCSV"), 3)
                        if p:
                            pe_prices.append(p)
                if pe_prices:
                    prime_exclusive_price = min(pe_prices)
            
            pricing["prime_exclusive_price"] = prime_exclusive_price

            # Check for Coupons
            coupon = product.get("coupon")
            pricing["coupon"] = None
            if coupon:
                if coupon > 0:
                    pricing["coupon"] = {"type": "absolute", "value": coupon / 100.0}
                elif coupon < 0:
                    pricing["coupon"] = {"type": "percentage", "value": abs(coupon)}

            # Best Current Offer Logic
            valid_prices = [p for p in [amazon_price, new_price, buy_box_price, prime_excl_hist_price, prime_exclusive_price] if p is not None]
            
            best_offer = min(valid_prices) if valid_prices else None
            
            # Apply coupon if it's absolute
            if best_offer and pricing["coupon"]:
                if pricing["coupon"]["type"] == "absolute":
                    best_offer -= pricing["coupon"]["value"]
                elif pricing["coupon"]["type"] == "percentage":
                    best_offer *= (1 - pricing["coupon"]["value"] / 100.0)

            pricing["best_current_offer"] = round(best_offer, 2) if best_offer is not None else None

            return {
                "status": "success",
                "asin": asin,
                "title": product.get("title"),
                "brand": product.get("brand"),
                "category_tree": product.get("categoryTree"),
                "pricing": pricing,
                "raw_product": {k: v for k, v in product.items() if k not in ["csv", "offers"]} # Exclude heavy fields from raw
            }

    except Exception as e:
        logger.exception("Keepa API error in keepa_get_product_data")
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_product_finder(
    selection: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Searches for products using Keepa's finder with extensive filters.

    Args:
        selection: A JSON string containing the selection criteria (e.g., '{"sort": [["current", "asc"]], "price_new": [1000, 5000]}').
                  See Keepa API documentation for full parameter list.

    Returns:
        dict: List of matching ASINs and metadata.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    try:
        criteria = json.loads(selection)
        params = {"key": api_key, "selection": json.dumps(criteria)}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_API_BASE}/finder", params=params)
            resp.raise_for_status()
            return {"status": "success", "data": resp.json()}
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON format for selection."}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_get_categories(
    domain: int,
    category: Optional[int] = None,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves Keepa's category tree or details for a specific category.

    Args:
        domain: Amazon locale (1-11).
        category: Optional category ID. If provided, returns details for that category. 
                  If omitted, returns the root categories.

    Returns:
        dict: Category data.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {"key": api_key, "domain": domain}
    if category is not None:
        params["category"] = category

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_API_BASE}/category", params=params)
            resp.raise_for_status()
            return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_get_bestsellers(
    domain: int,
    category: int,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves the bestseller list for a specific category.

    Args:
        domain: Amazon locale (1-11).
        category: The category ID.

    Returns:
        dict: Bestseller list.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {"key": api_key, "domain": domain, "category": category}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_API_BASE}/bestsellers", params=params)
            resp.raise_for_status()
            return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_get_seller_info(
    domain: int,
    seller_id: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves detailed information about a specific Amazon seller.

    Args:
        domain: Amazon locale (1-11).
        seller_id: The Amazon Seller ID.

    Returns:
        dict: Seller information.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {"key": api_key, "domain": domain, "seller": seller_id}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_API_BASE}/seller", params=params)
            resp.raise_for_status()
            return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_get_top_sellers(
    domain: int,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves the list of top sellers for a given Amazon locale.

    Args:
        domain: Amazon locale (1-11).

    Returns:
        dict: Top sellers list.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {"key": api_key, "domain": domain}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_API_BASE}/topseller", params=params)
            resp.raise_for_status()
            return {"status": "success", "data": resp.json()}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}
