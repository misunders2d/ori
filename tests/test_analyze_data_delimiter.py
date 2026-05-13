"""analyze_data delimiter sniffing tests.

2026-05-13 incident: pulled yesterday's SP-API report, the agent fed a
tab-delimited file with `.csv` extension into `analyze_data`, pandas
exploded with ``ParserError: Expected 1 fields in line 3, saw 6``.
Fix added ``_sniff_delimiter`` + ``_read_delimited`` so any of
``.csv``/``.tsv``/``.txt`` get their delimiter detected from a sample.
"""

from __future__ import annotations

import os

import pytest

pd = pytest.importorskip("pandas")

from app.tools.analyze_data import _read_delimited, _sniff_delimiter


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_sniff_picks_tab(tmp_path):
    p = tmp_path / "tsv_with_csv_ext.csv"
    _write(p, "a\tb\tc\n1\t2\t3\n4\t5\t6\n")
    assert _sniff_delimiter(str(p)) == "\t"


def test_sniff_picks_comma(tmp_path):
    p = tmp_path / "real.csv"
    _write(p, "a,b,c\n1,2,3\n4,5,6\n")
    assert _sniff_delimiter(str(p)) == ","


def test_sniff_picks_semicolon(tmp_path):
    """Some EU-locale exports use semicolons."""
    p = tmp_path / "eu.csv"
    _write(p, "a;b;c\n1;2;3\n")
    assert _sniff_delimiter(str(p)) == ";"


def test_sniff_falls_back_to_comma_on_empty(tmp_path):
    p = tmp_path / "empty.csv"
    _write(p, "")
    assert _sniff_delimiter(str(p)) == ","


def test_read_delimited_loads_tsv_with_csv_extension(tmp_path):
    """The exact 2026-05-13 shape: TSV content + .csv extension."""
    p = tmp_path / "spapi.csv"
    _write(p, "order-id\tasin\tquantity\n111\tB00X\t1\n222\tB00Y\t3\n")
    df = _read_delimited(pd, str(p))
    assert list(df.columns) == ["order-id", "asin", "quantity"]
    assert df.iloc[0]["asin"] == "B00X"
    assert df.iloc[1]["quantity"] == 3
