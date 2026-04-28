# Keepa `product_finder` selection JSON

## Important caveat about coverage

The keepa-skill (SKILL.md + references/research-examples.md) only documents three `keepa_product_finder` selection keys:

- `categoryIds` (list of ints)
- `salesRankRange` ([min, max])
- `current_COUNT_REVIEWS_min` (int)

The skill does **not** document the following pieces you asked for:

- The exact key for a **Buy Box price** range filter
- The exact key for excluding **variation parents** (the skill mentions `productType=5` for parents under "gotchas" but does not specify the `product_finder` filter key/value to exclude them)
- **Sort** syntax for `product_finder`
- The **result-count** / page-size argument
- The **price units** Keepa uses inside `product_finder`

Per the skill's error-handling mandate ("never fabricate"), I will not invent key names that aren't in the skill. Below I give you the JSON for the parts the skill *does* cover, and flag the rest as TODOs that need to be confirmed against the live [Keepa API docs](https://keepa.com/api/) before you call the tool.

## Selection JSON (only the documented filters)

```json
{
  "categoryIds": [1063252],
  "salesRankRange": [1, 5000],
  "current_COUNT_REVIEWS_min": 100
}
```

You would pass this as the `selection` argument to `keepa_product_finder`, with `domain=1` for amazon.com:

```
keepa_product_finder(
  domain=1,
  selection={
    "categoryIds": [1063252],
    "salesRankRange": [1, 5000],
    "current_COUNT_REVIEWS_min": 100
  }
)
```

## Filters the skill does not cover (need to be added from the live Keepa API reference)

| Requested filter | Status in this skill |
|---|---|
| Buy Box price between $15 and $50 | Not in skill — needs the buy-box price-range key from Keepa API docs |
| Only standard products (exclude variation parents, `productType != 5`) | Not in skill — skill only mentions `productType=5` as a gotcha, not the `product_finder` filter form |
| Sort by sales rank ascending | Not in skill — sort syntax not documented |
| Return 25 results | Not in skill — `perPage`/limit arg not documented |

If you want a JSON that includes those four, I'd need to either (a) be allowed to consult the live Keepa API docs, or (b) have the skill extended with a `product_finder` selection-schema reference. I won't guess key names, because a wrong key in `product_finder` is silently ignored and you'd think your filter applied when it didn't.

## One real-world gotcha about price units in this filter

Even though the skill doesn't cover the buy-box price filter directly, there is one universal Keepa price-units gotcha that absolutely applies to any price-range filter inside `product_finder`:

**Keepa expresses all prices in integer cents, not dollars.** A filter for "$15 to $50" must be passed as `1500` to `5000`, not `15` to `50`. If you pass `15`/`50`, Keepa will interpret that as 15 to 50 cents and your finder will return effectively nothing (or a wildly wrong slice of the catalog) — and it won't error, it will just silently return the wrong products. Same convention applies to every price field returned by the extraction tools (`keepa_extract_pricing`, `keepa_extract_history` on price metrics, etc.) — divide by 100 when displaying to humans.

A secondary corollary: Keepa uses `-1` (which becomes `-0.01` after the cents conversion) as the sentinel for "no data / out of stock." If you set a min price filter you can accidentally exclude items whose buy box is currently unavailable — usually fine for finder queries, but worth knowing.
