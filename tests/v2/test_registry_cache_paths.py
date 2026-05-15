"""Tests for ``app.v2.registry_cache.paths``.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §5.3.

Pins:
- ``cache_path(kind)`` returns the documented filename under
  :data:`DEFAULT_CACHE_BASE`.
- ``cache_path(kind, base=tmp_path)`` returns the tmp-prefixed
  path.
- Unknown ``kind`` raises :class:`KeyError`.
- :data:`DEFAULT_CACHE_BASE` is `data/cache/registry`.
"""

from __future__ import annotations

import pathlib

import pytest

from app.v2.registry_cache.paths import DEFAULT_CACHE_BASE, cache_path


def test_default_cache_base_path():
    assert DEFAULT_CACHE_BASE == pathlib.Path("data/cache/registry")


def test_slack_filename_under_default_base():
    p = cache_path("slack_channels")
    assert p == pathlib.Path("data/cache/registry/slack_channels.json")


def test_sheets_filename_under_default_base():
    p = cache_path("google_sheets_items")
    assert p == pathlib.Path(
        "data/cache/registry/google_sheets_items.json"
    )


def test_docs_filename_under_default_base():
    p = cache_path("google_docs_items")
    assert p == pathlib.Path(
        "data/cache/registry/google_docs_items.json"
    )


def test_base_override_resolves_to_tmp(tmp_path):
    p = cache_path("slack_channels", base=tmp_path)
    assert p == tmp_path / "slack_channels.json"


def test_base_override_for_sheets(tmp_path):
    p = cache_path("google_sheets_items", base=tmp_path)
    assert p == tmp_path / "google_sheets_items.json"


def test_base_override_for_docs(tmp_path):
    p = cache_path("google_docs_items", base=tmp_path)
    assert p == tmp_path / "google_docs_items.json"


def test_unknown_kind_raises_key_error():
    with pytest.raises(KeyError):
        cache_path("not_a_kind")  # type: ignore[arg-type]


def test_explicit_none_base_uses_default():
    """``base=None`` is the documented default-shape; pin so a
    future refactor that adds a non-None sentinel surfaces."""
    p = cache_path("slack_channels", base=None)
    assert p == pathlib.Path("data/cache/registry/slack_channels.json")
