"""Template engine tests — placeholder resolution, recursive walk,
special placeholders, error surfacing.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.contracts.templating import TemplateError, render


# ---------------------------------------------------------------------------
# Plain string substitution
# ---------------------------------------------------------------------------


def test_simple_key_substitution():
    out = render("hello {name}", {"name": "sergey"})
    assert out == "hello sergey"


def test_nested_dotted_path():
    state = {"news": {"top": {"title": "Big news"}}}
    out = render("Headline: {news.top.title}", state)
    assert out == "Headline: Big news"


def test_list_subscript():
    state = {"items": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}
    out = render("First: {items[0].title}, third: {items[2].title}", state)
    assert out == "First: A, third: C"


def test_multiple_placeholders_in_one_string():
    state = {"a": 1, "b": 2}
    out = render("a={a} b={b} sum stays manual ({a}+{b})", state)
    assert out == "a=1 b=2 sum stays manual (1+2)"


def test_complex_value_stringifies_as_json():
    """Dicts/lists embedded in a template render as compact JSON — so
    a reasoning step can splice the whole loader result into its user
    template without per-field plumbing."""
    state = {"items": [{"asin": "A1"}, {"asin": "A2"}]}
    out = render("data: {items}", state)
    assert out == 'data: [{"asin": "A1"}, {"asin": "A2"}]'


# ---------------------------------------------------------------------------
# Recursive walk
# ---------------------------------------------------------------------------


def test_render_walks_dicts():
    state = {"asin": "B0XYZ"}
    args = {"sql": "SELECT * FROM t WHERE asin = '{asin}'", "limit": 50}
    out = render(args, state)
    assert out == {"sql": "SELECT * FROM t WHERE asin = 'B0XYZ'", "limit": 50}


def test_render_walks_lists():
    state = {"x": 1, "y": 2}
    out = render(["a={x}", "b={y}", 42], state)
    assert out == ["a=1", "b=2", 42]


def test_render_walks_nested_structures():
    state = {"asin": "B0XYZ", "limit": 5}
    args = {
        "queries": [
            {"sql": "SELECT * FROM t WHERE asin = '{asin}'"},
            {"sql": "SELECT * FROM u LIMIT {limit}"},
        ],
        "static": True,
    }
    out = render(args, state)
    assert out["queries"][0]["sql"] == "SELECT * FROM t WHERE asin = 'B0XYZ'"
    assert out["queries"][1]["sql"] == "SELECT * FROM u LIMIT 5"
    assert out["static"] is True


# ---------------------------------------------------------------------------
# Special placeholders
# ---------------------------------------------------------------------------


def test_today_special():
    """``{today}`` resolves to YYYY-MM-DD without any state dependency."""
    out = render("As of {today}", {})
    assert out == f"As of {date.today().isoformat()}"


def test_today_minus_n_days_special():
    out = render("from {today-5d} to {today}", {})
    expected_from = (date.today() - timedelta(days=5)).isoformat()
    expected_to = date.today().isoformat()
    assert out == f"from {expected_from} to {expected_to}"


def test_today_plus_n_days_special():
    out = render("due {today+30d}", {})
    expected = (date.today() + timedelta(days=30)).isoformat()
    assert out == f"due {expected}"


def test_now_special_is_iso8601():
    out = render("ts={now}", {})
    assert out.startswith("ts=")
    # ISO format like 2026-05-11T19:30:00.123456+00:00
    ts = out[3:]
    assert "T" in ts and ts.endswith("+00:00")


def test_state_key_overrides_special_is_not_supported():
    """``{today}`` always resolves to the special value, even if the
    state happens to have a ``today`` key. Special placeholders are
    process-internal and reserved."""
    out = render("{today}", {"today": "wrong"})
    assert out == date.today().isoformat()


# ---------------------------------------------------------------------------
# Error surfacing
# ---------------------------------------------------------------------------


def test_unresolved_placeholder_raises():
    """Drift defence: a typo'd or missing-state placeholder must blow
    up the fire, not silently substitute empty string."""
    with pytest.raises(TemplateError, match="not in state"):
        render("hi {ghost}", {"name": "x"})


def test_index_out_of_range_raises():
    with pytest.raises(TemplateError, match="out of range"):
        render("{items[5]}", {"items": [1, 2]})


def test_non_int_subscript_on_list_raises():
    with pytest.raises(TemplateError, match="non-int"):
        render("{items[oops]}", {"items": [1, 2]})


def test_attempting_to_descend_into_scalar_raises():
    with pytest.raises(TemplateError, match="cannot traverse"):
        render("{count.foo}", {"count": 42})


# ---------------------------------------------------------------------------
# Non-string passthrough
# ---------------------------------------------------------------------------


def test_non_string_scalars_pass_through_unchanged():
    assert render(42, {}) == 42
    assert render(3.14, {}) == 3.14
    assert render(True, {}) is True
    assert render(None, {}) is None
