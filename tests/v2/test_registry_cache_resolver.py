"""Tests for ``app.v2.registry_cache.resolver``.

Phase 6 slice 4 per ``docs/PHASE_6_PLAN.md`` §5.6.

Pins:
- Channel id lookup: ``resolve_channel("C012", cache)``.
- Channel name lookup: ``#general`` and bare ``general``.
- ``CacheMiss`` on miss.
- ``NoCacheAvailable`` on ``cache=None`` (path is None per
  L154 fix).
- Archived channels excluded by default; one archived + one
  active entry sharing a name → default call returns the
  active one.
- ``include_archived=True`` with two same-named entries (one
  archived) returns the first match (no ambiguity check).
- Duplicate ACTIVE name → ``ChannelAmbiguous`` with
  ``candidate_ids`` in cache order. ID-keyed lookup still
  works.
- Archived duplicates do NOT trigger ``ChannelAmbiguous``
  (filtered before duplicate check).
- Sheets/docs by id, ``CacheMiss``, ``NoCacheAvailable``
  with ``path is None``.
- Type-mismatch checks via runtime AttributeError shape
  (passing a docs cache to ``resolve_sheet`` blows up
  because the docs cache has no ``items`` attribute).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAvailable,
)
from app.v2.registry_cache.resolver import (
    resolve_channel,
    resolve_doc,
    resolve_sheet,
)
from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _slack_cache(channels) -> SlackChannelsCache:
    return SlackChannelsCache(
        workspace_id="T_TEST",
        fetched_at=_UTC_NOW,
        source="x",
        etag=None,
        channels=channels,
    )


def _sheets_cache(items) -> GoogleSheetsCache:
    return GoogleSheetsCache(
        account_id="acct@x.iam",
        fetched_at=_UTC_NOW,
        source="x",
        etag=None,
        items=items,
    )


def _docs_cache(docs) -> GoogleDocsCache:
    return GoogleDocsCache(
        account_id="acct@x.iam",
        fetched_at=_UTC_NOW,
        source="x",
        etag=None,
        docs=docs,
    )


# ===========================================================================
# resolve_channel — happy paths
# ===========================================================================


def test_resolve_channel_by_id():
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C012", name="general"),
            SlackChannelEntry(id="C034", name="alerts"),
        ]
    )
    entry = resolve_channel("C012", cache)
    assert entry.id == "C012"
    assert entry.name == "general"


def test_resolve_channel_by_hash_name():
    cache = _slack_cache(
        [SlackChannelEntry(id="C012", name="general")]
    )
    entry = resolve_channel("#general", cache)
    assert entry.id == "C012"


def test_resolve_channel_by_bare_name():
    cache = _slack_cache(
        [SlackChannelEntry(id="C012", name="general")]
    )
    entry = resolve_channel("general", cache)
    assert entry.id == "C012"


# ===========================================================================
# resolve_channel — error paths
# ===========================================================================


def test_resolve_channel_missing_raises_cache_miss():
    cache = _slack_cache(
        [SlackChannelEntry(id="C001", name="general")]
    )
    with pytest.raises(CacheMiss) as exc_info:
        resolve_channel("missing", cache)
    assert exc_info.value.kind == "slack_channels"
    assert exc_info.value.lookup == "missing"


def test_resolve_channel_none_cache_raises_no_cache_available():
    with pytest.raises(NoCacheAvailable) as exc_info:
        resolve_channel("C012", cache=None)
    assert exc_info.value.kind == "slack_channels"
    # Resolver call path: path is None, message degrades to
    # "not provided" (round-2 reviewer L154).
    assert exc_info.value.path is None
    assert "not provided" in str(exc_info.value)


# ===========================================================================
# Archive policy — default include_archived=False
# ===========================================================================


def test_resolve_channel_filters_archived_by_default():
    """One archived + one active entry sharing a name —
    default call returns the active one."""
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts", is_archived=True),
            SlackChannelEntry(id="C002", name="alerts", is_archived=False),
        ]
    )

    entry = resolve_channel("alerts", cache)

    assert entry.id == "C002"
    assert entry.is_archived is False


def test_resolve_channel_id_lookup_of_archived_returns_miss_by_default():
    """ID lookup against an archived channel with default
    ``include_archived=False`` does NOT return the archived
    entry — caller passes ``include_archived=True`` if they
    explicitly want archive listings."""
    cache = _slack_cache(
        [SlackChannelEntry(id="C999", name="old", is_archived=True)]
    )
    with pytest.raises(CacheMiss):
        resolve_channel("C999", cache)


def test_resolve_channel_include_archived_finds_archived_by_id():
    cache = _slack_cache(
        [SlackChannelEntry(id="C999", name="old", is_archived=True)]
    )
    entry = resolve_channel("C999", cache, include_archived=True)
    assert entry.id == "C999"


def test_resolve_channel_include_archived_returns_first_with_dup_name():
    """``include_archived=True`` + two same-named entries
    (one archived) → returns whichever the search hits first
    (no ambiguity check)."""
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts", is_archived=True),
            SlackChannelEntry(id="C002", name="alerts", is_archived=False),
        ]
    )

    entry = resolve_channel(
        "alerts", cache, include_archived=True
    )

    assert entry.id == "C001"


# ===========================================================================
# ChannelAmbiguous — duplicate ACTIVE names
# ===========================================================================


def test_resolve_channel_dup_active_name_raises_ambiguous():
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts", is_archived=False),
            SlackChannelEntry(id="C002", name="alerts", is_archived=False),
        ]
    )

    with pytest.raises(ChannelAmbiguous) as exc_info:
        resolve_channel("alerts", cache)

    assert exc_info.value.name == "alerts"
    assert exc_info.value.candidate_ids == ["C001", "C002"]


def test_resolve_channel_dup_active_name_id_lookup_still_works():
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts", is_archived=False),
            SlackChannelEntry(id="C002", name="alerts", is_archived=False),
        ]
    )

    # Direct id lookup bypasses the name-ambiguity path.
    entry = resolve_channel("C001", cache)
    assert entry.id == "C001"


def test_resolve_channel_three_active_duplicates_message_lists_all():
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts"),
            SlackChannelEntry(id="C002", name="alerts"),
            SlackChannelEntry(id="C003", name="alerts"),
        ]
    )

    with pytest.raises(ChannelAmbiguous) as exc_info:
        resolve_channel("alerts", cache)
    assert exc_info.value.candidate_ids == ["C001", "C002", "C003"]


def test_resolve_channel_archived_duplicate_does_not_trigger_ambiguous():
    """Per Q6: archived entries are filtered BEFORE the
    duplicate check. One archived + one active sharing a
    name → active one returned, no ambiguity."""
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts", is_archived=True),
            SlackChannelEntry(id="C002", name="alerts", is_archived=False),
        ]
    )

    entry = resolve_channel("alerts", cache)
    assert entry.id == "C002"


def test_resolve_channel_hash_prefix_with_ambiguity():
    """Strip-# applies before the duplicate check too."""
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts"),
            SlackChannelEntry(id="C002", name="alerts"),
        ]
    )

    with pytest.raises(ChannelAmbiguous) as exc_info:
        resolve_channel("#alerts", cache)
    assert exc_info.value.name == "alerts"


# ===========================================================================
# resolve_sheet
# ===========================================================================


def test_resolve_sheet_by_id():
    cache = _sheets_cache(
        [
            GoogleSheetsEntry(id="1aBc", name="weekly"),
            GoogleSheetsEntry(id="2dEf", name="monthly"),
        ]
    )
    entry = resolve_sheet("1aBc", cache)
    assert entry.id == "1aBc"
    assert entry.name == "weekly"


def test_resolve_sheet_missing_raises_cache_miss():
    cache = _sheets_cache(
        [GoogleSheetsEntry(id="1aBc", name="x")]
    )
    with pytest.raises(CacheMiss) as exc_info:
        resolve_sheet("nope", cache)
    assert exc_info.value.kind == "google_sheets_items"
    assert exc_info.value.lookup == "nope"


def test_resolve_sheet_none_cache_raises_no_cache_available():
    with pytest.raises(NoCacheAvailable) as exc_info:
        resolve_sheet("1aBc", cache=None)
    assert exc_info.value.kind == "google_sheets_items"
    assert exc_info.value.path is None
    assert "not provided" in str(exc_info.value)


def test_resolve_sheet_empty_items_yields_cache_miss():
    cache = _sheets_cache([])
    with pytest.raises(CacheMiss):
        resolve_sheet("1aBc", cache)


# ===========================================================================
# resolve_doc
# ===========================================================================


def test_resolve_doc_by_id():
    cache = _docs_cache(
        [
            GoogleDocsEntry(id="1xYz", name="weekly notes"),
            GoogleDocsEntry(id="2yYz", name="quarterly review"),
        ]
    )
    entry = resolve_doc("1xYz", cache)
    assert entry.id == "1xYz"
    assert entry.name == "weekly notes"


def test_resolve_doc_missing_raises_cache_miss():
    cache = _docs_cache(
        [GoogleDocsEntry(id="1xYz", name="x")]
    )
    with pytest.raises(CacheMiss) as exc_info:
        resolve_doc("nope", cache)
    assert exc_info.value.kind == "google_docs_items"
    assert exc_info.value.lookup == "nope"


def test_resolve_doc_none_cache_raises_no_cache_available():
    with pytest.raises(NoCacheAvailable) as exc_info:
        resolve_doc("1xYz", cache=None)
    assert exc_info.value.kind == "google_docs_items"
    assert exc_info.value.path is None
    assert "not provided" in str(exc_info.value)


def test_resolve_doc_empty_docs_yields_cache_miss():
    cache = _docs_cache([])
    with pytest.raises(CacheMiss):
        resolve_doc("1xYz", cache)


# ===========================================================================
# Cross-cache type-mismatch (L398 defence in depth)
# ===========================================================================


def test_resolve_sheet_rejects_docs_cache_at_runtime():
    """``GoogleDocsCache`` has ``docs`` not ``items``. The
    resolver iterates ``cache.items``, so a docs cache
    surfaces as AttributeError (in addition to the static
    type error mypy would catch)."""
    docs = _docs_cache([GoogleDocsEntry(id="1", name="x")])
    with pytest.raises(AttributeError):
        resolve_sheet("1", docs)  # type: ignore[arg-type]


def test_resolve_doc_rejects_sheets_cache_at_runtime():
    """``GoogleSheetsCache`` has ``items`` not ``docs``.
    Inverse type-mismatch."""
    sheets = _sheets_cache([GoogleSheetsEntry(id="1", name="x")])
    with pytest.raises(AttributeError):
        resolve_doc("1", sheets)  # type: ignore[arg-type]


def test_resolve_channel_rejects_sheets_cache_at_runtime():
    """``SlackChannelsCache`` has ``channels``; sheets cache
    has ``items``."""
    sheets = _sheets_cache([GoogleSheetsEntry(id="1", name="x")])
    with pytest.raises(AttributeError):
        resolve_channel("x", sheets)  # type: ignore[arg-type]
