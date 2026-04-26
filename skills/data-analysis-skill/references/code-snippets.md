# Data Analysis — Code Snippets for analyze_data()

These are ready-to-use pandas code blocks for `analyze_data(file_path, code)`.
The file is pre-loaded as `df`. Print results to return them.

## Inspect Any File (always do this first)

```python
print("Shape:", df.shape)
print("\nColumns:", df.columns.tolist())
print("\nData types:")
print(df.dtypes)
print("\nFirst 5 rows:")
print(df.head())
print("\nNull counts:")
print(df.isnull().sum())
```

## SQP Multi-Week Aggregation

```python
# Combine weekly SQP exports — recompute shares from raw counts
agg = df.groupby('Search Query').agg({
    'Search Query Volume': 'sum',
    'Impressions: Total Count': 'sum',
    'Impressions: Brand Count': 'sum',
    'Clicks: Total Count': 'sum',
    'Clicks: Brand Count': 'sum',
    'Cart Adds: Total Count': 'sum',
    'Cart Adds: Brand Count': 'sum',
    'Purchases: Total Count': 'sum',
    'Purchases: Brand Count': 'sum',
}).reset_index()

agg['Impression Share'] = agg['Impressions: Brand Count'] / agg['Impressions: Total Count']
agg['Click Share'] = agg['Clicks: Brand Count'] / agg['Clicks: Total Count']
agg['Purchase Share'] = agg['Purchases: Brand Count'] / agg['Purchases: Total Count']
agg['CTR'] = agg['Clicks: Total Count'] / agg['Impressions: Total Count']
agg['Conversion Rate'] = agg['Purchases: Total Count'] / agg['Clicks: Total Count']

# Top 20 by purchase volume
top = agg.nlargest(20, 'Purchases: Brand Count')
print(top[['Search Query', 'Search Query Volume', 'Impression Share', 'Click Share', 'Purchase Share', 'CTR', 'Conversion Rate']].to_string())
```

## SQP Click-to-Impression Ratio (Opportunity Finder)

```python
# Keywords where click share >> impression share = high conversion but low visibility
agg = df.groupby('Search Query').agg({
    'Impressions: Brand Count': 'sum',
    'Impressions: Total Count': 'sum',
    'Clicks: Brand Count': 'sum',
    'Clicks: Total Count': 'sum',
}).reset_index()

agg['imp_share'] = agg['Impressions: Brand Count'] / agg['Impressions: Total Count']
agg['click_share'] = agg['Clicks: Brand Count'] / agg['Clicks: Total Count']
agg['ratio'] = agg['click_share'] / agg['imp_share'].replace(0, float('nan'))

# Filter: meaningful volume + high ratio
opportunities = agg[
    (agg['Clicks: Brand Count'] >= 10) &
    (agg['ratio'] > 3)
].sort_values('ratio', ascending=False)

print(f"Found {len(opportunities)} high-conversion low-visibility keywords:")
print(opportunities[['Search Query', 'imp_share', 'click_share', 'ratio']].head(20).to_string())
```

## Advertising — Campaign Performance with Proper ACoS

```python
import numpy as np

# Per-campaign metrics (weighted correctly)
camp = df.groupby('Campaign Name').agg({
    'Spend': 'sum',
    '7 Day Total Sales': 'sum',
    'Clicks': 'sum',
    'Impressions': 'sum',
    '7 Day Total Orders': 'sum',
}).reset_index()

camp['ACoS'] = camp['Spend'] / camp['7 Day Total Sales'].replace(0, np.nan)
camp['ROAS'] = camp['7 Day Total Sales'] / camp['Spend'].replace(0, np.nan)
camp['CPC'] = camp['Spend'] / camp['Clicks'].replace(0, np.nan)
camp['CVR'] = camp['7 Day Total Orders'] / camp['Clicks'].replace(0, np.nan)
camp['CTR'] = camp['Clicks'] / camp['Impressions'].replace(0, np.nan)

# Overall weighted ACoS (NOT camp['ACoS'].mean())
total_acos = camp['Spend'].sum() / camp['7 Day Total Sales'].sum()
print(f"Overall ACoS: {total_acos:.2%}")
print(f"Overall ROAS: {1/total_acos:.2f}")

print("\nTop campaigns by spend:")
print(camp.sort_values('Spend', ascending=False).head(15).to_string())
```

## Advertising — Keyword Bid Efficiency

```python
import numpy as np

kw = df.groupby('Targeting').agg({
    'Spend': 'sum',
    '7 Day Total Sales': 'sum',
    'Clicks': 'sum',
    'Impressions': 'sum',
    '7 Day Total Orders': 'sum',
}).reset_index()

kw['ACoS'] = kw['Spend'] / kw['7 Day Total Sales'].replace(0, np.nan)
kw['CPC'] = kw['Spend'] / kw['Clicks'].replace(0, np.nan)
kw['CVR'] = kw['7 Day Total Orders'] / kw['Clicks'].replace(0, np.nan)

# Only keywords with enough data
kw = kw[kw['Clicks'] >= 20]

# Profitable keywords (ACoS < 30%)
profitable = kw[kw['ACoS'] < 0.30].sort_values('7 Day Total Sales', ascending=False)
print(f"Profitable keywords (ACoS < 30%): {len(profitable)}")
print(profitable.head(15).to_string())

# Bleeding keywords (high spend, no/low sales)
bleeding = kw[(kw['Spend'] > 50) & ((kw['ACoS'] > 0.50) | kw['ACoS'].isna())]
print(f"\nBleeding keywords (spend>$50, ACoS>50%): {len(bleeding)}")
print(bleeding.sort_values('Spend', ascending=False).head(15).to_string())
```

## Multi-Variation Weighted Price

```python
# Child variation pricing analysis
df['revenue'] = df['price'] * df['units_sold']
weighted_price = df['revenue'].sum() / df['units_sold'].sum()
simple_avg = df['price'].mean()

print(f"Weighted avg price: ${weighted_price:.2f}")
print(f"Simple avg price:   ${simple_avg:.2f}  (MISLEADING)")
print(f"Difference:         ${abs(weighted_price - simple_avg):.2f}")

print("\nPer-variation breakdown:")
df['revenue_share'] = df['revenue'] / df['revenue'].sum() * 100
print(df[['variation', 'price', 'units_sold', 'revenue', 'revenue_share']].sort_values('revenue', ascending=False).to_string())
```

## Inventory Health — Days of Supply

```python
import numpy as np

# Per-ASIN days of supply
df['daily_run_rate'] = df['units_sold_30d'] / 30
df['days_of_supply'] = df['available_qty'] / df['daily_run_rate'].replace(0, np.nan)

# Risk categories
df['risk'] = np.where(df['days_of_supply'] < 14, 'URGENT',
             np.where(df['days_of_supply'] < 30, 'LOW STOCK',
             np.where(df['days_of_supply'] > 180, 'EXCESS', 'OK')))

print("Inventory risk summary:")
print(df['risk'].value_counts())
print("\nURGENT (< 14 days supply):")
urgent = df[df['risk'] == 'URGENT'].sort_values('days_of_supply')
print(urgent[['asin', 'sku', 'available_qty', 'daily_run_rate', 'days_of_supply']].to_string())
```

## Trend Detection — Weekly Sales

```python
import numpy as np

# Parse dates and aggregate weekly
df['date'] = pd.to_datetime(df['date'])
weekly = df.resample('W', on='date')['units'].sum().reset_index()

# Linear trend
x = np.arange(len(weekly))
slope, intercept = np.polyfit(x, weekly['units'], 1)
trend = 'upward' if slope > 0 else 'downward'

print(f"Trend: {trend} ({slope:+.1f} units/week)")
print(f"\nWeek-over-week changes:")
weekly['wow_change'] = weekly['units'].pct_change() * 100
print(weekly[['date', 'units', 'wow_change']].tail(8).to_string())
```

## Outlier Detection

```python
from scipy.stats import zscore

# Z-score method
df['z_score'] = zscore(df['metric_col'].dropna())
outliers = df[df['z_score'].abs() > 3]

print(f"Found {len(outliers)} outliers (|z| > 3):")
print(outliers.to_string())

# IQR method (more robust for skewed data)
Q1 = df['metric_col'].quantile(0.25)
Q3 = df['metric_col'].quantile(0.75)
IQR = Q3 - Q1
iqr_outliers = df[(df['metric_col'] < Q1 - 1.5*IQR) | (df['metric_col'] > Q3 + 1.5*IQR)]
print(f"\nIQR outliers: {len(iqr_outliers)}")
```

## Before/After Significance Test

```python
from scipy import stats

# Compare two periods (e.g., before/after a price change)
before = df[df['period'] == 'before']['conversion_rate']
after = df[df['period'] == 'after']['conversion_rate']

t_stat, p_value = stats.ttest_ind(before, after)
print(f"Before mean: {before.mean():.4f}")
print(f"After mean:  {after.mean():.4f}")
print(f"Change:      {(after.mean() - before.mean()) / before.mean() * 100:+.1f}%")
print(f"p-value:     {p_value:.4f}")
print(f"Significant: {'YES' if p_value < 0.05 else 'NO'} (at 95% confidence)")
```
