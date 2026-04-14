# Product Fees Reference

## Tool

### `sp_get_fees_estimate(asin, price, is_fba=True, shipping_price=0, currency="USD")`
Estimates what Amazon will take in fees if this ASIN sells at the given price. Returns a breakdown of referral, FBA fulfillment, variable closing fee, per-item fee, and total.

## When to use this

This is the tool for **profitability math**. Whenever the question is "if I price this at $X, what's my take-home?", `sp_get_fees_estimate` is what fills in the Amazon-side costs. Common flows:

- **Pricing a new product:** pair with `sp_get_competitive_pricing` to see the current market, estimate fees at a few candidate prices, subtract your COGS, pick the price that hits the margin target.
- **Repricing an existing SKU:** estimate at current price and at the proposed new price; quantify the profit delta.
- **Evaluating a sourcing opportunity:** caller has a landed cost and a target price; `estimate → margin = price - (COGS + fees + shipping)` tells them whether it's worth buying.

## The `is_fba` flag matters

Setting `is_fba=True` includes FBA fulfillment fees — pick/pack/weight handling that Amazon charges when they ship. For merchant-fulfilled listings, `is_fba=False` gives you only the referral side, and you need to layer your own shipping/label cost on top. **Wrong flag = silently wrong margin math.** Confirm with the user which fulfillment channel applies before quoting numbers.

## Example

```
# ASIN B01EXAMPLE at $24.99 via FBA
sp_get_fees_estimate(asin="B01EXAMPLE", price=24.99, is_fba=True)

# Same ASIN, merchant-fulfilled, seller charges $4.99 shipping
sp_get_fees_estimate(asin="B01EXAMPLE", price=24.99, is_fba=False, shipping_price=4.99)
```

## Gotcha

Amazon's response structure wraps the estimate inside `FeesEstimateResult → FeesEstimate → TotalFeesEstimate`. The tool surfaces the inner `estimate` object but preserves Amazon's nested field names — `FeeType`, `FeeAmount`, `FinalFee`, etc. Don't try to flatten or rename them in conversation with the user; use them as-is when quoting specific fees.

## Live references

- [Product Fees API](https://developer-docs.amazon.com/sp-api/docs/product-fees-api-v0-reference)
- [Official model: product-fees](https://github.com/amzn/selling-partner-api-models/tree/main/models/product-fees-api-model)
