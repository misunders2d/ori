# Keepa Research — Worked Examples

## Example 1: Single ASIN Deep Dive

```
User: "Tell me everything about B0CJ5G3GGN"

1. keepa_fetch_product(asin="B0CJ5G3GGN")
   → Caches raw data, returns summary (title, category, BSR, type)

2. keepa_extract_pricing(asin="B0CJ5G3GGN")
   → Current prices: Amazon $29.97, Buy Box $29.97, 3P New from $32.50
   → Active coupon: 10% ($3.00 off)

3. keepa_extract_sales_analysis(asin="B0CJ5G3GGN", days=30)
   → Monthly sold: 3000-4000 units (tier 3000)
   → Revenue: $89,910-$119,880/month
   → Effective price: $26.97 (after coupon)
   → BSR: #1,234 in Home & Kitchen

4. keepa_extract_offers(asin="B0CJ5G3GGN")
   → Buy Box: Brand X (FBA), 3 other FBA sellers, 2 FBM

5. keepa_extract_competitors(asin="B0CJ5G3GGN")
   → 5 historical Buy Box sellers in last 90 days
```

## Example 2: Multi-ASIN Competitor Research

```
User: "Compare our product B0CJ5G3GGN against the top 5 competitors in our subcategory"

1. keepa_get_categories(domain=1, parent=0, query="bed sheets")
   → Bed Sheets category ID: 1063252

2. keepa_product_finder(domain=1, selection={
     "categoryIds": [1063252],
     "salesRankRange": [1, 5000],
     "current_COUNT_REVIEWS_min": 100
   })
   → Returns 20 ASINs: B0CJ5G3GGN, B08Q2BQXX1, B09K3LMXYZ, ...

3. For each of top 5 competitors:
   keepa_fetch_product(asin=ASIN)
   keepa_extract_sales_analysis(asin=ASIN, days=30)
   → Write findings to scratchpad: scratchpad_write("competitors", findings)

4. scratchpad_read("competitors")
   → Synthesize: rank by estimated revenue, compare pricing, note coupons
```

## Example 3: Sales Trend Over Time

```
User: "How has B0CJ5G3GGN's BSR been trending over the last 90 days?"

1. keepa_fetch_product(asin="B0CJ5G3GGN")

2. keepa_extract_history(asin="B0CJ5G3GGN", metric="sales_rank", days=90)
   → Returns daily BSR values over 90 days
   → Report: "BSR improved from #2,100 → #1,234 (41% improvement)"

3. keepa_extract_history(asin="B0CJ5G3GGN", metric="buy_box", days=90)
   → Shows price stability or fluctuations
```

## Common Gotchas (Repeated for Quick Reference)

- Always `keepa_fetch_product` BEFORE any `keepa_extract_*` call
- Parent ASINs (productType=5) have NO data — query child ASINs
- monthlySold is a tier indicator, not exact units — always report as range
- Cache lasts 1 hour — re-fetch for fresh data
- Check `keepa_check_tokens` before bulk operations (each fetch = 1-3 tokens)
