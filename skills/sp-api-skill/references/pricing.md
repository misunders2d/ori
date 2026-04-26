# Competitive Pricing Reference

## Tool

### `sp_get_competitive_pricing(asins="", skus="")`
Returns the offer landscape for up to 20 items per call. Provide *either* `asins` or `skus` (comma-separated), not both.

## What the response means

Each item returns a block with multiple price tiers. The ones you care about most:

| Field | What it is |
|---|---|
| **Your price** | Your own listing's offer price |
| **Lowest competitor price** | Lowest offer from another seller (can be FBA or FBM — check the offer metadata) |
| **Buy Box price** | The price shown on the product page — what the customer actually sees. Not always the lowest offer. |
| **Number of offers** | How many sellers are competing on this listing |
| **Landed price** | Item price + shipping — the apples-to-apples comparison number |

**Always disclose whether you're quoting landed price or just item price** when reporting to the user. Customers compare on landed price; sellers often think in item price. Mixing them silently leads to wrong decisions.

## Usage pattern

Batch aggressively: one call for 20 ASINs is one call, vs. 20 individual calls. If you have more than 20, split into chunks.

```
sp_get_competitive_pricing(asins="B000AAAAAA,B000BBBBBB,B000CCCCCC,...")
```

For ASIN-by-ASIN profitability analysis, pair with `sp_get_fees_estimate` (see `product-fees.md`) — competitive pricing tells you the market price, fees estimate tells you your cost structure.

## Live references

- [Product Pricing API](https://developer-docs.amazon.com/sp-api/docs/product-pricing-api-v0-reference)
- [Official model: product-pricing](https://github.com/amzn/selling-partner-api-models/tree/main/models/product-pricing-api-model)
