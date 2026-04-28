# Keepa Tool Reference

Every tool returns a dict with `"status": "success" | "error"`. Error responses include a `message` field — relay it verbatim and stop.

## Fetch

### `keepa_fetch_product(asin: str, domain: int = 1) -> dict`

Hits `/product` with `offers=20`, caches the full response to `tmp/keepa_cache/{ASIN}.json`, returns a lightweight summary. Cache TTL = 1 hour. **Token cost: 3** (1 base + 2 for offers).

**Returns:**
```python
{
  "status": "success",
  "asin": "B0CJ5G3GGN",
  "title": "...",
  "brand": "...",
  "category": "Home & Kitchen > Bedding > Sheets",
  "current_prices": {
    "amazon": 29.97 | None,
    "new_3p": 29.97 | None,
    "buy_box": 29.97 | None,
    "prime_exclusive": None,
  },
  "coupon": "$3.00 off" | "10% off" | None,
  "monthly_sold": 3000 | None,        # tier value, not exact units
  "from_cache": False,
  "tokens_left": 1234,
  "hint": "Use keepa_extract_* tools..."
}
```

**Use it before:** any `keepa_extract_*` call. The summary is a teaser — call extract tools for real data.

## Bulk (one API call, many ASINs)

### `keepa_bulk_query(asins: str, fields: str = ..., with_offers: bool = False, domain: int = 1) -> dict`

When you have more than ~5 ASINs and only need a subset of fields per ASIN — review counts across 100 children of a parent, BSR check across a competitor list, price audit of your catalog — this is the right tool. Single API call, chunked internally (100 ASINs per batch without offers, 20 with), parallel. Returns a flat table with one row per ASIN. Each product's full payload is also cached so any individual `keepa_extract_*` follow-up works for free.

**`asins`** — comma-separated, no spaces required. e.g. `"B0...,B1...,B2..."`.

**`fields`** — comma-separated field names. ASIN is always included. Valid:

| Field | Source |
|---|---|
| `title`, `brand`, `category`, `product_type`, `parent_asin`, `parent_title` | catalog metadata |
| `review_count`, `rating`, `sales_rank`, `sales_rank_category`, `monthly_sold` | sales signals |
| `amazon_price`, `new_3p_price`, `buy_box`, `prime_exclusive`, `list_price`, `lightning_deal` | prices (USD, `null` when inactive) |
| `active_deal`, `coupon` | promo state |
| `listed_since`, `fba_pickpack_fee` | listing metadata |

Default field set covers the common bulk-research case: `asin, title, review_count, rating, sales_rank, monthly_sold, buy_box, active_deal`.

**`with_offers`** — `False` (default) costs **1 token per ASIN**, max 100 ASINs/batch. `True` adds live offer data, costs **3 tokens per ASIN**, max 20/batch. Leave it `False` for review-count / rank / price questions; only flip it on when you need offer-level data.

**Returns:**
```python
{
  "status": "success" | "partial",
  "asins_requested": 142,
  "asins_returned": 140,
  "asins_missing": ["B0X...", "B0Y..."],   # not found in Keepa
  "tokens_left": 850,
  "fields": ["asin", "title", "review_count", ...],
  "rows": [
    {"asin": "B0...", "title": "...", "review_count": 1740, "rating": 4.4, ...},
    ...
  ]
}
```

`asins_missing` costs no tokens. `status: "partial"` means some chunks errored (look at `batch_errors` for details); successful chunks are still in `rows`.

**Use it for:** "review count for all 120 children of B0X...", "BSR + monthly sold for these 40 competitor ASINs", "price + active_deal across our 200 SKUs".

**Don't use it for:** single-ASIN deep dives (use `keepa_fetch_product` + `keepa_extract_*` instead — same cost, more detail).

## Extract (read from cache, no API cost)

### `keepa_extract_pricing(asin: str) -> dict`

Detailed current prices across all channels + best-offer analysis.

**Returns** `prices` with: `amazon, new_3p, used, list_price, lightning_deal, warehouse, new_fba, buy_box, prime_exclusive, prime_exclusive_live`. Plus:

- `coupon` (typed) — current coupon, `null` if none
- `active_deals` — list of Amazon deal badges currently on the listing. Each entry has `{type, badge, audience}`. Common `type` values: `LIMITED_TIME_DEAL`, `LIGHTNING_DEAL`, `BEST_DEAL`, `PRIME_EARLY_ACCESS`. Empty list `[]` means no active badge. **This is how you detect "Limited time deal" / strike-through pricing — neither the price CSVs nor the coupon field carry that signal.**
- `promotions` — list of seller-side promotions. Each entry has `{type, amount_dollars, discount_percent, sns_bulk_discount_percent, seller_id}`. The most common is `type: "SNS"` whose `amount_dollars` acts as the Subscribe & Save reference price (the "typical price" Amazon strikes through during a deal).
- `best_offer` / `best_offer_source` — best price across all channels, including lightning deals and buy-box-with-coupon.

**Use it for:** "what's the current price?", "is there a coupon?", "is there a deal running?", "what's the typical price?". A `null` price field or `[]` deals list means "no active value right now," not "broken."

### `keepa_extract_sales_analysis(asin: str, days: int = 90) -> dict`

The **primary analysis tool**. Walks Keepa's change-only CSV data with time-weighted interpolation to produce daily rows.

**Returns:**
```python
{
  "summary": {
    "total_sales_min": 3200, "total_sales_max": 4100,
    "avg_daily_sales_min": 35.5, "avg_daily_sales_max": 45.5,
    "avg_daily_sales": 40.5,
    "total_revenue_min": 95880.00, "total_revenue_max": 122830.00,
  },
  "daily": [
    {"date": "2026-04-01", "sales_min": 33, "sales_max": 43,
     "new_price": 29.97, "buy_box_price": 29.97, "effective_price": 26.97,
     "coupon": "$3.00 off", "bsr": 1234},
    ...
  ]
}
```

`effective_price` accounts for lightning deals → prime exclusive → buy box → new, then applies any active coupon. Sales are derived from `monthlySoldHistory` mapped through tier→range.

**Use it for:** any "how much does it sell?", "what's the revenue?", "what's the BSR trend?" question. Reports must use ranges (min/max), never exact numbers.

### `keepa_extract_history(asin: str, metric: str = "buy_box", days: int = 90) -> dict`

Time series for one specific metric.

**Valid metrics:** `amazon, new, used, sales_rank, list_price, lightning_deal, new_fba, buy_box, prime_exclusive, rating, review_count`.

**Returns:** `{"metric": "buy_box", "data_points": 87, "history": [{"date": "2026-04-01", "price": 29.97}, ...]}`. For `metric="rating"`, the field is `"rating"` (already divided by 10 → 0–5 scale) instead of `"price"`.

**Use it for:** trend questions on a single metric when you don't need the full sales-analysis output. Cheaper signal-to-noise than `extract_sales_analysis` for "show me the BSR over time".

### `keepa_extract_offers(asin: str) -> dict`

Current offer landscape.

**Returns:**
```python
{
  "offer_counts": {"new_total": 12, "used_total": 0, "new_fba": 8, "new_fbm": 4},
  "buy_box_seller": "A1B2C3D4E5",
  "live_offers": [
    {"seller_id": "A1B2C3D4E5", "is_fba": True, "is_prime": True,
     "is_prime_excl": False, "condition": 1, "price": 29.97},
    ... up to 10 ...
  ]
}
```

**Use it for:** "who's selling this?", "is the buy box ours?", "FBA vs FBM split?".

### `keepa_extract_competitors(asin: str) -> dict`

Unique sellers from buy-box history + current offer sellers.

**Returns:**
```python
{
  "buy_box_sellers_historical": ["A1B2...", "F5G6..."],   # unique seller IDs
  "buy_box_seller_count": 2,
  "current_offer_sellers": [
    {"seller_id": "...", "is_fba": True, "price": 29.97},
    ...
  ]
}
```

**Use it for:** "who's been winning the buy box?", competitive landscape.

Pair with `keepa_get_seller_info(domain, seller_id)` to look up brand/storefront info for a specific seller.

### `keepa_extract_stats(asin: str) -> dict`

Quick stats roll-up.

**Returns:** `sales_rank, sales_rank_category, monthly_sold, rating, review_count, availability_amazon, is_sns, listed_since, tracking_since, fba_fees`.

**Use it for:** "give me the basics on this ASIN" — listing age, current rank, Subscribe & Save eligibility, Amazon's stock status, FBA fee breakdown.

## Discovery (own API calls — token cost applies)

### `keepa_product_finder(selection: str, domain: int = 1) -> dict`

Filter-based ASIN search. **Token cost: ~1**. See [`product-finder.md`](product-finder.md) for the selection JSON schema.

`selection` must be a **JSON string**, not a dict. Returns `{"asins": ["B0...", ...]}`.

### `keepa_get_categories(domain: int, category: int | None = None) -> dict`

Browse the Amazon category tree. **Token cost: ~1**. With no `category`, returns root categories. With a category ID, returns its children + parent chain.

**Use it for:** finding the category ID before calling `keepa_get_bestsellers` or filtering with `keepa_product_finder`.

### `keepa_get_bestsellers(domain: int, category: int) -> dict`

Top ASINs in a category. **Token cost: ~1**.

**Returns:** `{"category": 1063252, "count": 100, "asins": ["B0...", ...]}`. Then run `keepa_fetch_product` on the ASINs you want to analyze.

### `keepa_get_seller_info(domain: int, seller_id: str) -> dict`

Look up a 3P seller. **Token cost: ~1**. Returns Keepa's full seller payload (storefront name, ratings, etc.) under `data`.

### `keepa_get_top_sellers(domain: int) -> dict`

Biggest 3P sellers in a marketplace. **Token cost: 50** (expensive — sanity-check the user actually needs this before calling).

## Utility

### `keepa_check_tokens() -> dict`

**Token cost: 0**. Returns `{"tokens_left": 1234, "refill_rate_per_min": 5}`. Use before any bulk fetch operation. The toolset enforces a 5-token reserve and refuses fetches below that — call this first to surface the budget proactively.
