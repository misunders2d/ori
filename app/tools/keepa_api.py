"""Keepa API tools — fetch-store-extract architecture.

Raw Keepa responses are stored in data/keepa_cache/{asin}.json.
Only lightweight summaries go to the LLM. Focused extraction tools
pull specific slices from the cached data on demand.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_API_BASE = "https://api.keepa.com"
_CACHE_DIR = os.path.abspath("./tmp/keepa_cache")
_CACHE_TTL = 3600  # 1 hour
_MIN_TOKENS = 5

# Keepa time epoch: Jan 1, 2011 00:00 UTC (in minutes)
_KEEPA_EPOCH = 1293840000  # Unix timestamp


def _keepa_time_to_datetime(keepa_minutes: int) -> str:
    """Convert Keepa time (minutes since epoch) to ISO datetime string."""
    unix_ts = _KEEPA_EPOCH + keepa_minutes * 60
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get_api_key() -> str:
    return os.environ.get("KEEPA_API_KEY", "")


def _cache_path(asin: str) -> str:
    return os.path.join(_CACHE_DIR, f"{asin.upper()}.json")


def _load_cached(asin: str) -> dict | None:
    """Load cached product data if fresh enough."""
    path = _cache_path(asin)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            cached = json.load(f)
        if time.time() - cached.get("_cached_at", 0) < _CACHE_TTL:
            return cached
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(asin: str, product: dict, tokens_left: int):
    """Save raw product data to cache."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    product["_cached_at"] = time.time()
    product["_tokens_left"] = tokens_left
    with open(_cache_path(asin), "w") as f:
        json.dump(product, f)


def _price_from_csv(csv: list | None, items_per_row: int) -> float | None:
    """Extract current price from a Keepa CSV array. Returns None if inactive (-1)."""
    if not csv or len(csv) < items_per_row:
        return None
    last_price = csv[-items_per_row + 1]
    return last_price / 100.0 if last_price > 0 else None


def _history_from_csv(csv: list | None, items_per_row: int, days: int = 90) -> list[dict]:
    """Extract price history from CSV as [{date, price}] for the last N days."""
    if not csv or len(csv) < items_per_row:
        return []
    cutoff = time.time() - days * 86400
    result = []
    for i in range(0, len(csv) - items_per_row + 1, items_per_row):
        keepa_min = csv[i]
        price_raw = csv[i + 1]
        unix_ts = _KEEPA_EPOCH + keepa_min * 60
        if unix_ts < cutoff:
            continue
        dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        price = price_raw / 100.0 if price_raw > 0 else None
        result.append({"date": dt, "price": price})
    return result


async def _check_token_balance(client: httpx.AsyncClient, api_key: str) -> dict:
    try:
        resp = await client.get(f"{_API_BASE}/token", params={"key": api_key})
        resp.raise_for_status()
        data = resp.json()
        return {"tokensLeft": data.get("tokensLeft", 0), "refillRate": data.get("refillRate", 0), "refillIn": data.get("refillIn", 0)}
    except Exception as e:
        return {"tokensLeft": -1, "error": str(e)}


def _check_tokens_or_error(token_info: dict) -> dict | None:
    tokens = token_info.get("tokensLeft", 0)
    if tokens == -1:
        return {"status": "error", "message": f"Could not check Keepa token balance: {token_info.get('error')}"}
    if tokens < _MIN_TOKENS:
        refill_secs = token_info.get("refillIn", 0) // 1000
        return {"status": "error", "message": f"Keepa token budget exhausted ({tokens} left, need {_MIN_TOKENS}). Refill in ~{refill_secs}s."}
    return None


# ---------------------------------------------------------------------------
# FETCH tools — call Keepa API, store raw data, return lightweight summary
# ---------------------------------------------------------------------------

async def keepa_check_tokens(tool_context: Optional[ToolContext] = None) -> dict:
    """Check remaining Keepa API token balance. Costs 0 tokens."""
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}
    async with httpx.AsyncClient(timeout=10) as client:
        info = await _check_token_balance(client, api_key)
        if info.get("tokensLeft", 0) == -1:
            return {"status": "error", "message": f"Failed: {info.get('error')}"}
        return {"status": "success", "tokens_left": info["tokensLeft"], "refill_rate_per_min": info["refillRate"]}


async def keepa_fetch_product(
    asin: str,
    domain: int = 1,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Fetch product data from Keepa and cache it locally. Returns a lightweight summary only.

    Call this first for any ASIN, then use keepa_extract_* tools to get specific data.
    Uses cache if data is less than 1 hour old.

    Args:
        asin: Amazon Standard Identification Number.
        domain: Amazon locale (1: US, 2: GB, 3: DE, 4: FR, 5: JP, 6: CA, 7: CN, 8: IT, 9: ES, 10: IN, 11: MX).

    Returns:
        dict: Lightweight summary (title, brand, category, current prices). Full data is cached for extraction tools.
    """
    asin = asin.strip().upper()

    # Check cache first
    cached = _load_cached(asin)
    if cached:
        return _build_summary(asin, cached, from_cache=True)

    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}

    params = {"key": api_key, "asin": asin, "domain": domain, "offers": 20}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err

            resp = await client.get(f"{_API_BASE}/product", params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("products"):
                return {"status": "error", "message": "No product data found for ASIN.", "error_detail": data.get("error")}

            product = data["products"][0]
            tokens_left = data.get("tokensLeft", 0)
            _save_cache(asin, product, tokens_left)
            return _build_summary(asin, product, from_cache=False, tokens_left=tokens_left)

    except Exception as e:
        logger.exception("Keepa fetch error for %s", asin)
        return {"status": "error", "message": f"Keepa API error: {e}"}


def _build_summary(asin: str, product: dict, from_cache: bool = False, tokens_left: int = None) -> dict:
    """Build a lightweight summary from cached product data."""
    csv_data = product.get("csv", [])

    # Current prices
    amazon = _price_from_csv(csv_data[0], 2) if len(csv_data) > 0 else None
    new = _price_from_csv(csv_data[1], 2) if len(csv_data) > 1 else None
    buy_box = _price_from_csv(csv_data[18], 2) if len(csv_data) > 18 else None
    prime_excl = _price_from_csv(csv_data[33], 2) if len(csv_data) > 33 else None

    # Coupon
    coupon_raw = product.get("coupon")
    coupon = None
    if coupon_raw:
        if isinstance(coupon_raw, list):
            coupon_raw = coupon_raw[0] if coupon_raw else None
        if coupon_raw and coupon_raw > 0:
            coupon = f"${coupon_raw / 100:.2f} off"
        elif coupon_raw and coupon_raw < 0:
            coupon = f"{abs(coupon_raw)}% off"

    result = {
        "status": "success",
        "asin": asin,
        "title": product.get("title"),
        "brand": product.get("brand"),
        "category": " > ".join(c.get("name", "") for c in product.get("categoryTree", [])),
        "current_prices": {
            "amazon": amazon,
            "new_3p": new,
            "buy_box": buy_box,
            "prime_exclusive": prime_excl,
        },
        "coupon": coupon,
        "monthly_sold": product.get("monthlySold"),
        "from_cache": from_cache,
        "hint": "Use keepa_extract_* tools for detailed pricing history, offers, competitors, and stats.",
    }
    if tokens_left is not None:
        result["tokens_left"] = tokens_left
    return result


# ---------------------------------------------------------------------------
# EXTRACT tools — read from cache, return focused slices
# ---------------------------------------------------------------------------

def keepa_extract_pricing(asin: str, tool_context: Optional[ToolContext] = None) -> dict:
    """Extract detailed current pricing from cached Keepa data.

    Returns all price types, coupon details, and best current offer analysis.
    Call keepa_fetch_product first.

    Args:
        asin: The ASIN to extract pricing for (must be fetched first).
    """
    product = _load_cached(asin.strip().upper())
    if not product:
        return {"status": "error", "message": f"No cached data for {asin}. Call keepa_fetch_product first."}

    csv_data = product.get("csv", [])

    prices = {}
    _CSV_MAP = {
        "amazon": (0, 2), "new_3p": (1, 2), "used": (2, 2),
        "list_price": (4, 2), "warehouse": (9, 2), "new_fba": (10, 2),
        "buy_box": (18, 2), "prime_exclusive": (33, 2),
    }
    for name, (idx, row_size) in _CSV_MAP.items():
        prices[name] = _price_from_csv(csv_data[idx], row_size) if len(csv_data) > idx else None

    # Live offers — Prime Exclusive
    pe_price = None
    offers = product.get("offers", [])
    if offers:
        pe_prices = []
        for o in offers:
            if o.get("isPrimeExcl"):
                p = _price_from_csv(o.get("primeExclCSV"), 2)
                if not p:
                    p = _price_from_csv(o.get("offerCSV"), 3)
                if p:
                    pe_prices.append(p)
        if pe_prices:
            pe_price = min(pe_prices)
    prices["prime_exclusive_live"] = pe_price

    # Coupon
    coupon_raw = product.get("coupon")
    coupon = None
    if coupon_raw:
        if isinstance(coupon_raw, list):
            coupon_raw = coupon_raw[0] if coupon_raw else None
        if coupon_raw and coupon_raw > 0:
            coupon = {"type": "absolute", "value": coupon_raw / 100.0}
        elif coupon_raw and coupon_raw < 0:
            coupon = {"type": "percentage", "value": abs(coupon_raw)}

    # Best offer
    candidates = {k: v for k, v in prices.items() if v is not None}
    if coupon and prices.get("buy_box") is not None:
        if coupon["type"] == "absolute":
            candidates["buy_box_with_coupon"] = prices["buy_box"] - coupon["value"]
        else:
            candidates["buy_box_with_coupon"] = prices["buy_box"] * (1 - coupon["value"] / 100.0)

    valid = {k: v for k, v in candidates.items() if v is not None}
    best_source = min(valid, key=valid.get) if valid else None
    best_price = round(valid[best_source], 2) if best_source else None

    return {
        "status": "success",
        "asin": asin.upper(),
        "prices": prices,
        "coupon": coupon,
        "best_offer": best_price,
        "best_offer_source": best_source,
    }


def keepa_extract_history(
    asin: str,
    metric: str = "buy_box",
    days: int = 90,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Extract price or rank history from cached Keepa data.

    Args:
        asin: The ASIN (must be fetched first).
        metric: One of: amazon, new, used, sales_rank, buy_box, new_fba, prime_exclusive, rating, review_count, list_price.
        days: Number of days of history to return (default 90).
    """
    product = _load_cached(asin.strip().upper())
    if not product:
        return {"status": "error", "message": f"No cached data for {asin}. Call keepa_fetch_product first."}

    _METRIC_MAP = {
        "amazon": (0, 2), "new": (1, 2), "used": (2, 2),
        "sales_rank": (3, 2), "list_price": (4, 2),
        "new_fba": (10, 2), "buy_box": (18, 2),
        "prime_exclusive": (33, 2),
        "rating": (16, 2), "review_count": (17, 2),
    }
    if metric not in _METRIC_MAP:
        return {"status": "error", "message": f"Unknown metric '{metric}'. Options: {', '.join(_METRIC_MAP.keys())}"}

    idx, row_size = _METRIC_MAP[metric]
    csv_data = product.get("csv", [])
    if len(csv_data) <= idx or not csv_data[idx]:
        return {"status": "success", "asin": asin.upper(), "metric": metric, "history": [], "message": "No data available for this metric."}

    history = _history_from_csv(csv_data[idx], row_size, days)

    # For rating, divide by 10 (Keepa stores as 0-50, meaning 0.0-5.0)
    if metric == "rating":
        for h in history:
            if h["price"] is not None:
                h["rating"] = h.pop("price") / 10.0
            else:
                h["rating"] = h.pop("price")

    return {"status": "success", "asin": asin.upper(), "metric": metric, "days": days, "data_points": len(history), "history": history}


def keepa_extract_offers(asin: str, tool_context: Optional[ToolContext] = None) -> dict:
    """Extract current seller/offer information from cached Keepa data.

    Returns buy box holder, FBA vs FBM breakdown, seller count, and top offers.
    Call keepa_fetch_product first.

    Args:
        asin: The ASIN (must be fetched first).
    """
    product = _load_cached(asin.strip().upper())
    if not product:
        return {"status": "error", "message": f"No cached data for {asin}. Call keepa_fetch_product first."}

    csv_data = product.get("csv", [])

    # Offer counts from CSV
    count_new = _price_from_csv(csv_data[11], 2) if len(csv_data) > 11 else None
    count_used = _price_from_csv(csv_data[12], 2) if len(csv_data) > 12 else None
    count_new_fba = _price_from_csv(csv_data[34], 2) if len(csv_data) > 34 else None
    count_new_fbm = _price_from_csv(csv_data[35], 2) if len(csv_data) > 35 else None

    # Buy box seller history (last entry)
    bb_history = product.get("buyBoxSellerIdHistory", [])
    current_bb_seller = None
    if bb_history and len(bb_history) >= 2:
        current_bb_seller = bb_history[-1]  # Last seller ID

    # Summarize live offers
    offers = product.get("offers", [])
    offer_summary = []
    for o in offers[:10]:  # Cap at 10
        offer_summary.append({
            "seller_id": o.get("sellerId"),
            "is_fba": o.get("isFBA"),
            "is_prime": o.get("isPrime"),
            "is_prime_excl": o.get("isPrimeExcl"),
            "condition": o.get("condition"),
            "price": _price_from_csv(o.get("offerCSV"), 3),
        })

    return {
        "status": "success",
        "asin": asin.upper(),
        "offer_counts": {
            "new_total": int(count_new) if count_new else None,
            "used_total": int(count_used) if count_used else None,
            "new_fba": int(count_new_fba) if count_new_fba else None,
            "new_fbm": int(count_new_fbm) if count_new_fbm else None,
        },
        "buy_box_seller": current_bb_seller,
        "live_offers": offer_summary,
    }


def keepa_extract_stats(asin: str, tool_context: Optional[ToolContext] = None) -> dict:
    """Extract key product stats from cached Keepa data.

    Returns sales rank, monthly sold, rating, review count, availability, listing age.
    Call keepa_fetch_product first.

    Args:
        asin: The ASIN (must be fetched first).
    """
    product = _load_cached(asin.strip().upper())
    if not product:
        return {"status": "error", "message": f"No cached data for {asin}. Call keepa_fetch_product first."}

    csv_data = product.get("csv", [])

    # Current values from CSV
    sales_rank = _price_from_csv(csv_data[3], 2) if len(csv_data) > 3 else None
    rating_raw = _price_from_csv(csv_data[16], 2) if len(csv_data) > 16 else None
    review_count = _price_from_csv(csv_data[17], 2) if len(csv_data) > 17 else None

    listed_since = product.get("listedSince")
    tracking_since = product.get("trackingSince")

    return {
        "status": "success",
        "asin": asin.upper(),
        "sales_rank": int(sales_rank) if sales_rank else None,
        "sales_rank_category": product.get("salesRankReference"),
        "monthly_sold": product.get("monthlySold"),
        "rating": rating_raw / 10.0 if rating_raw else None,
        "review_count": int(review_count) if review_count else None,
        "availability_amazon": product.get("availabilityAmazon"),
        "is_sns": product.get("isSNS"),
        "listed_since": _keepa_time_to_datetime(listed_since) if listed_since and listed_since > 0 else None,
        "tracking_since": _keepa_time_to_datetime(tracking_since) if tracking_since and tracking_since > 0 else None,
        "fba_fees": product.get("fbaFees"),
    }


def keepa_extract_competitors(asin: str, tool_context: Optional[ToolContext] = None) -> dict:
    """Extract competitor/seller information from cached Keepa data.

    Returns unique seller IDs from buy box history and current offers.
    Call keepa_fetch_product first.

    Args:
        asin: The ASIN (must be fetched first).
    """
    product = _load_cached(asin.strip().upper())
    if not product:
        return {"status": "error", "message": f"No cached data for {asin}. Call keepa_fetch_product first."}

    # Buy box seller history — extract unique sellers
    bb_history = product.get("buyBoxSellerIdHistory", [])
    bb_sellers = set()
    for i in range(1, len(bb_history), 2):  # [time, seller, time, seller, ...]
        seller = bb_history[i]
        if seller and seller not in ("-1", ""):
            bb_sellers.add(seller)

    # Current offer sellers
    offers = product.get("offers", [])
    offer_sellers = []
    for o in offers[:10]:
        sid = o.get("sellerId")
        if sid:
            offer_sellers.append({
                "seller_id": sid,
                "is_fba": o.get("isFBA"),
                "price": _price_from_csv(o.get("offerCSV"), 3),
            })

    return {
        "status": "success",
        "asin": asin.upper(),
        "buy_box_sellers_historical": list(bb_sellers),
        "buy_box_seller_count": len(bb_sellers),
        "current_offer_sellers": offer_sellers,
    }


# ---------------------------------------------------------------------------
# Other Keepa endpoints (lightweight, no caching needed)
# ---------------------------------------------------------------------------

async def keepa_product_finder(
    selection: str,
    domain: int = 1,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """Search for products using Keepa's Product Finder with filters.

    Args:
        selection: JSON string with selection criteria.
        domain: Amazon locale (1-11). Default: 1 (US).
    """
    api_key = _get_api_key()
    if not api_key:
        return {"status": "error", "message": "KEEPA_API_KEY not configured."}
    try:
        criteria = json.loads(selection)
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON for selection."}

    params = {"key": api_key, "domain": domain, "selection": json.dumps(criteria)}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token_info = await _check_token_balance(client, api_key)
            token_err = _check_tokens_or_error(token_info)
            if token_err:
                return token_err
            resp = await client.get(f"{_API_BASE}/query", params=params)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "tokens_left": data.get("tokensLeft"), "asins": data.get("asinList", [])}
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {e}"}


async def keepa_get_categories(
    domain: int, category: Optional[int] = None, tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieve Keepa's category tree or details for a specific category.

    Args:
        domain: Amazon locale (1-11).
        category: Optional category ID.
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
        return {"status": "error", "message": f"Keepa API error: {e}"}


async def keepa_get_bestsellers(
    domain: int, category: int, tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieve the bestseller list for a specific category.

    Args:
        domain: Amazon locale (1-11).
        category: The category ID.
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
            # Keepa may nest under "bestSellersList" or return asinList at top level
            asins = (
                data.get("bestSellersList", {}).get("asinList", [])
                or data.get("asinList", [])
            )
            if not asins:
                return {
                    "status": "error",
                    "message": f"No bestsellers found for category {category}.",
                    "tokens_left": data.get("tokensLeft"),
                    "response_keys": list(data.keys()),
                }
            return {
                "status": "success",
                "tokens_left": data.get("tokensLeft"),
                "category": category,
                "count": len(asins),
                "asins": asins,
            }
    except Exception as e:
        return {"status": "error", "message": f"Keepa API error: {e}"}


async def keepa_get_seller_info(
    domain: int, seller_id: str, tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieve information about a specific Amazon seller.

    Args:
        domain: Amazon locale (1-11).
        seller_id: The Amazon Seller ID.
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
        return {"status": "error", "message": f"Keepa API error: {e}"}


async def keepa_get_top_sellers(
    domain: int, tool_context: Optional[ToolContext] = None,
) -> dict:
    """Retrieve the list of top sellers for a given Amazon locale.

    Args:
        domain: Amazon locale (1-11).
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
        return {"status": "error", "message": f"Keepa API error: {e}"}
