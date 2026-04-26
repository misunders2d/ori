# Helium 10 Keyword Analysis — Worked Examples

## Example 1: Cerebro Competitive Gap Analysis

```
User: "I uploaded a Cerebro export for 5 competitors. Find keywords they rank for but we don't."

1. keyword_summary(file_path="./tmp/uploads/cerebro_export.csv")
   → Returns: format (Cerebro), keyword count, top keywords by volume

2. analyze_keywords(file_path="./tmp/uploads/cerebro_export.csv")
   → Returns: keyword distribution, volume tiers, average positions

3. find_keyword_gaps(
     file_path="./tmp/uploads/cerebro_export.csv",
     min_competitors=3,
     max_own_rank=306
   )
   → Returns: keywords where 3+ competitors rank in top 50
     but our ASIN has no ranking (rank 0 or 306 = not ranked)

4. Write findings to scratchpad for DataAnalyst to chart:
   scratchpad_write("keyword-gaps", gap_report)
```

## Example 2: Magnet Keyword Discovery

```
User: "I uploaded a Magnet export for 'bamboo bed sheets'. Find the best opportunities."

1. keyword_summary(file_path="./tmp/uploads/magnet_export.csv")
   → Auto-detects Magnet format from column headers

2. find_long_tail_opportunities(file_path="./tmp/uploads/magnet_export.csv")
   → Returns: 3+ word phrases with high volume but low competition

3. keyword_score_report(file_path="./tmp/uploads/magnet_export.csv")
   → Returns: keywords ranked by composite score (volume, competition, relevance)
```

## Example 3: Trending Keywords

```
User: "Which keywords are growing in search volume?"

1. find_trending_keywords(file_path="./tmp/uploads/cerebro_export.csv")
   → Returns: keywords with increasing search volume trend
   → Note: requires multi-period data or H10 trend indicators
```

## Cerebro Analysis via DataAnalyst (Large Exports)

When the H10 export is too large for the keyword tools, pass to DataAnalyst:

```python
# Code for analyze_data() — Cerebro gap analysis
import numpy as np

# Filter out non-ranked positions
ranked = df[~df['Organic Rank'].isin([0, 306])].copy()

# Competitor columns (auto-detect: columns with "Rank" in name)
rank_cols = [c for c in df.columns if 'Rank' in c and 'Organic' not in c and 'Sponsored' not in c]

# Keywords where 3+ competitors rank top 50 but we don't
df['competitor_count'] = df[rank_cols].apply(
    lambda row: sum(1 for r in row if 0 < r <= 50), axis=1
)
our_rank_col = 'Organic Rank'  # adjust based on actual column name
gaps = df[
    (df['competitor_count'] >= 3) &
    ((df[our_rank_col] == 0) | (df[our_rank_col] > 300))
].sort_values('Search Volume', ascending=False)

print(f"Found {len(gaps)} keyword gaps:")
print(gaps[['Keyword', 'Search Volume', 'competitor_count', our_rank_col]].head(30).to_string())

# Weighted average competitor position (by search volume)
weighted_pos = (ranked['Organic Rank'] * ranked['Search Volume']).sum() / ranked['Search Volume'].sum()
print(f"\nWeighted avg organic position: {weighted_pos:.1f}")
```

## Key Column Names (Common Variations)

Cerebro exports may use different column names depending on H10 version:
- `Keyword` or `Keyword Phrase`
- `Search Volume` or `Estimated Search Volume`
- `Cerebro IQ Score` or `IQ Score`
- `Organic Rank` (your ASIN) — 0 or 306 means not ranked
- `Sponsored Rank` — 0 means no sponsored position
- `Competing Products` or `Number of Competing Products`
- Competitor ASIN columns vary by export configuration

Always inspect columns first with `keyword_summary` or `analyze_data` before assuming names.
