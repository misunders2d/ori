---
name: keepa-skill
description: "Keepa API workflow for Amazon product research — fetch-store-extract pattern with detailed pricing, real sales estimates (from Keepa's monthlySold tier mapping), historical price/rank trends, competitor analysis, bestseller discovery, and product-finder filtered search. Use this skill whenever the user asks about an ASIN's price history, BSR trend, sales volume, who's selling it, who the competitors are, what's in a category, or any product research question on Amazon — even if they don't say 'Keepa' explicitly."
---

# Keepa Product Research

You have access to Keepa for Amazon product intelligence: pricing, sales estimates, ranks, offers, sellers, categories, bestsellers, and filter-based discovery. Keepa product responses are **massive** (raw JSON can be hundreds of KB per ASIN), so this toolset uses a **fetch → extract** pattern: fetch caches the raw data on disk, then extract tools pull focused slices on demand. Never try to load or display raw Keepa data in the conversation.

## How to navigate this skill

SKILL.md is the routing doc. Each topic has its own reference file — read the one that matches what the user is asking about rather than holding all of Keepa in your head.

| User is asking about... | Read |
|---|---|
| What each tool does, parameters, example calls | [`references/tools.md`](references/tools.md) |
| Which CSV index holds what, or which product fields exist | [`references/product-data.md`](references/product-data.md) |
| Discovering ASINs with filters (rank, price, category, sales) | [`references/product-finder.md`](references/product-finder.md) |
| Token costs, domain IDs, cache TTL, rate limits | [`references/budget-and-limits.md`](references/budget-and-limits.md) |
| Worked end-to-end research workflows | [`references/research-examples.md`](references/research-examples.md) |

## Tool inventory

**Fetch** (calls API, caches raw data, returns lightweight summary):
- `keepa_fetch_product(asin, domain=1)` — must be called before any extract tool

**Extract** (read from cache, return focused slices):
- `keepa_extract_pricing(asin)` — current prices + best-offer analysis (with coupons)
- `keepa_extract_sales_analysis(asin, days=90)` — **primary analysis tool** — daily real sales (min/max from tier mapping), effective prices, BSR, revenue
- `keepa_extract_history(asin, metric, days=90)` — time series for one metric (amazon, new, used, sales_rank, buy_box, new_fba, prime_exclusive, rating, review_count, list_price)
- `keepa_extract_offers(asin)` — buy box holder, FBA/FBM counts, top live offers
- `keepa_extract_competitors(asin)` — historical buy box sellers, current offer sellers
- `keepa_extract_stats(asin)` — rank, monthly sold, rating, reviews, listing age, FBA fees

**Discovery** (no fetch needed; their own API calls):
- `keepa_product_finder(selection, domain=1)` — filtered ASIN search; selection is a JSON string
- `keepa_get_categories(domain, category=None)` — browse category tree to find IDs
- `keepa_get_bestsellers(domain, category)` — top ASINs in a category
- `keepa_get_seller_info(domain, seller_id)` — details on a 3P seller
- `keepa_get_top_sellers(domain)` — biggest 3P sellers in marketplace (**expensive: 50 tokens**)

**Utility:**
- `keepa_check_tokens()` — current API token balance (0 cost)

See [`references/tools.md`](references/tools.md) for full signatures, return shapes, and when to use each.

## Standard workflows

### Single-ASIN deep dive
1. `keepa_fetch_product(asin)` — caches raw data
2. `keepa_extract_pricing(asin)` — current price landscape
3. `keepa_extract_sales_analysis(asin, days=30)` — real sales + revenue estimate
4. `keepa_extract_offers(asin)` — who's currently selling
5. (optional) `keepa_extract_history(asin, "sales_rank", days=90)` — BSR trend

### Multi-ASIN comparison
1. `keepa_check_tokens()` first — each fetch costs ~3 tokens
2. For each ASIN: `keepa_fetch_product` → `keepa_extract_sales_analysis(days=30)`
3. Write per-ASIN findings to scratchpad between iterations (3+ ASINs)
4. Read scratchpad and synthesize comparison at the end

### Discovery (find ASINs to analyze)
1. `keepa_get_categories(domain, query="…")` to find a category ID
2. `keepa_product_finder(selection=…)` with rank/price/category filters → list of ASINs
3. Then run the deep-dive workflow on each candidate

## Cross-cutting rules

### Fetch before extract
Every `keepa_extract_*` tool reads from the local cache. If the ASIN has not been fetched (or the cache is stale, >1 hour old), the tool returns `{"status": "error", ...}`. Always call `keepa_fetch_product` first. The cache lives at `tmp/keepa_cache/{ASIN}.json`.

### Parent ASINs have no useful data
A parent ASIN (Keepa `productType=5`) is a variation hub — it has no prices, no rank, no offers. If `keepa_fetch_product` returns a summary with all-null prices and no `monthly_sold`, you're looking at a parent. Ask the user for a child ASIN, or use `keepa_product_finder` with `hasParentASIN: false` to get only buyable products.

### BSR is shared across variations
All children of the same parent inherit the parent's sales rank. Use `monthlySold` (via `keepa_extract_sales_analysis`) — not BSR — to differentiate variation performance.

### `monthlySold` is a tier indicator, not exact units
Keepa's `monthlySold` field is sourced from Amazon's "bought past month" badge but bucketed into tiers (50, 100, 200, …, 3000, 4000, …). The sales-analysis tool maps each tier to a `(min, max)` range. **Always report sales as a range** (e.g., "3,000–4,000 units/month"), never as a precise number. Many ASINs have no `monthlySold` value at all — Amazon only shows the badge for some products.

### `offers` parameter rules
The fetch tool always requests `offers=20`. Keepa requires `offers` to be either omitted (no live offer data) or in the range 20–100. Offer queries cost +2 tokens on top of the base 1.

### Error handling
If a tool returns `{"status": "error", "message": "…"}`, relay the message verbatim and stop. Don't retry, don't fabricate, don't guess at data. Token-budget errors specifically include the refill ETA — pass that on so the user can decide whether to wait or proceed differently.

### Token budgeting
Run `keepa_check_tokens()` before any operation that will fetch >5 ASINs, or before discovery + bulk-fetch flows. See [`references/budget-and-limits.md`](references/budget-and-limits.md) for per-endpoint costs. The 50-token cost of `keepa_get_top_sellers` and the very-high cost of lightning-deal queries are easy to overlook.

## What's NOT available

- **Write operations.** No tracking-add, no price tracking, no notifications. Everything in this toolset is read-only.
- **Lightning deals endpoint.** Not exposed (it's expensive — ~500 tokens per call).
- **Generic Keepa "deals" search.** Not exposed. If the user wants discounted products, use `keepa_product_finder` with price-drop filters instead.
- **A non-default `offers` value.** Tools always fetch `offers=20`. If you need 100 offers, that's not currently wired up.
- **Brazil (`domain=12`).** The existing toolset documents domains 1–11; Keepa supports BR=12 but the tools haven't been verified against it. Check before using.

## Live references

- [Keepa API endpoints](https://keepa.com/api/)
- [Keepa product object (Java struct, canonical)](https://github.com/keepacom/api_backend/blob/master/src/main/java/com/keepa/api/backend/structs/Product.java)
- [Keepa Request struct (parameters)](https://github.com/keepacom/api_backend/blob/master/src/main/java/com/keepa/api/backend/structs/Request.java)
- [Keepa community forum (gotchas, schema discussions)](https://keepa.com/#!discuss/t/keepa-api/150)
