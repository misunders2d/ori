# Amazon Analytics — Real-World Examples & Methodology

## 1. Search Query Performance (SQP) Analysis

### The ICAP Funnel
SQP tracks: **Impressions -> Clicks -> Add-to-Cart -> Purchases** at the search-term + ASIN level.
For each stage Amazon provides: total count, your brand's count, your brand's share %.

### Click Share vs Impression Share Ratio
When Click Share far exceeds Impression Share (e.g. 29% clicks vs 0.36% impressions = 81x multiplier),
your product converts strongly when seen but lacks visibility. Action: increase PPC spend on that keyword.

### Multi-Week Aggregation (CRITICAL)
Share percentages are ratios computed per time period. You CANNOT average shares across weeks.

```python
# A keyword has 100K total impressions in week 1 (you got 10% share)
# and 10K in week 2 (you got 50% share)
# WRONG: average share = (10% + 50%) / 2 = 30%
# RIGHT: (10000 + 5000) / (100000 + 10000) = 13.6%

agg = df.groupby('search_query').agg({
    'total_impressions': 'sum',
    'brand_impressions': 'sum',
    'total_clicks': 'sum',
    'brand_clicks': 'sum',
    'total_purchases': 'sum',
    'brand_purchases': 'sum',
}).reset_index()
agg['impression_share'] = agg['brand_impressions'] / agg['total_impressions']
agg['click_share'] = agg['brand_clicks'] / agg['total_clicks']
agg['purchase_share'] = agg['brand_purchases'] / agg['total_purchases']
```

### SQP Gotchas
- **24-hour attribution window**: customer must search AND purchase within 24 hours.
  SQP will systematically undercount vs Seller Central sales.
- **Branded vs unbranded**: Amazon does NOT separate these. You must manually segment
  queries containing your brand name. Branded queries convert 3-10x higher and will
  skew your overall metrics if mixed.
- **Data availability**: Weekly reports process Mon/Thu at 11:00 AM UTC. Monthly on 3rd/18th.

Sources:
- https://parker-lambert.com/how-to-use-amazons-search-query-performance-report-and-what-it-actually-tells-you/
- https://incrementumdigital.com/blog/performance-growth/understanding-search-query-performance-sqp-on-amazon-a-comprehensive-guide/
- https://ecomclips.com/blog/in-depth-analysis-with-amazon-sqp-brand-analytics-the-ultimate-guide/

---

## 2. Advertising Analysis (ACoS / ROAS / TACoS)

### Core Formulas
```python
acos = ad_spend / ad_revenue  # percentage of revenue spent on ads
roas = ad_revenue / ad_spend  # revenue per dollar of ad spend (= 1/ACoS)
tacos = total_ad_spend / total_revenue  # includes organic revenue
break_even_acos = unit_margin / selling_price  # max ACoS before losing money
```

### TACoS: The True Efficiency Metric
Declining TACoS while ad spend is stable = advertising is lifting organic rank ("halo effect").
Rising TACoS while ACoS is stable = organic sales are eroding.

### ACoS Targets by Business Stage
| Stage | Target ACoS | Rationale |
|-------|------------|-----------|
| New launch | 40-50% | Buy velocity and ranking |
| Growth | Near break-even | Balance visibility with profit |
| Mature | 15-20% | 5-10 points below margin |
| Branded keywords | Lowest possible | Already captured demand |
| Competitor targeting | Higher tolerance | Conquest has lower CVR |

### Bid Analysis: CPC-to-Bid Ratio
- CPC / Bid > 0.6: Market is competitive, adjust cautiously
- CPC / Bid < 0.6: You're overbidding, trim aggressively

### Aggregation Example
```python
# Campaign-level ACoS — MUST weight properly
# WRONG: df['acos'].mean()
# RIGHT:
total_acos = df['spend'].sum() / df['revenue'].sum()

# Per-keyword ACoS with minimum spend filter (avoid noise)
kw = df.groupby('keyword').agg({'spend': 'sum', 'revenue': 'sum', 'clicks': 'sum', 'orders': 'sum'})
kw['acos'] = kw['spend'] / kw['revenue']
kw['cvr'] = kw['orders'] / kw['clicks']
kw['cpc'] = kw['spend'] / kw['clicks']
# Filter out low-data keywords
kw = kw[kw['clicks'] >= 20]
```

### Ads Gotchas
- **Conversion lag**: Amazon attributes sales 7-14 days after click. Daily bid changes chase noise.
- **ACoS spike is often retail, not PPC**: stockout, lost Buy Box, pricing, or rating dip.
- **Blended metrics mask problems**: always break down by match type, placement, and variant.
- **2025 benchmarks**: Average CPC $0.99-$1.14, average CVR ~10%, strong ACoS 15-20%.

Sources:
- https://www.atom11.co/blog/amazon-ads-acos-optimization-all-use-cases-covered
- https://canopymanagement.com/ultimate-guide-to-acos-and-tacos/
- https://perpetua.io/blog-amazon-tacos/

---

## 3. Helium 10 Keyword Gap Analysis

### Cerebro IQ Score
```
Cerebro IQ Score = f(estimated search volume / number of competing products)
```
Minimum threshold: **3+** for viable keywords.

### Keyword Gap Methodology
1. Input 5-10 top competitor ASINs into Cerebro
2. Apply filters:
   - Min Search Volume: 1,000+ monthly
   - Cerebro IQ Score: 3+
   - Position Rank: 10-50 (realistic entry points)
   - Phrase Count: 3+ words (long-tail = higher conversion)
   - Sponsored Keyword: Yes (proven money keywords)
   - Advanced Rank: at least 3 of 5 competitors rank organically
3. Gap = keywords where 3+ competitors rank but you have NO ranking

### Opportunity Keywords
Keywords where only 1-2 of 5+ competitors rank in positions 1-15.
Underexploited terms with proven demand but low competition.

### Analysis in pandas
```python
# H10 Cerebro export analysis
df = pd.read_csv(file_path)

# Filter out "not ranked" positions
df = df[~df['Organic Rank'].isin([0, 306])]

# Weighted average rank (by search volume)
weighted_rank = (df['Organic Rank'] * df['Search Volume']).sum() / df['Search Volume'].sum()

# Find gaps: competitor ranks but you don't
gap_keywords = df[
    (df['Competitor 1 Rank'].between(1, 50)) &
    (df['Your Rank'].isna() | (df['Your Rank'] > 300))
]
```

Sources:
- https://kb.helium10.com/hc/en-us/articles/360034481473
- https://kb.helium10.com/hc/en-us/articles/32836629378715

---

## 4. Multi-Variation Pricing Analysis

### Weighted Average Price
When a parent ASIN has child variations at different prices:
```python
# WRONG: df['price'].mean()
# RIGHT: weight by units sold
weighted_price = (df['price'] * df['units_sold']).sum() / df['units_sold'].sum()

# Per-variation contribution
df['revenue'] = df['price'] * df['units_sold']
df['revenue_share'] = df['revenue'] / df['revenue'].sum()
```

### Price Elasticity Signal
Track BSR response to price changes over time:
```python
df['price_change'] = df['price'].pct_change()
df['bsr_change'] = df['bsr'].pct_change()
# Negative correlation = price elastic (lower price -> better rank)
elasticity = df['price_change'].corr(df['bsr_change'])
```

### Buy Box Analysis
Amazon considers: price, fulfillment method, seller metrics, shipping speed.
Track Buy Box win rate by price point to find the sweet spot.

Sources:
- https://gotrellis.com/resources/blog/amazon-pricing-strategy/
- https://www.ecomengine.com/blog/amazon-competitive-pricing
