# Keepa Data Shape — Quick Reference Answers

## (a) What does CSV index 18 contain?

**`BUY_BOX_SHIPPING`** — the buy box price including shipping.

Like all Keepa price CSVs, it's stored as a flat `[time, value, time, value, ...]` array, where times are Keepa minutes (since 2011-01-01 00:00 UTC) and values are integer cents. A value of `-1` at a timestamp means there was no qualifying buy box offer at that moment.

## (b) What does it mean when a value in a price CSV is -1?

**"No offer / unavailable at that timestamp."**

Inside a Keepa CSV array, every value entry represents the price (in integer cents) at the paired time. `-1` is Keepa's sentinel for "there was no listing/offer of that type at this point in time" — e.g., no buy box winner, no qualifying FBA offer, no Amazon stock, etc. Treat `-1` as a gap, not a real price; never plot or average it as if it were $-0.01.

(Note: `-1` is also used in some non-CSV product fields like `packageWeight`, `numberOfItems`, etc., to mean "unavailable" — same sentinel, same meaning.)

## (c) How is the rating field stored — what number do I divide by to get stars?

CSV index 16 (`RATING`) stores the star rating as **stars × 10**, as an integer.

**Divide by 10** to get the human-readable star value. Example: a stored value of `45` means 4.5 stars.

## (d) What is productType=5 and why does it matter?

`productType=5` marks a **variation parent ASIN** — a hub that aggregates child variations (size/color/etc.) but is not itself a buyable listing.

Why it matters: parent ASINs have **no useful market data**. No prices, no sales rank, no offers, no `monthlySold`. If you call `keepa_fetch_product` on a parent and try to extract pricing or sales, you'll get all-null prices and effectively empty results. The skill explicitly says to always check `productType` before trusting price/rank fields — and if you detect a parent, either ask the user for a child ASIN or use `keepa_product_finder` with `hasParentASIN: false` to get only buyable products.

For reference: `productType=0` is a standard, buyable product (the normal case). Other non-zero values indicate special listings (downloadable, ebook, etc.) and should also be treated with skepticism.

## (e) For the BR (Brazil) marketplace, what domain ID would I pass to keepa_fetch_product?

**`domain=12`** for `amazon.com.br` (Brazil).

Caveat from the skill: BR is supported by Keepa's API, but the toolset documents it as **"not verified in this toolset."** The default-supported, verified domains are 1–11 (US, GB, DE, FR, JP, CA, IT, ES, IN, MX; with `7` reserved/deprecated and unusable). So passing `domain=12` should work, but the skill flags it as untested — worth a smoke test on a known-good Brazilian ASIN before relying on it for production research.
