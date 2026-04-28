# Research Examples

Worked end-to-end workflows. Use them as a template, not verbatim — the user's question will rarely match exactly.

## Example 1: Single ASIN deep dive

```
User: "Tell me everything about B0CJ5G3GGN"

1. keepa_fetch_product(asin="B0CJ5G3GGN")
   → Caches raw data, returns summary (title, brand, category, current prices, monthly_sold tier)

2. keepa_extract_pricing(asin="B0CJ5G3GGN")
   → Amazon $29.97, Buy Box $29.97, 3P New from $32.50
   → Active coupon: 10% off → best_offer $26.97

3. keepa_extract_sales_analysis(asin="B0CJ5G3GGN", days=30)
   → 3,000–4,000 units/month (tier 3000)
   → Revenue: $89,910–$119,880/month
   → Effective price avg: $26.97 (after coupon)
   → BSR avg: ~#1,234 in Home & Kitchen

4. keepa_extract_offers(asin="B0CJ5G3GGN")
   → Buy Box: seller A1B2C3D4E5 (FBA), 8 other FBA, 4 FBM

5. keepa_extract_competitors(asin="B0CJ5G3GGN")
   → 5 unique buy box winners over the last 90 days
```

Report sales as a range, never as an exact number.

## Example 2: Multi-ASIN competitor research

```
User: "Compare our B0CJ5G3GGN against the top 5 competitors in our subcategory"

1. keepa_check_tokens()
   → Need ~18 tokens (1 finder + 1 categories + 5 × 3 fetches). Confirm we have headroom.

2. keepa_get_categories(domain=1, query="bed sheets")
   → Bed Sheets category ID: 1063252

3. keepa_product_finder(domain=1, selection=json.dumps({
     "categoryIds": [1063252],
     "current_SALES_gte": 1,
     "current_SALES_lte": 5000,
     "current_COUNT_REVIEWS_gte": 100,
     "productType": [0],
     "hasParentASIN": false,
     "sort": [["current_SALES", "asc"]],
     "perPage": 20
   }))
   → 20 candidate ASINs

4. For each of top 5 (excluding our own):
   keepa_fetch_product(asin=X)
   keepa_extract_sales_analysis(asin=X, days=30)
   → Write per-ASIN findings to scratchpad after each: scratchpad_write("competitor_<asin>", findings)

5. scratchpad_read_all (or list keys)
   → Synthesize: rank by mid-point of revenue range, compare effective prices, note coupon usage,
     flag any seller dominance (one seller winning buy box >80% of days)
```

## Example 3: Trend analysis over time

```
User: "How has B0CJ5G3GGN's BSR been trending over the last 90 days?"

1. keepa_fetch_product(asin="B0CJ5G3GGN")

2. keepa_extract_history(asin="B0CJ5G3GGN", metric="sales_rank", days=90)
   → Daily BSR points
   → Report: "BSR improved from #2,100 (90 days ago) → #1,234 (today), a ~41% improvement"

3. keepa_extract_history(asin="B0CJ5G3GGN", metric="buy_box", days=90)
   → Confirm price stability vs. promotion-driven spike
   → If price dropped 20% mid-period, the rank improvement is partly promo-driven, not organic
```

## Example 4: Discovery — "find me products that look like good opportunities"

```
User: "Find me kitchen gadgets under $30 with 4+ stars, decent sales, but few reviews
       (i.e., not yet saturated)"

1. keepa_get_categories(domain=1, query="kitchen")
   → Confirm a relevant category ID, e.g., 284507 (Kitchen & Dining)

2. keepa_product_finder(domain=1, selection=json.dumps({
     "rootCategory": 284507,
     "current_BUY_BOX_SHIPPING_lte": 3000,         # ≤ $30.00
     "current_RATING_gte": 40,                      # 4.0★ on 0–50 scale
     "current_SALES_lte": 50000,                    # decent sales
     "current_COUNT_REVIEWS_lte": 200,              # few reviews
     "productType": [0],
     "hasParentASIN": false,
     "sort": [["current_SALES", "asc"]],
     "perPage": 25
   }))
   → 25 candidate ASINs

3. Pick top 3–5, run the deep-dive workflow on each
4. Report: ranked by estimated revenue and review-to-sales ratio
```

## Example 5: Buy box history

```
User: "Have we been losing the buy box on B0CJ5G3GGN?"

1. keepa_fetch_product(asin="B0CJ5G3GGN")

2. keepa_extract_competitors(asin="B0CJ5G3GGN")
   → buy_box_sellers_historical: ["A1B2C3D4E5" (us), "F5G6H7I8J9", "K0L1M2N3O4"]
   → 3 unique winners

3. (Optional) keepa_get_seller_info(domain=1, seller_id="F5G6H7I8J9")
   → Storefront: "Acme Wholesale" — 3P FBA, 95% positive feedback

4. Report: "Buy box has rotated between 3 sellers over the last 90 days.
   You held it most of the time; Acme Wholesale and one other took it during
   their offer windows. Consider checking your repricer's floor price."
```

## Common gotchas (quick reference)

- Always `keepa_fetch_product` before any `keepa_extract_*` call
- Parent ASINs (`productType=5`) have no prices/rank/offers — query a child ASIN
- `monthlySold` is a tier, not exact units — always report as a range
- Cache lasts 1 hour — same-session re-queries are free; next-hour queries re-fetch
- `keepa_check_tokens` before any bulk operation (each fetch = 3 tokens)
- For complex filter sets, suggest the user generate JSON via Keepa's web UI
