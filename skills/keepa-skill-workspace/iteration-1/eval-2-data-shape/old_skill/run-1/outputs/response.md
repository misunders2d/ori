# Keepa Data Shape — Quick Reference Answers

The keepa-skill in this workspace is workflow-focused (fetch → extract pattern). It deliberately abstracts the raw CSV/array shape behind extraction tools (`keepa_extract_pricing`, `keepa_extract_sales_analysis`, `keepa_extract_history`, etc.), so most low-level Keepa API field semantics are **not documented in the skill**. Only one of the five questions below is covered.

I am answering each part explicitly and flagging where the skill is silent rather than guessing.

## (a) What does CSV index 18 contain?

**Not covered by the skill.** The skill never enumerates Keepa CSV indices. It exposes history through `keepa_extract_history(asin, metric, days)` with named metrics (`amazon`, `new`, `buy_box`, `sales_rank`, `rating`, `review_count`, etc.), so callers never have to know the underlying integer index. Index 18 is not mentioned anywhere in `SKILL.md` or `references/research-examples.md`.

If you need the raw mapping, consult the live Keepa API documentation linked in the skill: <https://keepa.com/api/> (and the discussion thread <https://keepa.com/#!discuss/t/keepa-api/150>).

## (b) What does it mean when a value in a price CSV is -1?

**Not covered by the skill.** The skill never describes Keepa's sentinel values for missing/unavailable data points in price arrays. The extraction tools handle this translation internally and return cleaned summaries, so the `-1` convention is not surfaced.

I am not stating what `-1` means here because the skill does not document it and I was instructed not to guess.

## (c) How is the rating field stored — what number do I divide by to get stars?

**Not covered by the skill.** `rating` appears only as a named metric for `keepa_extract_history` and as a quick-stat in `keepa_extract_stats`. The skill does not say how the value is encoded in the raw payload or what scaling factor (if any) you must apply to convert it to a star rating. Use the extraction tools to get a properly formatted rating, or consult the Keepa API docs for the raw encoding.

## (d) What is productType=5 and why does it matter?

**Covered by the skill.** From the SKILL.md "Gotchas" section and the `references/research-examples.md` "Common Gotchas":

> **Parent ASINs have NO data.** `productType=5` means no prices, no rank, no offers. Always query child ASINs.

Why it matters in practice:
- A `productType=5` ASIN is a **variation parent** (umbrella listing for a family of child ASINs that differ by size, color, etc.).
- Pricing, BSR, offers, and sales-tier data are all empty/null on the parent — every `keepa_extract_*` call against it will be useless.
- You must enumerate the child ASINs and run the fetch → extract workflow against each child.
- Related gotcha: BSR is shared across variations, so to differentiate which child actually sells, use `monthlySold` via `keepa_extract_sales_analysis`.

## (e) For the BR (Brazil) marketplace, what domain ID would I pass to keepa_fetch_product?

**Not covered by the skill.** `keepa_fetch_product(asin)` in the skill takes only an ASIN — the documented signature does not show a `domain` parameter at all. `domain` is referenced for the discovery tools (`keepa_product_finder`, `keepa_get_bestsellers`, `keepa_get_categories`, `keepa_get_seller_info`, `keepa_get_top_sellers`) and one example uses `domain=1` (US), but the skill never enumerates the full domain-ID table and does not mention Brazil.

I am not asserting a numeric domain ID for BR because the skill does not document it. The Keepa API reference (<https://keepa.com/api/>) has the canonical domain-ID list.

---

**Summary of skill coverage on these questions:** 1 of 5 (only `productType=5`). For the other four, the skill's deliberate abstraction means you either rely on the extraction tools or go to the upstream Keepa API docs.
