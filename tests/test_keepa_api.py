"""Unit tests for Keepa CSV decoding helpers and extract tools.

Regression coverage for the bug where _price_from_csv was used for rank /
review-count / offer-count fields, dividing them by 100. These are raw
integers in Keepa CSVs, not cents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.tools import keepa_api


def test_int_from_csv_returns_raw_integer():
    # CSV layout: [time, value, time, value, ...]; we read the last value.
    assert keepa_api._int_from_csv([0, 18592], 2) == 18592
    assert keepa_api._int_from_csv([0, 1740], 2) == 1740


def test_int_from_csv_returns_none_for_missing_or_inactive():
    assert keepa_api._int_from_csv(None, 2) is None
    assert keepa_api._int_from_csv([], 2) is None
    assert keepa_api._int_from_csv([0, -1], 2) is None


def test_price_from_csv_still_divides_by_100():
    # Sanity: price helper unchanged — 4997 cents → $49.97
    assert keepa_api._price_from_csv([0, 4997], 2) == pytest.approx(49.97)


def _write_cache(tmp_path: Path, asin: str, product: dict) -> None:
    """Write a fake cached product file the extract tools can read."""
    cache_dir = tmp_path / "keepa_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{asin}.json").write_text(json.dumps(product))


def test_extract_stats_returns_raw_rank_and_review_count(tmp_path, monkeypatch):
    asin = "B0BTZ24S9W"
    csv = [None] * 18
    csv[3] = [0, 18592]   # sales rank
    csv[17] = [0, 1740]   # review count
    product = {"csv": csv, "_cached_at": 9_999_999_999, "monthlySold": 50}
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_stats(asin)

    assert result["status"] == "success"
    assert result["sales_rank"] == 18592, "BSR must be raw integer, not divided by 100"
    assert result["review_count"] == 1740, "Review count must be raw integer, not divided by 100"


def test_extract_offers_returns_raw_offer_counts(tmp_path, monkeypatch):
    asin = "B0BTZ24S9W"
    csv = [None] * 36
    csv[11] = [0, 12]     # count_new
    csv[12] = [0, 0]      # count_used (treated as inactive at -1; here 0 → None)
    csv[34] = [0, 8]      # count_new_fba
    csv[35] = [0, 4]      # count_new_fbm
    product = {"csv": csv, "_cached_at": 9_999_999_999}
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_offers(asin)

    assert result["status"] == "success"
    counts = result["offer_counts"]
    assert counts["new_total"] == 12
    assert counts["new_fba"] == 8
    assert counts["new_fbm"] == 4
