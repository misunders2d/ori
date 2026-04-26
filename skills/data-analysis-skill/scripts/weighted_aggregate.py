"""Weighted aggregation for rate/ratio metrics across groups or time periods.

Usage: paste into analyze_data() code block.

This script provides helper functions that correctly aggregate metrics
that are ratios (like conversion rate, ACoS, CTR, average price).
Simple averaging of these metrics produces wrong results because it
gives equal weight to each row regardless of volume.
"""
import numpy as np


def weighted_rate(df, numerator_col, denominator_col, group_col=None):
    """Compute a rate metric with proper weighting.

    Args:
        df: DataFrame
        numerator_col: column name for the numerator (e.g., 'orders', 'spend')
        denominator_col: column name for the denominator (e.g., 'sessions', 'revenue')
        group_col: optional column to group by before aggregating

    Returns:
        float (if no group_col) or Series (if group_col)
    """
    if group_col:
        grouped = df.groupby(group_col).agg({
            numerator_col: 'sum',
            denominator_col: 'sum',
        })
        return grouped[numerator_col] / grouped[denominator_col].replace(0, np.nan)
    else:
        total_num = df[numerator_col].sum()
        total_den = df[denominator_col].sum()
        return total_num / total_den if total_den else np.nan


def weighted_average(df, value_col, weight_col, group_col=None):
    """Compute weighted average of a value column.

    Args:
        df: DataFrame
        value_col: column to average (e.g., 'price')
        weight_col: column to weight by (e.g., 'units_sold')
        group_col: optional column to group by

    Returns:
        float (if no group_col) or Series (if group_col)
    """
    df = df.dropna(subset=[value_col, weight_col])
    if group_col:
        weighted = df.groupby(group_col).apply(
            lambda g: (g[value_col] * g[weight_col]).sum() / g[weight_col].sum()
            if g[weight_col].sum() > 0 else np.nan
        )
        return weighted
    else:
        total_weight = df[weight_col].sum()
        if total_weight == 0:
            return np.nan
        return (df[value_col] * df[weight_col]).sum() / total_weight


def aggregate_sqp(df, query_col='Search Query'):
    """Aggregate SQP data across time periods with proper share recomputation.

    Returns a DataFrame with recomputed shares from raw counts.
    """
    # Auto-detect column names (SQP reports vary)
    count_pairs = []
    for col in df.columns:
        if 'Total Count' in col:
            base = col.replace('Total Count', '').strip(': ')
            brand_col = f"{base}: Brand Count" if f"{base}: Brand Count" in df.columns else None
            if not brand_col:
                brand_col = col.replace('Total Count', 'Brand Count')
            if brand_col in df.columns:
                count_pairs.append((base, col, brand_col))

    agg_cols = {col: 'sum' for _, total, brand in count_pairs for col in [total, brand]}
    if 'Search Query Volume' in df.columns:
        agg_cols['Search Query Volume'] = 'sum'

    agg = df.groupby(query_col).agg(agg_cols).reset_index()

    for base, total, brand in count_pairs:
        share_col = f"{base} Share"
        agg[share_col] = agg[brand] / agg[total].replace(0, np.nan)

    return agg


# Example usage in analyze_data():
#
# # Weighted average price
# avg_price = weighted_average(df, 'price', 'units_sold')
# print(f"Weighted avg price: ${avg_price:.2f}")
#
# # Proper ACoS
# acos = weighted_rate(df, 'spend', 'revenue')
# print(f"ACoS: {acos:.2%}")
#
# # SQP aggregation
# sqp = aggregate_sqp(df)
# print(sqp.head(20).to_string())
