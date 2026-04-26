"""Helium10 keyword analysis tools — analyze Cerebro and Magnet CSV/XLSX exports.

Accepts uploaded H10 export files and runs structured keyword analyses:
power keywords, gaps, trends, scoring, long-tail opportunities, and
listing placement recommendations.
"""

import logging
import os

import pandas as pd
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_UPLOADS_DIR = os.path.abspath("./tmp/uploads")

# Column name normalization — H10 exports have inconsistent casing/spacing
_COLUMN_MAP = {
    "keyword phrase": "keyword",
    "search volume": "search_volume",
    "search volume trend": "sv_trend",
    "cerebro iq score": "iq_score",
    "magnet iq score": "iq_score",
    "cpr": "cpr",
    "competing products": "competing_products",
    "sponsored asins": "sponsored_asins",
    "organic rank": "organic_rank",
    "title density": "title_density",
    "word count": "word_count",
    "keyword sales": "keyword_sales",
    "match type": "match_type",
    "amazon choice": "amazon_choice",
    "position (rank)": "position_rank",
    "relative rank": "relative_rank",
    "competitor rank (avg)": "competitor_rank_avg",
    "aba total click rate": "aba_click_rate",
    "sponsored rank (avg)": "sponsored_rank_avg",
}


def _load_h10_file(file_path: str) -> tuple[pd.DataFrame | None, str | None]:
    """Load and normalize an H10 export file."""
    abs_path = os.path.abspath(file_path)
    if not abs_path.startswith(_UPLOADS_DIR):
        return None, f"Access denied. Only files in {_UPLOADS_DIR} can be analyzed."
    if not os.path.isfile(abs_path):
        return None, f"File not found: {file_path}"

    ext = os.path.splitext(abs_path)[1].lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(abs_path, engine="openpyxl")
        elif ext == ".csv":
            df = pd.read_csv(abs_path)
        else:
            return None, f"Unsupported file type: {ext}. Use .csv or .xlsx."
    except Exception as e:
        return None, f"Failed to read file: {e}"

    # Normalize column names
    df.columns = df.columns.str.strip()
    rename = {}
    for col in df.columns:
        key = col.lower().strip()
        if key in _COLUMN_MAP:
            rename[col] = _COLUMN_MAP[key]
    df = df.rename(columns=rename)

    # Coerce numeric columns
    for col in ["search_volume", "sv_trend", "iq_score", "cpr", "competing_products",
                "sponsored_asins", "organic_rank", "title_density", "word_count",
                "keyword_sales", "competitor_rank_avg", "position_rank"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df, None


def _detect_format(df: pd.DataFrame) -> str:
    """Detect whether this is a Cerebro or Magnet export."""
    cols = set(df.columns)
    if "organic_rank" in cols or "competitor_rank_avg" in cols:
        return "cerebro"
    return "magnet"


def _df_to_records(df: pd.DataFrame, max_rows: int = 50) -> list[dict]:
    """Convert DataFrame to list of dicts, capped at max_rows. NaN → None for valid JSON."""
    records = df.head(max_rows).to_dict(orient="records")
    for record in records:
        for k, v in record.items():
            if isinstance(v, float) and (pd.isna(v) or v != v):
                record[k] = None
    return records


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def analyze_keywords(
    file_path: str,
    min_search_volume: int = 500,
    min_iq_score: float = 2.0,
    max_cpr: int = 0,
    top_n: int = 30,
    tool_context: ToolContext = None,
) -> dict:
    """Analyze a Helium10 Cerebro or Magnet CSV/XLSX export for power keywords.

    Returns top keywords ranked by a composite opportunity score that factors in
    search volume, IQ score, trend, competition, and CPR feasibility.

    Args:
        file_path: Path to the uploaded H10 export file.
        min_search_volume: Minimum monthly search volume to include (default 500).
        min_iq_score: Minimum Cerebro/Magnet IQ score (default 2.0).
        max_cpr: Maximum CPR (units to rank). 0 = no limit.
        top_n: Number of top keywords to return (default 30).
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    fmt = _detect_format(df)
    total_keywords = len(df)

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if "search_volume" in df.columns:
        mask &= df["search_volume"] >= min_search_volume
    if "iq_score" in df.columns:
        mask &= df["iq_score"] >= min_iq_score
    if max_cpr > 0 and "cpr" in df.columns:
        mask &= df["cpr"] <= max_cpr

    filtered = df[mask].copy()

    if filtered.empty:
        return {
            "status": "success",
            "total_keywords": total_keywords,
            "filtered": 0,
            "message": "No keywords match the filters. Try lowering min_search_volume or min_iq_score.",
        }

    # Compute opportunity score
    filtered["opportunity_score"] = _compute_score(filtered)
    filtered = filtered.sort_values("opportunity_score", ascending=False)

    # Build result columns
    result_cols = ["keyword", "search_volume", "iq_score", "cpr", "competing_products",
                   "sv_trend", "title_density", "organic_rank", "opportunity_score"]
    result_cols = [c for c in result_cols if c in filtered.columns]

    return {
        "status": "success",
        "format": fmt,
        "total_keywords": total_keywords,
        "filtered": len(filtered),
        "top_keywords": _df_to_records(filtered[result_cols], top_n),
        "summary": {
            "avg_search_volume": round(filtered["search_volume"].mean()) if "search_volume" in filtered.columns else None,
            "avg_iq_score": round(filtered["iq_score"].mean(), 1) if "iq_score" in filtered.columns else None,
            "avg_cpr": round(filtered["cpr"].mean()) if "cpr" in filtered.columns else None,
        },
    }


def find_keyword_gaps(
    file_path: str,
    min_search_volume: int = 300,
    your_max_rank: int = 100,
    competitor_max_rank: int = 50,
    top_n: int = 30,
    tool_context: ToolContext = None,
) -> dict:
    """Find keywords where competitors rank well but you don't (keyword gaps).

    Requires a Cerebro multi-ASIN export. Your ASIN should be the first one searched.
    Gaps are keywords where your organic rank is missing or > your_max_rank, while
    competitors have an average rank <= competitor_max_rank.

    Args:
        file_path: Path to the uploaded Cerebro multi-ASIN export.
        min_search_volume: Minimum search volume for gap keywords (default 300).
        your_max_rank: Your rank must be worse than this to count as a gap (default 100).
        competitor_max_rank: Competitor avg rank must be better than this (default 50).
        top_n: Number of top gaps to return (default 30).
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    total = len(df)

    # For gap analysis we need organic_rank (your ASIN) and competitor_rank_avg
    has_organic = "organic_rank" in df.columns
    has_competitor = "competitor_rank_avg" in df.columns
    has_position = "position_rank" in df.columns

    # Determine your rank column
    your_rank_col = None
    if has_organic:
        your_rank_col = "organic_rank"
    elif has_position:
        your_rank_col = "position_rank"

    if not your_rank_col and not has_competitor:
        return {
            "status": "error",
            "message": "This doesn't appear to be a multi-ASIN Cerebro export. "
                       "Need 'Organic Rank' or 'Position (Rank)' and 'Competitor Rank (avg)' columns. "
                       "Run Cerebro with your ASIN + competitors and export again.",
        }

    mask = pd.Series(True, index=df.index)
    if "search_volume" in df.columns:
        mask &= df["search_volume"] >= min_search_volume

    # Your ASIN doesn't rank (NaN or > threshold)
    if your_rank_col:
        mask &= (df[your_rank_col].isna()) | (df[your_rank_col] > your_max_rank)

    # Competitors DO rank
    if has_competitor:
        mask &= df["competitor_rank_avg"].notna() & (df["competitor_rank_avg"] <= competitor_max_rank)

    gaps = df[mask].copy()

    if gaps.empty:
        return {
            "status": "success",
            "total_keywords": total,
            "gaps_found": 0,
            "message": "No keyword gaps found with these filters. Your ASIN may already cover the top keywords, "
                       "or try lowering min_search_volume.",
        }

    # Score gaps by volume * inverse competitor rank
    if "search_volume" in gaps.columns and has_competitor:
        gaps["gap_priority"] = gaps["search_volume"] * (1 / gaps["competitor_rank_avg"].clip(lower=1))
        gaps = gaps.sort_values("gap_priority", ascending=False)
    elif "search_volume" in gaps.columns:
        gaps = gaps.sort_values("search_volume", ascending=False)

    result_cols = ["keyword", "search_volume", your_rank_col, "competitor_rank_avg",
                   "iq_score", "cpr", "competing_products", "sv_trend"]
    result_cols = [c for c in result_cols if c and c in gaps.columns]

    return {
        "status": "success",
        "total_keywords": total,
        "gaps_found": len(gaps),
        "top_gaps": _df_to_records(gaps[result_cols], top_n),
    }


def find_trending_keywords(
    file_path: str,
    min_trend: float = 20.0,
    min_search_volume: int = 300,
    top_n: int = 30,
    tool_context: ToolContext = None,
) -> dict:
    """Find keywords with rising search volume trends.

    Args:
        file_path: Path to the uploaded H10 export file.
        min_trend: Minimum Search Volume Trend % to qualify as trending (default 20).
        min_search_volume: Minimum search volume (default 300).
        top_n: Number of top trending keywords to return (default 30).
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    if "sv_trend" not in df.columns:
        return {"status": "error", "message": "No 'Search Volume Trend' column found in this export."}

    mask = df["sv_trend"] >= min_trend
    if "search_volume" in df.columns:
        mask &= df["search_volume"] >= min_search_volume

    trending = df[mask].copy()
    trending = trending.sort_values("sv_trend", ascending=False)

    result_cols = ["keyword", "search_volume", "sv_trend", "iq_score", "cpr",
                   "competing_products", "organic_rank"]
    result_cols = [c for c in result_cols if c in trending.columns]

    return {
        "status": "success",
        "total_keywords": len(df),
        "trending_count": len(trending),
        "top_trending": _df_to_records(trending[result_cols], top_n),
    }


def find_long_tail_opportunities(
    file_path: str,
    min_words: int = 3,
    max_search_volume: int = 2000,
    min_search_volume: int = 100,
    min_iq_score: float = 2.0,
    top_n: int = 30,
    tool_context: ToolContext = None,
) -> dict:
    """Find long-tail keyword opportunities — lower volume, less competition, higher conversion.

    Args:
        file_path: Path to the uploaded H10 export file.
        min_words: Minimum word count in keyword phrase (default 3).
        max_search_volume: Maximum search volume — long-tail is typically under 2000 (default 2000).
        min_search_volume: Minimum search volume to be worth targeting (default 100).
        min_iq_score: Minimum IQ score (default 2.0).
        top_n: Number of results to return (default 30).
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    # Use word_count column if available, otherwise compute from keyword
    if "word_count" not in df.columns and "keyword" in df.columns:
        df["word_count"] = df["keyword"].astype(str).str.split().str.len()

    mask = pd.Series(True, index=df.index)
    if "word_count" in df.columns:
        mask &= df["word_count"] >= min_words
    if "search_volume" in df.columns:
        mask &= df["search_volume"].between(min_search_volume, max_search_volume)
    if "iq_score" in df.columns:
        mask &= df["iq_score"] >= min_iq_score

    longtail = df[mask].copy()

    if "iq_score" in longtail.columns:
        longtail = longtail.sort_values("iq_score", ascending=False)
    elif "search_volume" in longtail.columns:
        longtail = longtail.sort_values("search_volume", ascending=False)

    result_cols = ["keyword", "search_volume", "iq_score", "cpr", "competing_products",
                   "word_count", "title_density", "sv_trend"]
    result_cols = [c for c in result_cols if c in longtail.columns]

    return {
        "status": "success",
        "total_keywords": len(df),
        "long_tail_count": len(longtail),
        "opportunities": _df_to_records(longtail[result_cols], top_n),
    }


def keyword_score_report(
    file_path: str,
    min_search_volume: int = 100,
    top_n: int = 50,
    tool_context: ToolContext = None,
) -> dict:
    """Generate a scored report of ALL keywords with placement recommendations.

    Each keyword gets an opportunity score (0-100) and a recommended placement:
    - Title (score >= 75): Primary keywords for the listing title
    - Bullets (score 50-74): Secondary keywords for bullet points
    - Backend (score 25-49): Backend search terms
    - PPC Only (score < 25): Test via ads before investing in listing

    Args:
        file_path: Path to the uploaded H10 export file.
        min_search_volume: Minimum search volume to include (default 100).
        top_n: Number of top scored keywords to return (default 50).
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    fmt = _detect_format(df)

    mask = pd.Series(True, index=df.index)
    if "search_volume" in df.columns:
        mask &= df["search_volume"] >= min_search_volume

    scored = df[mask].copy()
    if scored.empty:
        return {"status": "success", "total_keywords": len(df), "scored": 0,
                "message": "No keywords above the minimum search volume threshold."}

    scored["opportunity_score"] = _compute_score(scored)
    scored["placement"] = scored["opportunity_score"].apply(_score_to_placement)
    scored = scored.sort_values("opportunity_score", ascending=False)

    # Placement distribution
    placement_dist = scored["placement"].value_counts().to_dict()

    result_cols = ["keyword", "search_volume", "iq_score", "cpr", "competing_products",
                   "sv_trend", "opportunity_score", "placement"]
    result_cols = [c for c in result_cols if c in scored.columns]

    return {
        "status": "success",
        "format": fmt,
        "total_keywords": len(df),
        "scored": len(scored),
        "placement_distribution": placement_dist,
        "top_keywords": _df_to_records(scored[result_cols], top_n),
    }


def keyword_summary(
    file_path: str,
    tool_context: ToolContext = None,
) -> dict:
    """Get a quick overview of an H10 keyword export — column detection, stats, and top keywords.

    Use this as a first step to understand what's in the file before running deeper analysis.

    Args:
        file_path: Path to the uploaded H10 export file.
    """
    df, err = _load_h10_file(file_path)
    if err:
        return {"status": "error", "message": err}

    fmt = _detect_format(df)
    stats = {"total_keywords": len(df), "format": fmt, "columns_detected": list(df.columns)}

    if "search_volume" in df.columns:
        sv = df["search_volume"].dropna()
        stats["search_volume"] = {
            "min": int(sv.min()) if len(sv) else 0,
            "max": int(sv.max()) if len(sv) else 0,
            "mean": round(sv.mean()) if len(sv) else 0,
            "median": round(sv.median()) if len(sv) else 0,
        }
    if "iq_score" in df.columns:
        iq = df["iq_score"].dropna()
        stats["iq_score"] = {
            "min": round(float(iq.min()), 1) if len(iq) else 0,
            "max": round(float(iq.max()), 1) if len(iq) else 0,
            "mean": round(float(iq.mean()), 1) if len(iq) else 0,
        }
    if "sv_trend" in df.columns:
        trend = df["sv_trend"].dropna()
        stats["trending_up"] = int((trend > 0).sum())
        stats["trending_down"] = int((trend < 0).sum())
    if "organic_rank" in df.columns:
        ranked = df["organic_rank"].dropna()
        stats["you_rank_for"] = len(ranked)
        stats["you_dont_rank"] = int(df["organic_rank"].isna().sum())
    if "word_count" in df.columns:
        stats["avg_word_count"] = round(df["word_count"].mean(), 1)

    # Top 10 by volume
    if "search_volume" in df.columns and "keyword" in df.columns:
        top10 = df.nlargest(10, "search_volume")[["keyword", "search_volume"]].to_dict(orient="records")
        stats["top_10_by_volume"] = top10

    return {"status": "success", **stats}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _compute_score(df: pd.DataFrame) -> pd.Series:
    """Compute a 0-100 opportunity score for each keyword."""
    score = pd.Series(0.0, index=df.index)

    # Search Volume (25% weight) — log scale to avoid mega-volume domination
    if "search_volume" in df.columns:
        sv = df["search_volume"].clip(lower=1)
        import numpy as np
        sv_log = np.log10(sv)
        sv_norm = (sv_log / sv_log.max() * 100).fillna(0)
        score += sv_norm * 0.25

    # IQ Score (20% weight)
    if "iq_score" in df.columns:
        iq_max = df["iq_score"].max()
        if iq_max > 0:
            score += (df["iq_score"] / iq_max * 100).fillna(0) * 0.20

    # Trend (15% weight) — centered at 50, positive trends score higher
    if "sv_trend" in df.columns:
        trend_norm = (df["sv_trend"].clip(-100, 200) + 100) / 300 * 100
        score += trend_norm.fillna(50) * 0.15

    # Competition (20% weight, inverted — fewer competitors = higher score)
    if "competing_products" in df.columns:
        cp_max = df["competing_products"].max()
        if cp_max > 0:
            comp_score = (1 - df["competing_products"] / cp_max) * 100
            score += comp_score.fillna(50) * 0.20

    # CPR feasibility (20% weight, inverted — lower CPR = easier to rank)
    if "cpr" in df.columns:
        cpr_max = df["cpr"].max()
        if cpr_max > 0:
            cpr_score = (1 - df["cpr"].clip(lower=0) / cpr_max) * 100
            score += cpr_score.fillna(50) * 0.20

    return score.round(1)


def _score_to_placement(score: float) -> str:
    """Map opportunity score to recommended listing placement."""
    if score >= 75:
        return "Title"
    elif score >= 50:
        return "Bullets"
    elif score >= 25:
        return "Backend"
    else:
        return "PPC Only"
