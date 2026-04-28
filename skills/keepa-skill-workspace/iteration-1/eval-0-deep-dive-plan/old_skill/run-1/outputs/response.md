# Research Plan: ASIN B0CJ5G3GGN — Pricing, Sales, Buy Box

Below is the exact tool-call sequence I'd run, in order, with parameters. The Keepa skill enforces a **fetch → extract** pattern: one `keepa_fetch_product` populates a 1-hour cache, then each extraction tool reads from that cache (no extra API hit per extract).

## Tool calls in order

### 1. (Optional but recommended on a budget) Check token balance

```
keepa_check_tokens()
```
**Returns (one line):** Remaining Keepa token balance. Cost: 0 tokens.

### 2. Fetch and cache the product

```
keepa_fetch_product(asin="B0CJ5G3GGN")
```
**Returns (one line):** Lightweight summary (title, category, BSR, productType) and caches the full raw payload for the next ~1 hour so subsequent `extract_*` calls are free.

### 3. (a) Current pricing across all channels + active discounts

```
keepa_extract_pricing(asin="B0CJ5G3GGN")
```
**Returns (one line):** Current Amazon, Buy Box, 3P New, and Prime Exclusive prices, plus any active coupon / best offer (e.g. "10% off, $3.00").

### 4. (b) Estimated monthly sales and revenue

```
keepa_extract_sales_analysis(asin="B0CJ5G3GGN", days=30)
```
**Returns (one line):** Monthly sales as a min–max range derived from Keepa's `monthlySold` tier (e.g. 3,000–4,000 units), effective price (after coupons/lightning deals), revenue range, and current BSR — this is the primary "how much does it sell?" tool per the skill.

### 5. (c) Who's been winning the buy box

```
keepa_extract_offers(asin="B0CJ5G3GGN")
```
**Returns (one line):** Current Buy Box holder, FBA vs FBM seller counts, top offer details (this is the "right now" view).

```
keepa_extract_competitors(asin="B0CJ5G3GGN")
```
**Returns (one line):** Historical Buy Box sellers over the recent window plus the current offer-list sellers — this is what answers "who's been winning" (plural / over time), as opposed to just the current holder.

## Token cost estimate

Per the skill's gotchas: **"Each product fetch = 1 token + 2 for offers."**

| Call | Tokens |
|---|---|
| `keepa_check_tokens` | 0 |
| `keepa_fetch_product` (with offers, needed for buy-box/seller data) | ~3 |
| `keepa_extract_pricing` | 0 (reads cache) |
| `keepa_extract_sales_analysis` | 0 (reads cache) |
| `keepa_extract_offers` | 0 (reads cache) |
| `keepa_extract_competitors` | 0 (reads cache) |
| **Total** | **~3 tokens** for the whole single-ASIN flow |

If we skipped offers (i.e. you didn't care about buy-box / seller data), it would be ~1 token — but you explicitly asked about (c), so the +2 for offers is required.

If the cache expires (>1 hour) and you re-run any extract, that triggers another `keepa_fetch_product` (~3 tokens). Stay inside the hour and you're at 3 tokens total.

## Things to flag before you commit inventory

- **Verify productType is not 5 (parent ASIN).** If `keepa_fetch_product` returns productType=5, B0CJ5G3GGN has no usable data and you must research the child ASIN(s) instead. (Skill gotcha: parent ASINs have no prices/rank/offers.)
- **`monthlySold` is a tier**, not a literal count — always read the result as a range (min–max), never as a single point estimate.
- **BSR is shared across variations** — if this is a variation parent/child set, sales rank alone won't differentiate the SKUs; trust `monthlySold` from sales analysis instead.

## What the skill does NOT cover for this question

- **Trend analysis over time** (e.g. "is the buy box being won more often by 3P sellers in the last 90 days?") would need an additional call: `keepa_extract_history(asin="B0CJ5G3GGN", metric="buy_box", days=90)` and/or `metric="sales_rank"`. Still cache-served (0 extra tokens) once the product is fetched. I didn't include it above because you asked for who's *been* winning buy box, which `keepa_extract_competitors` covers via its historical-sellers list — but if you want the day-by-day timeline, add the history call.
- **Profitability math** (FBA fees, landed cost, margin after coupon) — `keepa_extract_stats` exposes FBA fees, but the skill has no dedicated landed-cost / margin tool. You'd need to combine `keepa_extract_stats` output with your own COGS to compute margin.
- **Demand seasonality beyond the 30-day window** — the sales analysis tool takes a `days` arg; for seasonality you'd run it with a longer window or use `keepa_extract_history(metric="sales_rank", days=365)`. The skill documents this is supported but doesn't give a worked example beyond 90 days.
