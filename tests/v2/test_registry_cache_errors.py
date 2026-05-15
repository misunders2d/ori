"""Tests for ``app.v2.registry_cache.errors``.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §5.1.

Pins:
- Five subclasses of :class:`RegistryCacheError`.
- ``NoCacheAvailable(kind, path=None)`` message switches between
  ``not provided`` (resolver call path) and ``missing at
  <path>`` (loader call path). Both shapes preserve
  ``exc.kind``.
- Each subclass carries the documented attributes; the message
  references them.
- Every subclass inherits from :class:`RegistryCacheError`
  which inherits from :class:`Exception`.
"""

from __future__ import annotations

import pytest

from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAndNetworkDown,
    NoCacheAvailable,
    RegistryCacheError,
    WorkspaceMismatch,
)


# ===========================================================================
# RegistryCacheError base
# ===========================================================================


def test_base_inherits_from_exception():
    assert issubclass(RegistryCacheError, Exception)


@pytest.mark.parametrize(
    "subclass",
    [
        NoCacheAvailable,
        CacheMiss,
        WorkspaceMismatch,
        NoCacheAndNetworkDown,
        ChannelAmbiguous,
    ],
)
def test_subclass_inherits_from_registry_cache_error(subclass):
    assert issubclass(subclass, RegistryCacheError)
    assert issubclass(subclass, Exception)


def test_exactly_five_subclasses_exported():
    """Pin the public surface so a 6th subclass without
    explicit review (or a removed one) surfaces here."""
    from app.v2.registry_cache import errors as errors_mod

    exported = set(errors_mod.__all__)
    expected = {
        "RegistryCacheError",
        "NoCacheAvailable",
        "CacheMiss",
        "WorkspaceMismatch",
        "NoCacheAndNetworkDown",
        "ChannelAmbiguous",
    }
    assert exported == expected


# ===========================================================================
# NoCacheAvailable — L154 dual call path
# ===========================================================================


def test_no_cache_available_with_path():
    """Loader call path: file path is known, surfaces in
    the message + ``exc.path``."""
    exc = NoCacheAvailable("slack_channels", path="/data/cache/slack.json")

    assert exc.kind == "slack_channels"
    assert exc.path == "/data/cache/slack.json"
    assert "missing at /data/cache/slack.json" in str(exc)
    assert "slack_channels" in str(exc)


def test_no_cache_available_without_path_defaults_to_none():
    """Resolver call path: ``cache=None`` reached us. No file
    path to report; message degrades to ``not provided`` so
    logs disambiguate."""
    exc = NoCacheAvailable("google_sheets_items")

    assert exc.kind == "google_sheets_items"
    assert exc.path is None
    assert "not provided" in str(exc)
    assert "google_sheets_items" in str(exc)


def test_no_cache_available_explicit_none_path():
    """Same behaviour as omitted path."""
    exc = NoCacheAvailable("google_docs_items", path=None)

    assert exc.path is None
    assert "not provided" in str(exc)


# ===========================================================================
# CacheMiss
# ===========================================================================


def test_cache_miss_records_kind_and_lookup():
    exc = CacheMiss("slack_channels", "#missing")

    assert exc.kind == "slack_channels"
    assert exc.lookup == "#missing"
    assert "#missing" in str(exc)
    assert "slack_channels" in str(exc)


# ===========================================================================
# WorkspaceMismatch
# ===========================================================================


def test_workspace_mismatch_records_expected_and_found():
    exc = WorkspaceMismatch(
        "slack_channels", expected="T_NEW", found="T_OLD"
    )

    assert exc.kind == "slack_channels"
    assert exc.expected == "T_NEW"
    assert exc.found == "T_OLD"
    assert "T_NEW" in str(exc)
    assert "T_OLD" in str(exc)


# ===========================================================================
# NoCacheAndNetworkDown
# ===========================================================================


def test_no_cache_and_network_down_carries_both_attrs():
    exc = NoCacheAndNetworkDown(
        "slack_channels", network_error="DNS resolution failed"
    )

    assert exc.kind == "slack_channels"
    assert exc.network_error == "DNS resolution failed"
    assert "slack_channels" in str(exc)
    assert "DNS resolution failed" in str(exc)


# ===========================================================================
# ChannelAmbiguous
# ===========================================================================


def test_channel_ambiguous_records_name_and_candidates():
    exc = ChannelAmbiguous("alerts", candidate_ids=["C001", "C002"])

    assert exc.name == "alerts"
    assert exc.candidate_ids == ["C001", "C002"]
    msg = str(exc)
    assert "alerts" in msg
    assert "2" in msg
    assert "C001" in msg
    assert "C002" in msg


def test_channel_ambiguous_three_candidates_count_in_message():
    """Message reports the candidate count; pin so a future
    refactor that drops the count line surfaces."""
    exc = ChannelAmbiguous(
        "alerts", candidate_ids=["C001", "C002", "C003"]
    )

    assert "3" in str(exc)
