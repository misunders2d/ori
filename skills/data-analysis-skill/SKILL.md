---
name: data-analysis-skill
description: "How to analyze data files with pandas/numpy/scipy — statistical analysis, weighted aggregation, and Amazon business intelligence. Use this skill whenever asked to analyze CSV/Excel files, compute metrics from SP-API reports, SQP data, advertising reports, H10 keyword exports, BigQuery results, or any data that requires proper statistical methodology. Also use when combining data across time periods or segments — naive averaging of rates and ratios is a common trap this skill prevents."
---

# Data Analysis Skill

You run analysis code via `analyze_data(file_path, code)`. The file is pre-loaded as `df` (DataFrame). Available: `pd`, `numpy`, `scipy.stats`, `statistics`, `collections`, `re`. Print results to return them.

## First Rule: Inspect Before Analyzing

Never assume column names or data types. Always start with:
```python
print("Shape:", df.shape)
print("Columns:", df.columns.tolist())
print("Types:", df.dtypes.to_string())
print(df.head())
```

This matters because Amazon reports change column names across versions, and files from different agents may have unexpected structures.

## The Weighted Aggregation Principle

This is the single most important concept in this skill. When combining data across time periods, segments, or categories:

- **Additive metrics** (units, revenue, spend, clicks) — safe to sum directly
- **Rate/ratio metrics** (conversion rate, ACoS, CTR, avg price) — MUST be recomputed from raw components

The reason: a rate is `A/B`. The correct aggregate is `sum(A) / sum(B)`, never `mean(A/B)`. Arithmetic averaging gives equal weight to each row regardless of volume, producing misleading results.

Read `references/weighted-aggregation.md` for the full explanation with examples — including the classic multi-week SQP trap and the multi-variation pricing gotcha.

## Amazon-Specific Patterns

Amazon data has domain-specific gotchas that generic statistics won't catch. Read `references/amazon-analytics.md` for detailed patterns covering:

- **SQP reports**: share percentages, 24-hour attribution, branded vs unbranded segmentation
- **Advertising**: ACoS/ROAS/TACoS computation, attribution lag, campaign-level vs keyword-level
- **Pricing**: multi-variation weighted average, Buy Box analysis
- **H10 keyword data**: rank exclusion rules (0/306 = not ranked), volume-weighted ranking
- **Inventory**: days of supply, sell-through rate

## Statistical Methods

Read `references/statistical-methods.md` for the full toolkit:
- Descriptive stats (mean, median, mode, std, IQR, coefficient of variation)
- Distribution analysis (skewness, kurtosis, percentiles)
- Outlier detection (IQR method, z-score)
- Trend detection (linear regression, week-over-week)
- Correlation (Pearson, Spearman)
- Significance testing (t-test, paired t-test, Mann-Whitney, chi-squared)

## Code Snippets

Read `references/code-snippets.md` for ready-to-paste pandas code blocks covering common analysis tasks.

## When to Ask for Clarification

The cost of running the wrong analysis far exceeds the cost of one clarifying question. Ask before proceeding when:

- **Aggregation method is ambiguous** — "average price" could be simple or weighted (by what?)
- **Time period is unclear** — "last month" vs "last 30 days" vs "last 4 weeks" produce different results
- **You can't tell if a column is a rate or a raw count** — averaging a pre-computed rate column is wrong
- **Multiple attribution windows may be in play** — 7-day vs 14-day ad attribution changes the numbers
- **The comparison metric isn't specified** — "compare these products" needs a specific axis

## Output Guidelines

- Format numbers clearly: currency with `$`, percentages with `%`, large numbers with commas
- For results other agents need, write to scratchpad via `scratchpad_write`
- Output is truncated at 15,000 chars — for large results, summarize or write to scratchpad
- When reporting rates/ratios, always show the raw components too (e.g., "ACoS 22% ($1,100 spend / $5,000 revenue)")

## Live References

- [Pandas Documentation](https://pandas.pydata.org/docs/)
- [SciPy Statistics](https://docs.scipy.org/doc/scipy/reference/stats.html)
- [Amazon SP-API Report Types](https://developer-docs.amazon.com/sp-api/docs/report-type-values)
- [Amazon Advertising API](https://advertising.amazon.com/API/docs/en-us/)
- [Helium 10 Knowledge Base](https://help.helium10.com/)
