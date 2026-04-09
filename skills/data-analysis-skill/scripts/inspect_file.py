"""Inspect a data file — shape, columns, types, sample rows, null counts.

Usage: python inspect_file.py <file_path>
Or paste into analyze_data(file_path, code) — df is pre-loaded.
"""
import sys
import pandas as pd

def inspect(file_path=None, df=None):
    if df is None:
        ext = file_path.rsplit('.', 1)[-1].lower()
        if ext in ('xlsx', 'xls'):
            sheets = pd.read_excel(file_path, sheet_name=None, engine='openpyxl')
            print(f"Excel file with {len(sheets)} sheet(s): {list(sheets.keys())}")
            df = next(iter(sheets.values()))
            print(f"Using first sheet: '{next(iter(sheets.keys()))}'")
        else:
            df = pd.read_csv(file_path)

    print(f"\nShape: {df.shape[0]} rows x {df.shape[1]} columns")
    print(f"\nColumns and types:")
    for col in df.columns:
        non_null = df[col].count()
        null_pct = (1 - non_null / len(df)) * 100
        print(f"  {col:40s} {str(df[col].dtype):10s} {non_null}/{len(df)} ({null_pct:.0f}% null)")

    print(f"\nFirst 5 rows:")
    print(df.head().to_string())

    # Detect numeric columns and show basic stats
    numeric = df.select_dtypes(include='number')
    if len(numeric.columns) > 0:
        print(f"\nNumeric summary:")
        print(numeric.describe().to_string())

    # Detect date-like columns
    for col in df.columns:
        if df[col].dtype == 'object':
            sample = df[col].dropna().head(5)
            try:
                pd.to_datetime(sample)
                print(f"\nPossible date column: '{col}' (sample: {sample.iloc[0]})")
            except (ValueError, TypeError):
                pass

if __name__ == '__main__':
    inspect(file_path=sys.argv[1])
