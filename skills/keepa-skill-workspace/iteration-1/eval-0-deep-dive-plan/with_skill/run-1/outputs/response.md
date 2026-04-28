# Research plan for ASIN B0CJ5G3GGN

This is a single-ASIN deep-dive with three specific asks (pricing+coupons, sales+revenue, buy-box winners). Keepa works on a **fetch-then-extract** pattern: one API call caches the raw product blob to `tmp/keepa_cache/B0CJ5G3GGN.json`, then a series of zero-cost extract calls pull focused slices from that cache. Cache is good for 1 hour, so all the extract calls below are free as long as they run inside that window.

## Recommended order of tool calls

### Step 0 — Sanity check the token budget (optional but cheap)

```
keepa_check_tokens()
```
Returns `{tokens_left, refill_rate_per_min}` — confirms we have at least ~3 tokens free before the fetch. **Cost: 0 tokens.**

### Step 1 — Fetch (the only API call in this flow)

```
keepa_fetch_product(asin="B0CJ5G3GGN", domain=1)
```
Hits Keepa `/product` with `offers=20`, caches the raw response to disk, returns a lightweight summary (title, brand, category, current_prices, coupon, monthly_sold tier). **Cost: 3 tokens (1 base + 2 for offers).**

### Step 2 — (a) Pricing across channels + active discounts

```
keepa_extract_pricing(asin="B0CJ5G3GGN")
```
Returns `prices` for amazon / new_3p / used / list_price / warehouse / new_fba / buy_box / prime_exclusive / prime_exclusive_live, plus a typed `coupon` field and a computed `best_offer` / `best_offer_source` (cheapest path to buy, including buy-box-with-coupon). **Cost: 0 (cache read).**

### Step 3 — (b) Estimated monthly sales and revenue

```
keepa_extract_sales_analysis(asin="B0CJ5G3GGN", days=30)
```
Walks `monthlySoldHistory` through Keepa's tier→range mapping and time-weights against effective price (lightning deal > prime exclusive > buy box > new, with active coupon applied). Returns a `summary` block with `total_sales_min/max`, `avg_daily_sales_min/max/avg`, `total_revenue_min/max`, plus a `daily` array of per-day sales/price/BSR. I'll use `days=30` because you're sizing inventory — a recent 30-day window is more decision-relevant than 90 days of stale signal. **Cost: 0 (cache read).** Sales must be reported as a range, never a precise number — `monthlySold` is a tier indicator (50, 100, 200, ..., 3000, 4000, ...), not a unit count.

### Step 4 — (c) Who's currently winning the buy box

```
keepa_extract_offers(asin="B0CJ5G3GGN")
```
Returns `offer_counts` (new_total / used_total / new_fba / new_fbm), the **current** `buy_box_seller` ID, and up to 10 live offers with seller / FBA / Prime / condition / price. **Cost: 0 (cache read).**

### Step 5 — Historical buy-box winners (the "who's been winning" part)

```
keepa_extract_competitors(asin="B0CJ5G3GGN")
```
Returns `buy_box_sellers_historical` (unique seller IDs that have held the box) and `buy_box_seller_count`, plus the same current-offer seller list. This is what answers "who's been winning the box" over time, vs. step 4 which is just the current snapshot. **Cost: 0 (cache read).**

### Step 6 — (optional) Look up an unknown 3P seller by storefront name

If `extract_competitors` surfaces a seller ID you don't recognize and you want to know who they actually are:

```
keepa_get_seller_info(domain=1, seller_id="<id from step 5>")
```
Returns Keepa's full seller payload (storefront name, ratings, etc.) under `data`. **Cost: ~1 token per lookup.** Skip this unless a specific competitor is interesting — it's the only step that adds meaningful cost on top of the fetch.

## Total token cost

| Scenario | Tokens |
|---|---|
| Steps 0–5 only (full deep dive, no seller lookups) | **3** |
| + 1 seller lookup (step 6) | ~4 |
| + 3 seller lookups (look up every historical buy-box winner) | ~6 |

So the realistic budget for this whole flow is **3–6 Keepa tokens**. The fetch is the only meaningful cost; everything inside the 1-hour cache window is free. If you re-run any step within the hour the fetch doesn't repeat — only after TTL expiry does it cost another 3.

## What this flow will NOT tell you

A few things you might want for an inventory-buying decision that this skill explicitly does **not** cover, so I want to flag them rather than guess:

- **Your own seller-account internals** (real-time inventory, your actual sell-through, account-level fees, settlement). The skill notes these belong to SP-API (`sp_get_inventory_summaries`, `sp_list_orders`), not Keepa.
- **Lightning-deal calendar / upcoming promos.** The lightning-deals endpoint is intentionally not exposed (it's ~500 tokens per call).
- **PPC / advertising spend or ACoS.** Not in Keepa at all.
- **Parent-ASIN gotcha.** If B0CJ5G3GGN turns out to be a parent (`productType=5`), step 1's summary will come back with all-null prices and no `monthly_sold` — in that case I'll stop and ask you for a child ASIN, because parent ASINs have no usable price/rank/offer data and BSR is shared across all variations anyway.

## One trend signal worth adding if you go deeper

If after the above you want to know whether sales are accelerating or decaying (relevant for an inventory buy decision), one extra free call:

```
keepa_extract_history(asin="B0CJ5G3GGN", metric="sales_rank", days=90)
```
Daily BSR series over 90 days — answers "is rank trending up or down?" Pair with `metric="buy_box"` if you want to disambiguate "rank improved organically" vs. "rank improved because price dropped 20%". Both are 0-cost cache reads inside the TTL window.
