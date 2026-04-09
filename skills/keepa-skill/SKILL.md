---
name: keepa-skill
description: "Keepa API workflow — fetch-store-extract pattern for Amazon product research. Covers pricing, sales analysis, competitor research, and bestseller discovery."
---

# Keepa Product Research Skill

You have access to Keepa tools for Amazon product intelligence. Keepa data is **massive** — never return raw API responses. Always use the fetch→extract pattern.

## Workflow: Fetch → Extract

- [ ] Step 1: **Fetch** — `keepa_fetch_product(asin)` to cache the raw data. Returns a lightweight summary only.
- [ ] Step 2: **Extract** — call the specific extraction tool you need. Each reads from the cached file.
- [ ] Step 3: **Record** — for multi-ASIN research, write findings to scratchpad between fetches.

## Extraction Tools

| Tool | When to use |
|------|-------------|
| `keepa_extract_pricing(asin)` | Current prices across all channels (Amazon, 3P, Buy Box, Prime Exclusive), coupons, best offer |
| `keepa_extract_sales_analysis(asin, days)` | **Primary analysis tool.** Real daily sales estimates (min/max from tier mapping), effective prices (with coupons/LDs), BSR, revenue. Use this for any "how much does it sell?" question |
| `keepa_extract_history(asin, metric, days)` | Price/rank/rating trends over time. Metrics: amazon, new, buy_box, sales_rank, rating, review_count, etc. |
| `keepa_extract_offers(asin)` | Who's selling: buy box holder, FBA vs FBM counts, top offer details |
| `keepa_extract_competitors(asin)` | Historical buy box sellers, current offer sellers |
| `keepa_extract_stats(asin)` | Quick stats: rank, monthlySold, rating, reviews, listing age, FBA fees |

## Discovery Tools

| Tool | When to use |
|------|-------------|
| `keepa_product_finder(selection, domain)` | Find ASINs by filters (title, rank, price, category). Returns ASIN list only. |
| `keepa_get_bestsellers(domain, category)` | Top ASINs in a category. Need the category ID first. |
| `keepa_get_categories(domain, category)` | Browse/search the category tree to find category IDs. |
| `keepa_get_seller_info(domain, seller_id)` | Details about a specific 3P seller. |
| `keepa_get_top_sellers(domain)` | Biggest 3P sellers on the marketplace. |
| `keepa_check_tokens()` | Check remaining API token balance (0 cost). |

## Competitor Analysis Procedure

- [ ] Step 1: `keepa_get_categories` to find the right subcategory ID
- [ ] Step 2: `keepa_product_finder` with title/category/rank filters to get candidate ASINs
- [ ] Step 3: `keepa_fetch_product` for each candidate
- [ ] Step 4: `keepa_extract_sales_analysis` to get real sales numbers
- [ ] Step 5: Compare, rank, and report — write to scratchpad between steps if >3 ASINs

## Understanding Sales Data

Keepa's `monthlySold` is a **tier indicator**, not exact units. The sales analysis tool maps tiers to min/max ranges:
- 3000 → 3,000-4,000 units/month
- 50 → 50-100 units/month
- Always report sales as a range (min-max), never as exact numbers.

## Live References

- [Keepa API Documentation](https://keepa.com/#!discuss/t/keepa-api/150)
- [Keepa API Endpoint Reference](https://keepa.com/api/)
- [Amazon Product Advertising API (context)](https://webservices.amazon.com/paapi5/documentation/)

## Gotchas

- **Always fetch before extracting.** Extract tools read from cache — they fail if the ASIN wasn't fetched first.
- **Cache lasts 1 hour.** After that, `keepa_fetch_product` will re-fetch from the API.
- **Parent ASINs have NO data.** `productType=5` means no prices, no rank, no offers. Always query child ASINs.
- **BSR is shared across variations.** All children of the same parent have the same sales rank. Use `monthlySold` (via sales analysis) to differentiate variation performance.
- **Never pass offers > 20.** Keepa requires offers=0 or offers≥20. The fetch tool handles this.
- **Token costs add up.** Each product fetch = 1 token + 2 for offers. Check balance with `keepa_check_tokens` before bulk operations.
- **Error handling**: follows system-level error mandate (report immediately, never fabricate).
