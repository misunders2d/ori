"""Unit tests for Keepa CSV decoding helpers and extract tools.

Regression coverage for the bug where _price_from_csv was used for rank /
review-count / offer-count fields, dividing them by 100. These are raw
integers in Keepa CSVs, not cents.
"""

from __future__ import annotations

import asyncio
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


def test_extract_stats_decodes_real_probe_values(tmp_path, monkeypatch):
    """Uses values pulled from a live Keepa response on B0BTZ5HGMB.

    Ground truth captured by scripts/keepa_probe.py:
      csv[3]  last value 18592   → BSR is 18,592
      csv[16] last value 44      → rating is 4.4 stars (Keepa stores 0-50)
      csv[17] last value 1740    → review count is 1,740
    """
    asin = "B0BTZ5HGMB"
    csv = [None] * 18
    csv[3] = [0, 18592]
    csv[16] = [0, 44]
    csv[17] = [0, 1740]
    product = {"csv": csv, "_cached_at": 9_999_999_999, "monthlySold": 100}
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_stats(asin)

    assert result["status"] == "success"
    assert result["sales_rank"] == 18592, "BSR must be raw integer, not divided by 100"
    assert result["review_count"] == 1740, "Review count must be raw integer, not divided by 100"
    assert result["rating"] == pytest.approx(4.4), "Rating must be raw/10 (44 → 4.4), not raw/1000"


def test_extract_pricing_picks_lightning_deal_as_best_offer(tmp_path, monkeypatch):
    """When a Lightning Deal is active and lower than all other channels,
    extract_pricing must surface it in `prices.lightning_deal` AND select it
    as `best_offer`. Previously LD wasn't in _CSV_MAP and would be missed."""
    asin = "B01M16WBW1"
    csv = [None] * 19
    csv[1] = [0, 2499]                              # NEW $24.99
    csv[8] = [0, 1899]                              # LIGHTNING_DEAL $18.99 (active)
    csv[18] = [0, 2499, 0]                          # buy box $24.99 (3-tuple)
    product = {"csv": csv, "_cached_at": 9_999_999_999}
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_pricing(asin)

    assert result["status"] == "success"
    assert result["prices"]["lightning_deal"] == pytest.approx(18.99)
    assert result["prices"]["new_3p"] == pytest.approx(24.99)
    assert result["best_offer"] == pytest.approx(18.99)
    assert result["best_offer_source"] == "lightning_deal"


def test_extract_pricing_decodes_buy_box_from_shipping_csv(tmp_path, monkeypatch):
    """csv[18] is BUY_BOX_SHIPPING — a 3-tuple CSV [time, price, shipping].

    Ground truth from probe of B0BTZ5HGMB: last triplet was [8057304, 4097, 0]
    meaning $40.97 buy-box price with free shipping. Reading with
    items_per_row=2 (the old bug) returns the shipping value (0) and treats
    it as inactive, masking the real buy-box price.
    """
    asin = "B0BTZ5HGMB"
    csv = [None] * 19
    # NEW (index 1) — 2-tuple [time, price] for sanity check that other indices still work
    csv[1] = [0, 4097]
    # BUY_BOX (index 18) — 3-tuple [time, price, shipping] — two historic + one current
    csv[18] = [
        7000000, 5897, 0,
        7500000, 4500, 0,
        8057304, 4097, 0,
    ]
    product = {"csv": csv, "_cached_at": 9_999_999_999}
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_pricing(asin)

    assert result["status"] == "success"
    assert result["prices"]["new_3p"] == pytest.approx(40.97)
    assert result["prices"]["buy_box"] == pytest.approx(40.97), (
        "Buy box must read the price slot (csv[-2]) of the 3-tuple, not the shipping slot (csv[-1])"
    )


def test_extract_pricing_surfaces_limited_time_deal_and_promotions(tmp_path, monkeypatch):
    """B0BHX9121W (PUPIBOO pee pads) had an active 'Limited time deal' badge
    and a Subscribe & Save reference price. Keepa exposes both via the
    `deals` and `promotions` non-CSV fields. extract_pricing must pass
    them through so the agent can tell the customer the price is
    discounted, not flat."""
    asin = "B0BHX9121W"
    csv = [None] * 19
    csv[1] = [0, 2463]                              # NEW $24.63
    csv[18] = [0, 2463, 0]                          # buy box $24.63 (3-tuple)
    product = {
        "csv": csv,
        "_cached_at": 9_999_999_999,
        "deals": [
            {"accessType": "ALL", "badge": "Limited time deal", "dealType": "LIMITED_TIME_DEAL"},
        ],
        "promotions": [
            {
                "amount": 2999,
                "discountPercent": 0,
                "sellerId": "A2T4WIBJIHSJGX",
                "snsBulkDiscountPercent": None,
                "type": "SNS",
            },
        ],
    }
    _write_cache(tmp_path, asin, product)
    monkeypatch.setattr(keepa_api, "_CACHE_DIR", str(tmp_path / "keepa_cache"))

    result = keepa_api.keepa_extract_pricing(asin)

    assert result["status"] == "success"
    assert result["active_deals"] == [
        {"type": "LIMITED_TIME_DEAL", "badge": "Limited time deal", "audience": "ALL"}
    ], "extract_pricing must surface the Limited time deal badge"
    assert len(result["promotions"]) == 1
    promo = result["promotions"][0]
    assert promo["type"] == "SNS"
    assert promo["amount_dollars"] == pytest.approx(29.99), (
        "SnS reference price ($29.99) is the 'typical price' Amazon strikes through during the deal"
    )


def test_bulk_extract_row_pulls_only_requested_fields():
    """The row extractor that backs keepa_bulk_query should:
      - always include asin
      - return None for fields the product is missing
      - decode rank/review-count as raw integers (not /100)
      - decode buy_box from the 3-tuple shipping CSV
      - surface active deal badge from product.deals
    """
    csv = [None] * 19
    csv[1] = [0, 2463]                              # NEW $24.63
    csv[3] = [0, 18592]                             # BSR
    csv[16] = [0, 44]                               # rating raw 44 → 4.4 stars
    csv[17] = [0, 1740]                             # review count
    csv[18] = [0, 2463, 0]                          # buy box $24.63 (3-tuple)
    product = {
        "asin": "B0TESTASIN",
        "csv": csv,
        "title": "Test product",
        "monthlySold": 100,
        "deals": [
            {"accessType": "ALL", "badge": "Limited time deal", "dealType": "LIMITED_TIME_DEAL"},
        ],
    }

    fields = [
        "asin",
        "title",
        "review_count",
        "rating",
        "sales_rank",
        "monthly_sold",
        "buy_box",
        "active_deal",
        "prime_exclusive",       # missing from product → None
    ]
    row = keepa_api._extract_row(product, fields)

    assert row["asin"] == "B0TESTASIN"
    assert row["title"] == "Test product"
    assert row["review_count"] == 1740
    assert row["rating"] == pytest.approx(4.4)
    assert row["sales_rank"] == 18592
    assert row["monthly_sold"] == 100
    assert row["buy_box"] == pytest.approx(24.63)
    assert row["active_deal"] == "Limited time deal"
    assert row["prime_exclusive"] is None


def test_bulk_extract_row_returns_variation_asins_for_parent():
    """Querying a parent ASIN should expose the full child list via
    `variation_asins`. Falls back to the deprecated `variationCSV` string
    if the structured `variations` array is missing."""
    parent = {
        "asin": "B0DCP3LJHC",
        "csv": [],
        "title": "Mellanni Duvet Cover Set",
        "variations": [
            {"asin": "B07PSK5GG2", "attributes": []},
            {"asin": "B07GQ38TP8", "attributes": []},
            {"asin": "B07MS7QWTG", "attributes": []},
        ],
    }
    row = keepa_api._extract_row(parent, ["asin", "variation_asins", "variation_count"])
    assert row["variation_asins"] == ["B07PSK5GG2", "B07GQ38TP8", "B07MS7QWTG"]
    assert row["variation_count"] == 3

    # Fallback to variationCSV string when `variations` is not populated.
    parent_legacy = {"asin": "B0LEGACY", "csv": [], "variationCSV": "B07A,B07B,B07C"}
    row = keepa_api._extract_row(parent_legacy, ["asin", "variation_asins", "variation_count"])
    assert row["variation_asins"] == ["B07A", "B07B", "B07C"]
    assert row["variation_count"] == 3

    # Children return null for both — not a parent.
    child = {"asin": "B07CHILD", "csv": [], "parentAsin": "B0PARENT"}
    row = keepa_api._extract_row(child, ["asin", "variation_asins", "variation_count"])
    assert row["variation_asins"] is None
    assert row["variation_count"] is None


def test_bulk_extract_row_returns_parent_asin_for_variation_children():
    """Variation children carry a parentAsin pointer. Surfacing it in
    bulk_query lets the agent pivot to the parent for review/rating data
    (which Amazon aggregates at the parent level, not per child)."""
    product = {
        "asin": "B07PSK5GG2",
        "csv": [None] * 18,  # no review_count / rating data — typical for children
        "title": "Mellanni Duvet Cover Set (King, Laced Sky Blue)",
        "parentAsin": "B07PARENT1",
        "parentTitle": "Mellanni Duvet Cover Set",
    }
    row = keepa_api._extract_row(product, ["asin", "parent_asin", "parent_title", "review_count"])
    assert row["asin"] == "B07PSK5GG2"
    assert row["parent_asin"] == "B07PARENT1"
    assert row["parent_title"] == "Mellanni Duvet Cover Set"
    assert row["review_count"] is None


def test_bulk_query_rejects_unknown_field_names(monkeypatch):
    """keepa_bulk_query should refuse the call up-front when the caller
    passes a field name the toolset doesn't know — better than silently
    returning rows with missing columns."""
    monkeypatch.setenv("KEEPA_API_KEY", "fake-key")

    result = asyncio.run(keepa_api.keepa_bulk_query(
        asins="B01,B02",
        fields="asin,review_count,not_a_real_field,bogus",
    ))

    assert result["status"] == "error"
    assert "not_a_real_field" in result["message"]
    assert "bogus" in result["message"]


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
