# Statistical Methods Reference

## Descriptive Statistics

```python
# Central tendency
mean = df['col'].mean()
median = df['col'].median()
mode = df['col'].mode()[0]

# Dispersion
std = df['col'].std()
var = df['col'].var()
iqr = df['col'].quantile(0.75) - df['col'].quantile(0.25)
cv = std / mean  # coefficient of variation — useful for comparing variability across different scales

# Percentiles
percentiles = df['col'].quantile([0.05, 0.25, 0.5, 0.75, 0.95])
```

## Distribution Analysis

```python
skew = df['col'].skew()       # >0 right-skewed (long tail to right), <0 left-skewed
kurt = df['col'].kurtosis()   # >0 heavy tails (outlier-prone), <0 light tails

# Histogram-style binning
counts, bins = np.histogram(df['col'].dropna(), bins=20)
```

Skewness matters for choosing the right summary statistic: for right-skewed data (common in sales/revenue), median is more representative than mean.

## Outlier Detection

### IQR Method (robust, works well for skewed data)
```python
Q1, Q3 = df['col'].quantile([0.25, 0.75])
IQR = Q3 - Q1
lower = Q1 - 1.5 * IQR
upper = Q3 + 1.5 * IQR
outliers = df[(df['col'] < lower) | (df['col'] > upper)]
print(f"Outliers: {len(outliers)} rows ({len(outliers)/len(df)*100:.1f}%)")
```

### Z-Score Method (assumes roughly normal distribution)
```python
from scipy.stats import zscore
df['z'] = zscore(df['col'].dropna())
outliers = df[df['z'].abs() > 3]
```

Use IQR for skewed data (most Amazon metrics). Use z-score when data is roughly symmetric.

## Trend Detection

### Linear Trend
```python
import numpy as np
x = np.arange(len(df))
slope, intercept = np.polyfit(x, df['metric'], 1)
# slope > 0: upward, < 0: downward
# Multiply slope by len(df) for total change over the period
print(f"Trend: {slope:+.2f} per period, total change: {slope * len(df):+.2f}")
```

### Week-over-Week / Period-over-Period
```python
df['wow'] = df['metric'].pct_change() * 100  # percentage change
print(df[['date', 'metric', 'wow']].tail(8).to_string())
```

### Moving Average (smoothing noise)
```python
df['ma_7'] = df['metric'].rolling(7).mean()   # 7-day moving average
df['ma_30'] = df['metric'].rolling(30).mean()  # 30-day moving average
```

## Correlation

### Pearson (linear relationships)
```python
corr = df['col_a'].corr(df['col_b'])
# |corr| > 0.7: strong, 0.3-0.7: moderate, <0.3: weak
```

### Spearman (rank-based, better for non-linear monotonic relationships)
```python
corr = df['col_a'].corr(df['col_b'], method='spearman')
```

### Correlation Matrix (all numeric columns)
```python
print(df.select_dtypes(include='number').corr().to_string())
```

Use Spearman when you suspect the relationship exists but isn't linear (e.g., price vs BSR — lower price generally means better rank, but not linearly).

## Significance Testing

### Two-Sample T-Test (comparing two independent groups)
```python
from scipy import stats
t_stat, p_value = stats.ttest_ind(group_a, group_b)
print(f"p-value: {p_value:.4f} — {'significant' if p_value < 0.05 else 'not significant'} at 95%")
```
Use when: comparing metrics between two different products, campaigns, or segments.

### Paired T-Test (same items, different time periods)
```python
t_stat, p_value = stats.ttest_rel(before, after)
```
Use when: before/after comparison on the same ASINs (e.g., did a price change affect conversion?).

### Mann-Whitney U (non-parametric — no normality assumption)
```python
u_stat, p_value = stats.mannwhitneyu(group_a, group_b, alternative='two-sided')
```
Use when: small samples or heavily skewed data where t-test assumptions don't hold.

### Chi-Squared (categorical associations)
```python
contingency = pd.crosstab(df['category_a'], df['category_b'])
chi2, p_value, dof, expected = stats.chi2_contingency(contingency)
```
Use when: testing whether two categorical variables are related (e.g., does fulfillment method affect return rate?).

## Choosing the Right Test

| Scenario | Test | Why |
|----------|------|-----|
| Compare two groups, large samples | t-test | assumes normality, but robust with n>30 |
| Compare two groups, small/skewed | Mann-Whitney U | no normality assumption |
| Before/after on same items | Paired t-test | accounts for item-level variation |
| Categorical association | Chi-squared | tests independence of categories |
| Trend over time | Linear regression (polyfit) | gives slope + direction |
| Relationship between variables | Pearson or Spearman | linear vs rank-based |
