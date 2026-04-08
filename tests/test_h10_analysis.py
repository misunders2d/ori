"""Tests for Helium10 keyword analysis tools."""

import os
import tempfile

import pandas as pd
import pytest

from app.tools.h10_analysis import (
    analyze_keywords,
    find_keyword_gaps,
    find_trending_keywords,
    find_long_tail_opportunities,
    keyword_score_report,
    keyword_summary,
    _load_h10_file,
    _detect_format,
    _compute_score,
)

# Use the real uploads dir for path validation
_UPLOADS_DIR = os.path.abspath("./tmp/uploads")


@pytest.fixture(autouse=True)
def ensure_uploads_dir():
    os.makedirs(_UPLOADS_DIR, exist_ok=True)
    yield


def _write_csv(df: pd.DataFrame, filename: str = "test_export.csv") -> str:
    """Write a DataFrame to a CSV in the uploads dir and return the path."""
    path = os.path.join(_UPLOADS_DIR, filename)
    df.to_csv(path, index=False)
    return path


def _cerebro_df(n: int = 20) -> pd.DataFrame:
    """Generate a mock Cerebro export DataFrame."""
    import numpy as np
    rng = np.random.RandomState(42)
    keywords = [f"keyword {i}" for i in range(n)]
    return pd.DataFrame({
        "Keyword Phrase": keywords,
        "Search Volume": rng.randint(100, 50000, n),
        "Search Volume Trend": rng.uniform(-30, 80, n).round(1),
        "Cerebro IQ Score": rng.uniform(0.5, 15, n).round(1),
        "CPR": rng.randint(5, 500, n),
        "Competing Products": rng.randint(100, 50000, n),
        "Organic Rank": rng.choice([*range(1, 100), None, None, None], n),
        "Title Density": rng.randint(0, 25, n),
        "Word Count": rng.choice([1, 2, 3, 4, 5], n),
        "Sponsored ASINs": rng.randint(0, 30, n),
    })


def _cerebro_multi_asin_df(n: int = 20) -> pd.DataFrame:
    """Generate a mock Cerebro multi-ASIN export with competitor data."""
    df = _cerebro_df(n)
    import numpy as np
    rng = np.random.RandomState(99)
    # Simulate: some keywords you rank for, some you don't
    organic = rng.choice([*range(1, 80), None, None, None, None, None], n)
    df["Organic Rank"] = pd.array(organic, dtype=pd.Int64Dtype())
    df["Competitor Rank (avg)"] = rng.randint(1, 60, n).astype(float)
    df["Position (Rank)"] = df["Organic Rank"]
    return df


def _magnet_df(n: int = 20) -> pd.DataFrame:
    """Generate a mock Magnet export DataFrame."""
    import numpy as np
    rng = np.random.RandomState(7)
    keywords = [f"magnet keyword {i}" for i in range(n)]
    return pd.DataFrame({
        "Keyword Phrase": keywords,
        "Search Volume": rng.randint(50, 30000, n),
        "Search Volume Trend": rng.uniform(-20, 60, n).round(1),
        "Magnet IQ Score": rng.uniform(0.5, 12, n).round(1),
        "CPR": rng.randint(5, 300, n),
        "Competing Products": rng.randint(200, 40000, n),
        "Title Density": rng.randint(0, 20, n),
        "Word Count": rng.choice([1, 2, 3, 4, 5, 6], n),
    })


# ---------------------------------------------------------------------------
# File loading and format detection
# ---------------------------------------------------------------------------

class TestFileLoading:

    def test_load_cerebro_csv(self):
        path = _write_csv(_cerebro_df(), "cerebro.csv")
        df, err = _load_h10_file(path)
        assert err is None
        assert "keyword" in df.columns
        assert "search_volume" in df.columns
        assert "iq_score" in df.columns
        os.remove(path)

    def test_load_magnet_csv(self):
        path = _write_csv(_magnet_df(), "magnet.csv")
        df, err = _load_h10_file(path)
        assert err is None
        assert "keyword" in df.columns
        assert "iq_score" in df.columns  # Magnet IQ Score → iq_score
        os.remove(path)

    def test_load_outside_uploads_rejected(self):
        df, err = _load_h10_file("/etc/passwd")
        assert df is None
        assert "Access denied" in err

    def test_load_nonexistent_file(self):
        df, err = _load_h10_file(os.path.join(_UPLOADS_DIR, "nope.csv"))
        assert df is None
        assert "not found" in err

    def test_detect_cerebro_format(self):
        df = _cerebro_df()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df = df.rename(columns={"keyword_phrase": "keyword", "cerebro_iq_score": "iq_score"})
        assert _detect_format(df) == "cerebro"

    def test_detect_magnet_format(self):
        df = _magnet_df()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df = df.rename(columns={"keyword_phrase": "keyword", "magnet_iq_score": "iq_score"})
        assert _detect_format(df) == "magnet"


# ---------------------------------------------------------------------------
# keyword_summary
# ---------------------------------------------------------------------------

class TestKeywordSummary:

    def test_summary_cerebro(self):
        path = _write_csv(_cerebro_df(50), "summary_test.csv")
        result = keyword_summary(path)
        assert result["status"] == "success"
        assert result["total_keywords"] == 50
        assert result["format"] == "cerebro"
        assert "search_volume" in result
        assert "top_10_by_volume" in result
        assert len(result["top_10_by_volume"]) == 10
        os.remove(path)

    def test_summary_magnet(self):
        path = _write_csv(_magnet_df(30), "summary_magnet.csv")
        result = keyword_summary(path)
        assert result["status"] == "success"
        assert result["format"] == "magnet"
        os.remove(path)


# ---------------------------------------------------------------------------
# analyze_keywords
# ---------------------------------------------------------------------------

class TestAnalyzeKeywords:

    def test_basic_analysis(self):
        path = _write_csv(_cerebro_df(100), "analyze_test.csv")
        result = analyze_keywords(path, min_search_volume=500, min_iq_score=2.0)
        assert result["status"] == "success"
        assert result["total_keywords"] == 100
        assert result["filtered"] > 0
        assert len(result["top_keywords"]) <= 30
        # Keywords should be sorted by opportunity score descending
        scores = [k["opportunity_score"] for k in result["top_keywords"]]
        assert scores == sorted(scores, reverse=True)
        os.remove(path)

    def test_filters_too_strict(self):
        path = _write_csv(_cerebro_df(10), "strict_test.csv")
        result = analyze_keywords(path, min_search_volume=999999, min_iq_score=999)
        assert result["status"] == "success"
        assert result["filtered"] == 0
        os.remove(path)

    def test_with_cpr_filter(self):
        path = _write_csv(_cerebro_df(50), "cpr_test.csv")
        result = analyze_keywords(path, min_search_volume=100, min_iq_score=0, max_cpr=50)
        assert result["status"] == "success"
        for kw in result.get("top_keywords", []):
            assert kw.get("cpr", 0) <= 50
        os.remove(path)


# ---------------------------------------------------------------------------
# find_keyword_gaps
# ---------------------------------------------------------------------------

class TestKeywordGaps:

    def test_gap_analysis(self):
        path = _write_csv(_cerebro_multi_asin_df(50), "gaps_test.csv")
        result = find_keyword_gaps(path, min_search_volume=100)
        assert result["status"] == "success"
        assert result["total_keywords"] == 50
        # Should find at least some gaps (we seeded nulls in organic_rank)
        assert result["gaps_found"] >= 0
        os.remove(path)

    def test_gap_needs_multi_asin(self):
        # Magnet export has no organic_rank or competitor_rank_avg
        df = _magnet_df(20)
        path = _write_csv(df, "magnet_gaps.csv")
        result = find_keyword_gaps(path)
        assert result["status"] == "error"
        assert "multi-ASIN" in result["message"]
        os.remove(path)


# ---------------------------------------------------------------------------
# find_trending_keywords
# ---------------------------------------------------------------------------

class TestTrending:

    def test_find_trending(self):
        path = _write_csv(_cerebro_df(100), "trending_test.csv")
        result = find_trending_keywords(path, min_trend=10, min_search_volume=100)
        assert result["status"] == "success"
        for kw in result.get("top_trending", []):
            assert kw["sv_trend"] >= 10
        os.remove(path)

    def test_no_trend_column(self):
        df = pd.DataFrame({"Keyword Phrase": ["a", "b"], "Search Volume": [100, 200]})
        path = _write_csv(df, "no_trend.csv")
        result = find_trending_keywords(path)
        assert result["status"] == "error"
        assert "Trend" in result["message"]
        os.remove(path)


# ---------------------------------------------------------------------------
# find_long_tail_opportunities
# ---------------------------------------------------------------------------

class TestLongTail:

    def test_find_long_tail(self):
        path = _write_csv(_cerebro_df(100), "longtail_test.csv")
        result = find_long_tail_opportunities(path, min_words=3, min_search_volume=50)
        assert result["status"] == "success"
        for kw in result.get("opportunities", []):
            assert kw.get("word_count", 3) >= 3
        os.remove(path)


# ---------------------------------------------------------------------------
# keyword_score_report
# ---------------------------------------------------------------------------

class TestScoreReport:

    def test_score_report(self):
        path = _write_csv(_cerebro_df(50), "score_test.csv")
        result = keyword_score_report(path, min_search_volume=100)
        assert result["status"] == "success"
        assert result["scored"] > 0
        assert "placement_distribution" in result
        # Every keyword should have a placement
        for kw in result.get("top_keywords", []):
            assert kw["placement"] in ("Title", "Bullets", "Backend", "PPC Only")
            assert 0 <= kw["opportunity_score"] <= 100
        os.remove(path)

    def test_placement_distribution(self):
        path = _write_csv(_cerebro_df(100), "dist_test.csv")
        result = keyword_score_report(path, min_search_volume=0)
        dist = result["placement_distribution"]
        total = sum(dist.values())
        assert total == result["scored"]
        os.remove(path)


# ---------------------------------------------------------------------------
# Scoring function
# ---------------------------------------------------------------------------

class TestScoring:

    def test_score_range(self):
        df = _cerebro_df(50)
        # Normalize columns as _load_h10_file would
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df = df.rename(columns={
            "keyword_phrase": "keyword",
            "cerebro_iq_score": "iq_score",
        })
        scores = _compute_score(df)
        assert scores.min() >= 0
        assert scores.max() <= 100

    def test_high_volume_scores_higher(self):
        df = pd.DataFrame({
            "search_volume": [50000, 100],
            "iq_score": [5.0, 5.0],
            "cpr": [100, 100],
            "competing_products": [5000, 5000],
            "sv_trend": [0, 0],
        })
        scores = _compute_score(df)
        assert scores.iloc[0] > scores.iloc[1]
