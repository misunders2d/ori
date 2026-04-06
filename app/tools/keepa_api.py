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

# Minimum token balance required to proceed with a request
_MIN_TOKENS = 5


def _get_api_key() -> str:
    """Retrieve Keepa API key from environment."""
    return os.environ.get("KEEPA_API_KEY", "")


async def _check_token_balance(client: httpx.AsyncClient, api_key: str) -> dict:
    """Check remaining Keepa API tokens. Costs 0 tokens.

    Returns dict with tokensLeft, refillRate, refillIn or error.
    """
    try:
        resp = await client.get(f"{_API_BASE}/token", params={"key": api_key})
        resp.raise_for_status()
        data = resp.json()
        return {
            "tokensLeft": data.get("tokensLeft", 0),
            "refillRate": data.get("refillRate", 0),
            "refillIn": data.get("refillIn", 0),
        }
    except Exception as e:
        return {"tokensLeft": -1, "error": str(e)}


def _check_tokens_or_error(token_info: dict) -> dict | None:
    """Return an error dict if token balance is too low, otherwise None."""
    tokens = token_info.get("tokensLeft", 0)
    if tokens == -1:
        return {"status": "error", "message": f"Could not check Keepa token balance: {token_info.get('error')}"}
    if tokens < _MIN_TOKENS:
        refill_in = token_info.get("refillIn", 0)
        refill_secs = refill_in // 1000 if refill_in else 0
        return {
            "status": "error",
            "message": f"Keepa API token budget exhausted ({tokens} tokens remaining, need {_MIN_TOKENS}). Tokens refill in ~{refill_secs}s at {token_info.get('refillRate', '?')}/min.",
        }
    return None


def get_latest_price_from_csv(csv: Optional[List[int]], items_per_row: int) -> Optional[float]:
    """Helper to extract the current price from a Keepa CSV array.

    Only checks the most recent entry. Returns None if the latest price
    is -1 (not available / no longer active) or missing.
    """
    if not csv or len(csv) < items_per_row:
        return None
    # Keepa CSVs are flattened: [time, price, ...], [time, price, ...], ...
    # Only the last entry reflects the current state
    last_price = csv[-items_per_row + 1]
    if last_price > 0:
        return last_price / 100.0
    return None


async def keepa_check_tokens(
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Check remaining Keepa API token balance. Costs 0 tokens.

    Returns:
        dict: Token balance, refill rate, and time until next refill.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    async with httpx.AsyncClient(timeout=10) as client:
        info = await _check_token_balance(client, api_key)
        if info.get("tokensLeft", 0) == -1:
            return {"status": "error", "message": f"Failed to check tokens: {info.get('error')}"}
        return {
            "status": "success",
            "tokens_left": info["tokensLeft"],
            "refill_rate_per_min": info["refillRate"],
            "refill_in_ms": info["refillIn"],
        }


async def keepa_get_product_data(
    asin: str,
    domain: int = 1,
    stats: Optional[int] = None,
    update: Optional[int] = None,
    offers: int = 1,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieves detailed product data from Keepa, including real selling price analysis.

    Args:
        asin: Amazon Standard Identification Number.
        domain: Amazon locale (1: US, 2: GB, 3: DE, 4: FR, 5: JP, 6: CA, 7: CN, 8: IT, 9: ES, 10: IN, 11: MX).
        stats: If set, returns statistics for the specified period (days).
        update: If set (0 to 24), forces an update of the product data if older than X hours.
        offers: Number of offer pages to retrieve (0-3). Each page has up to 10 offers and costs 6 extra tokens. Default 1. Do NOT set higher than 3.

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
        params["offers"] = min(int(offers), 3)  # Hard cap to prevent token/context explosion

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Check token balance first (0 cost)
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/product", params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("products"):
                return {
                    "status": "error",
                    "message": "No product data found for ASIN.",
                    "tokens_left": data.get("tokensLeft"),
                    "raw_response_keys": list(data.keys()),
                    "error_detail": data.get("error"),
                }

            product = data["products"][0]

            # Pricing Analysis
            pricing = {}
            csv_data = product.get("csv", [])

            # Extract standard prices from history CSV
            # Indices: 0: AMAZON, 1: NEW, 18: BUY_BOX_SHIPPING, 33: PRIME_EXCL
            amazon_price = None
            new_price = None
            buy_box_price = None
            prime_excl_hist_price = None

            if csv_data:
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
            # Coupon applies to buy box price only (it's the listed discount on the product page)
            candidates = {
                "amazon": amazon_price,
                "new": new_price,
                "buy_box": buy_box_price,
                "prime_excl_hist": prime_excl_hist_price,
                "prime_exclusive": prime_exclusive_price,
            }

            # Apply coupon to buy box price before comparison
            if buy_box_price is not None and pricing["coupon"]:
                if pricing["coupon"]["type"] == "absolute":
                    candidates["buy_box_with_coupon"] = buy_box_price - pricing["coupon"]["value"]
                elif pricing["coupon"]["type"] == "percentage":
                    candidates["buy_box_with_coupon"] = buy_box_price * (1 - pricing["coupon"]["value"] / 100.0)

            valid = {k: v for k, v in candidates.items() if v is not None}
            if valid:
                best_source = min(valid, key=valid.get)
                best_offer = round(valid[best_source], 2)
            else:
                best_source = None
                best_offer = None

            pricing["best_current_offer"] = best_offer
            pricing["best_offer_source"] = best_source

            return {
                "status": "success",
                "asin": asin,
                "title": product.get("title"),
                "brand": product.get("brand"),
                "category_tree": product.get("categoryTree"),
                "pricing": pricing,
                "tokens_left": data.get("tokensLeft"),
                "raw_product": {k: v for k, v in product.items() if k not in ["csv", "offers"]}
            }

    except Exception as e:
        logger.exception("Keepa API error in keepa_get_product_data")
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}


async def keepa_product_finder(
    selection: str,
    domain: int = 1,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Searches for products using Keepa's Product Finder with extensive filters.

    Args:
        selection: A JSON string containing the selection criteria (e.g., '{"sort": [["current", "asc"]], "current_NEW_FBA_GTE": 1000, "current_NEW_FBA_LTE": 5000}').
                  See Keepa API documentation for full parameter list.
        domain: Amazon locale (1-11). Default: 1 (US).

    Returns:
        dict: List of matching ASINs and metadata.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    try:
        criteria = json.loads(selection)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON format for selection."}

    params = {
        "key": api_key,
        "domain": domain,
        "selection": json.dumps(criteria),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/query", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "success",
                "tokens_left": data.get("tokensLeft"),
                "data": data,
            }
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
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/category", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "tokens_left": data.get("tokensLeft"), "data": data}
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
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/bestsellers", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "tokens_left": data.get("tokensLeft"), "data": data}
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
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/seller", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "tokens_left": data.get("tokensLeft"), "data": data}
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
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/topseller", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "tokens_left": data.get("tokensLeft"), "data": data}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {str(e)}"}
