"""Auto-detect metric types in a DataFrame — identifies which columns are
rates/ratios (unsafe to average) vs additive (safe to sum).

Usage: paste into analyze_data() code block.

This helps avoid the weighted aggregation trap by flagging columns
that look like rates before you accidentally average them.
"""


# Known rate/ratio column name patterns (case-insensitive)
_RATE_PATTERNS = [
    'rate', 'ratio', 'share', 'percentage', '%', 'pct',
    'acos', 'roas', 'tacos', 'ctr', 'cvr', 'cpc', 'cpm',
    'average price', 'avg price', 'mean price',
    'conversion', 'buy box',
]

# Known additive column name patterns
_ADDITIVE_PATTERNS = [
    'count', 'total', 'sum', 'units', 'quantity', 'qty',
    'revenue', 'sales', 'spend', 'cost', 'budget',
    'clicks', 'impressions', 'orders', 'sessions', 'views',
    'returns', 'refunds',
]


def classify_columns(df):
    """Classify numeric columns as likely rate/ratio or additive.

    Returns:
        dict with keys 'rates' and 'additive', each a list of column names
    """
    rates = []
    additive = []
    uncertain = []

    for col in df.select_dtypes(include='number').columns:
        col_lower = col.lower()

        if any(p in col_lower for p in _RATE_PATTERNS):
            rates.append(col)
        elif any(p in col_lower for p in _ADDITIVE_PATTERNS):
            additive.append(col)
        else:
            # Heuristic: if all values are between 0 and 1 (or 0-100),
            # it's probably a rate
            vals = df[col].dropna()
            if len(vals) > 0:
                if vals.between(0, 1).all():
                    rates.append(col)
                elif vals.between(0, 100).all() and vals.mean() < 100:
                    uncertain.append(col)  # could be rate expressed as %
                else:
                    additive.append(col)
            else:
                uncertain.append(col)

    return {
        'rates': rates,
        'additive': additive,
        'uncertain': uncertain,
    }


def print_classification(df):
    """Print a human-readable classification of all numeric columns."""
    result = classify_columns(df)

    print("=== Column Classification ===\n")

    if result['rates']:
        print("RATE/RATIO columns (do NOT average — use weighted aggregation):")
        for col in result['rates']:
            print(f"  - {col}")

    if result['additive']:
        print("\nADDITIVE columns (safe to sum/average):")
        for col in result['additive']:
            print(f"  - {col}")

    if result['uncertain']:
        print("\nUNCERTAIN (inspect manually — could be rates expressed as %):")
        for col in result['uncertain']:
            vals = df[col].dropna()
            print(f"  - {col}  (range: {vals.min():.2f} - {vals.max():.2f}, mean: {vals.mean():.2f})")

    print(f"\nTotal: {len(result['rates'])} rates, {len(result['additive'])} additive, {len(result['uncertain'])} uncertain")


# Example usage in analyze_data():
#
# print_classification(df)
