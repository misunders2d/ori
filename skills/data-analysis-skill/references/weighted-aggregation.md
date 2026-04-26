# Weighted Aggregation — Why and How

## The Problem

When you average a pre-computed rate across rows, you treat every row equally regardless of volume. This produces wrong answers when rows represent different amounts of activity.

## The Formula

For any metric that is a ratio `A / B`:
```
Correct aggregate = sum(A) / sum(B)
Wrong aggregate   = mean(A / B)
```

## Example: Multi-Week Average Price

```
Week 1: 1000 units sold at $29.97
Week 2: 1 unit sold at $129.97

WRONG: ($29.97 + $129.97) / 2 = $79.97
RIGHT: ($29,970 + $129.97) / 1001 = $30.07
```

The arithmetic average says the price is $79.97. The reality is that 99.9% of units sold at $29.97. The weighted answer ($30.07) reflects this.

## Common Amazon Metrics and Their Weights

| Metric | Formula | Weight by |
|--------|---------|-----------|
| Conversion Rate | orders / sessions | sessions |
| ACoS | spend / ad revenue | ad revenue |
| ROAS | ad revenue / spend | spend |
| CTR | clicks / impressions | impressions |
| CPC | spend / clicks | clicks |
| CVR | orders / clicks | clicks |
| Average Selling Price | revenue / units | units |
| Buy Box % | BB wins / total offers | total offers |
| SQP Impression Share | brand impressions / total impressions | total impressions |
| SQP Click Share | brand clicks / total clicks | total clicks |
| SQP Purchase Share | brand purchases / total purchases | total purchases |

## Code Pattern

```python
import numpy as np

# WRONG — averaging pre-computed rates
wrong_acos = df['acos'].mean()

# RIGHT — recompute from components
right_acos = df['spend'].sum() / df['revenue'].sum()

# RIGHT — with groupby
by_campaign = df.groupby('campaign').agg({'spend': 'sum', 'revenue': 'sum'})
by_campaign['acos'] = by_campaign['spend'] / by_campaign['revenue'].replace(0, np.nan)

# Weighted average price
weighted_price = (df['price'] * df['units']).sum() / df['units'].sum()
```

## The SQP Multi-Week Trap

SQP reports provide "share" percentages per time period. These shares have different denominators each week (total market volume changes).

```
Week 1: 100,000 total impressions, you got 10% share (10,000)
Week 2:  10,000 total impressions, you got 50% share  (5,000)

WRONG average share: (10% + 50%) / 2 = 30%
RIGHT weighted share: (10,000 + 5,000) / (100,000 + 10,000) = 13.6%
```

The naive average says 30% share. The truth is 13.6%. This happens because week 1 had 10x more volume.

## When Simple Averaging IS Correct

- Averaging across individual observations of the same type (e.g., average review rating)
- When all rows represent equal weight (e.g., per-ASIN metrics where each ASIN is equally important)
- When explicitly asked for "unweighted" or "equal-weight" comparison

When in doubt, ask: "does each row represent the same amount of activity?" If no, weight.

## Helper Scripts

The `scripts/weighted_aggregate.py` file provides ready-to-use functions:
- `weighted_rate(df, numerator, denominator, group_col)` — for rate metrics
- `weighted_average(df, value, weight, group_col)` — for weighted means
- `aggregate_sqp(df)` — auto-detects SQP columns and recomputes shares

The `scripts/detect_metrics.py` file can auto-classify columns as rate vs additive.
