# Keepa Product Data Reference

Two parallel data shapes live on a Keepa product response: the **CSV arrays** (time-series history) and the **flat product fields** (current snapshot + metadata). The extract tools already decode the CSV indices for you, but knowing the canonical map is essential when you need a metric the extract tools don't expose, or when debugging unexpected results.

## CSV index map (canonical, indices 0–35)

Every `csv[i]` is a flat array of `[time, value, time, value, ...]` (or `[time, value, shipping, time, value, shipping, ...]` if the CSV includes shipping). Times are **Keepa minutes** since 2011-01-01 00:00 UTC. Prices are **integer cents**. Value of `-1` means "no offer / unavailable at that timestamp."

| Index | Constant | Meaning | Notes |
|------:|----------|---------|-------|
| 0 | AMAZON | Amazon's own price | |
| 1 | NEW | Lowest 3P New (incl. Amazon if Amazon has lowest) | Shipping included from Feb 2026 onward |
| 2 | USED | Lowest 3P Used | |
| 3 | SALES | Sales rank (BSR) | Shared across variation children |
| 4 | LISTPRICE | List/MSRP | |
| 5 | COLLECTIBLE | Lowest collectible | |
| 6 | REFURBISHED | Lowest refurbished | |
| 7 | NEW_FBM_SHIPPING | 3P New FBM, with shipping | |
| 8 | LIGHTNING_DEAL | Lightning deal price | |
| 9 | WAREHOUSE | Amazon Warehouse Deals | Mostly used condition |
| 10 | NEW_FBA | Lowest 3P New FBA (excl. Amazon) | |
| 11 | COUNT_NEW | Count of New offers | |
| 12 | COUNT_USED | Count of Used offers | |
| 13 | COUNT_REFURBISHED | Count of Refurbished offers | |
| 14 | COUNT_COLLECTIBLE | Count of Collectible offers | |
| 15 | EXTRA_INFO_UPDATES | Offers/buybox refresh timestamps | Value sign = whether all offers fetched |
| 16 | RATING | Star rating × 10 (e.g., 45 = 4.5★) | Divide by 10 |
| 17 | COUNT_REVIEWS | Review count | |
| 18 | BUY_BOX_SHIPPING | Buy box price + shipping | **3-tuple CSV** (`[time, price, shipping]`); -1 if no qualifying offer |
| 19 | USED_NEW_SHIPPING | "Used – Like New" + shipping | |
| 20 | USED_VERY_GOOD_SHIPPING | "Used – Very Good" + shipping | |
| 21 | USED_GOOD_SHIPPING | "Used – Good" + shipping | |
| 22 | USED_ACCEPTABLE_SHIPPING | "Used – Acceptable" + shipping | |
| 23 | COLLECTIBLE_NEW_SHIPPING | "Collectible – Like New" + shipping | |
| 24 | COLLECTIBLE_VERY_GOOD_SHIPPING | "Collectible – Very Good" + shipping | |
| 25 | COLLECTIBLE_GOOD_SHIPPING | "Collectible – Good" + shipping | |
| 26 | COLLECTIBLE_ACCEPTABLE_SHIPPING | "Collectible – Acceptable" + shipping | |
| 27 | REFURBISHED_SHIPPING | Refurbished + shipping | |
| 28 | EBAY_NEW_SHIPPING | eBay lowest New + shipping | |
| 29 | EBAY_USED_SHIPPING | eBay lowest Used + shipping | |
| 30 | TRADE_IN | Trade-in price | Not all locales |
| 31 | RENT | Amazon Rental price | US only; needs `rental` + `offers` params |
| 32 | BUY_BOX_USED_SHIPPING | Used buy box + shipping | -1 if no qualifying offer |
| 33 | PRIME_EXCL | Lowest Prime Exclusive New | |
| 34 | COUNT_NEW_FBA | New FBA offer count (incl. Amazon) | |
| 35 | COUNT_NEW_FBM | New FBM offer count | |

**Time decoding:** `unix_seconds = 1293840000 + keepa_minutes * 60` (epoch is Jan 1 2011 00:00 UTC).

## Non-CSV product fields

Top-level fields on the product object that the extract tools surface or that are useful when reading raw cache:

| Field | What it is |
|---|---|
| `title`, `brand`, `manufacturer`, `partNumber`, `model` | Catalog basics |
| `categoryTree` | Array of `{catId, name}` from root to leaf — join with " > " for breadcrumb |
| `salesRanks` | Map of `{categoryId: [time, rank, time, rank, ...]}` — per-category rank history |
| `salesRankReference` | The category Keepa uses for the headline BSR |
| `productType` | `0` = standard product (has data); `5` = variation parent (no prices/rank/offers); other values = special listings (downloadable, ebook, etc.). **Always check before trusting price/rank fields.** |
| `parentAsin`, `parentTitle` | If the product is a variation child, this points to the rolled-up parent. BSR (`csv[3]`) is shared across variations (all children inherit the parent's rank). |
| `monthlySold` | Tier indicator (not exact units). See sales-tier mapping in `keepa_extract_sales_analysis`. May be null. |
| `monthlySoldHistory` | `[time, tier, time, tier, ...]` — past values of `monthlySold` |
| `coupon` | Active coupon. Positive int = absolute cents off; negative int = percent off |
| `couponHistory` | `[time, oneTime, sns, time, oneTime, sns, ...]` — groups of 3 |
| `deals` | Array of `{dealType, badge, accessType}`. Source of truth for **"Limited time deal"** / "Best Deal" / "Lightning Deal" badges currently shown on Amazon. `extract_pricing` exposes this as `active_deals`. |
| `promotions` | Array of seller promotions. SnS entries (`type: "SNS"`) carry the SnS-eligible "typical price" that gets struck-through when a deal is active. `extract_pricing` exposes this as `promotions`. |
| `buyBoxSellerIdHistory` | `[time, seller_id, time, seller_id, ...]` |
| `offers` | Live offer details (only present when `offers` parameter was used). Each offer has `sellerId, isFBA, isPrime, isPrimeExcl, condition, offerCSV, primeExclCSV` |
| `liveOffersOrder` | Indices into `offers` ranked by competitiveness |
| `availabilityAmazon` | Amazon's own stock state (0 = in stock, others = various OOS states) |
| `isSNS` | True if Subscribe & Save eligible |
| `listedSince`, `trackingSince` | Keepa minutes; convert via the time formula above |
| `fbaFees` | Object with `pickAndPackFee`, `storageFee`, `lastUpdate` |
| `packageWeight`, `packageHeight`, `packageWidth`, `packageLength` | In grams / mm. -1 = unavailable |
| `itemWeight`, `itemHeight`, `itemWidth`, `itemLength` | Same units |
| `numberOfItems`, `numberOfPages` | -1 if unavailable |
| `hazardousMaterialType`, `isAdultProduct`, `isHeatSensitive` | Flags |

## Decoding pattern (when extract tools don't cover what you need)

If a user asks for something none of the `keepa_extract_*` tools surfaces (say, the rental price history), and you confirm Keepa has it, surface the gap to the user — don't try to read the raw cache file from inside the LLM. The cache contains hundreds of KB per ASIN; the right move is to add a new extract tool, not to inline-decode it.
