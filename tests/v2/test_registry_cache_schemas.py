"""Tests for ``app.v2.registry_cache.schemas``.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §5.2.

Pins:
- ``extra="forbid"`` on every model.
- ``Literal[<kind>]`` discriminator enforced per model.
- ``_require_utc`` validator on ``fetched_at`` rejects naive
  datetime AND non-UTC offsets on every cache model
  (round-2 reviewer L223 + L229).
- MIME-literal entry classes for the Google kinds reject any
  cross-type payload at validation time (round-1 reviewer
  L398).
- ``owner_id`` property returns ``workspace_id`` for Slack,
  ``account_id`` for the two Google caches (round-1 reviewer
  L295 rename).
- Round-trip via ``model_dump_json`` / ``model_validate_json``
  preserves every field.
- Empty entries list is valid (a freshly emptied workspace is
  legal).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_NAIVE = datetime(2026, 5, 15, 12, 0)
_TZ_NONUTC = datetime(
    2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=5))
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _slack_cache(**overrides) -> SlackChannelsCache:
    base = dict(
        workspace_id="T_TEST",
        fetched_at=_UTC_NOW,
        source="slack.api.conversations.list",
        etag=None,
        channels=[
            SlackChannelEntry(id="C001", name="general"),
            SlackChannelEntry(id="C002", name="alerts"),
        ],
    )
    base.update(overrides)
    return SlackChannelsCache(**base)


def _sheets_cache(**overrides) -> GoogleSheetsCache:
    base = dict(
        account_id="acct@x.iam.gserviceaccount.com",
        fetched_at=_UTC_NOW,
        source="drive.api.files.list?mimeType=spreadsheet",
        etag="W/\"abc123\"",
        items=[
            GoogleSheetsEntry(id="1aBc", name="weekly metrics"),
        ],
    )
    base.update(overrides)
    return GoogleSheetsCache(**base)


def _docs_cache(**overrides) -> GoogleDocsCache:
    base = dict(
        account_id="acct@x.iam.gserviceaccount.com",
        fetched_at=_UTC_NOW,
        source="drive.api.files.list?mimeType=document",
        etag=None,
        docs=[
            GoogleDocsEntry(id="1xYz", name="weekly notes"),
        ],
    )
    base.update(overrides)
    return GoogleDocsCache(**base)


# ===========================================================================
# extra="forbid" on every model
# ===========================================================================


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (
            SlackChannelEntry,
            dict(id="C001", name="general", unknown_field="x"),
        ),
        (
            GoogleSheetsEntry,
            dict(id="1aBc", name="x", unknown_field="x"),
        ),
        (
            GoogleDocsEntry,
            dict(id="1xYz", name="x", unknown_field="x"),
        ),
    ],
)
def test_entry_models_forbid_extra(cls, kwargs):
    with pytest.raises(ValidationError):
        cls(**kwargs)


def test_slack_cache_forbids_extra():
    with pytest.raises(ValidationError):
        SlackChannelsCache(
            workspace_id="T_X",
            fetched_at=_UTC_NOW,
            source="x",
            channels=[],
            unknown_field="x",
        )


def test_sheets_cache_forbids_extra():
    with pytest.raises(ValidationError):
        GoogleSheetsCache(
            account_id="x",
            fetched_at=_UTC_NOW,
            source="x",
            items=[],
            unknown_field="x",
        )


def test_docs_cache_forbids_extra():
    with pytest.raises(ValidationError):
        GoogleDocsCache(
            account_id="x",
            fetched_at=_UTC_NOW,
            source="x",
            docs=[],
            unknown_field="x",
        )


# ===========================================================================
# Discriminator Literal enforcement
# ===========================================================================


def test_slack_cache_rejects_wrong_kind():
    with pytest.raises(ValidationError):
        SlackChannelsCache(
            kind="google_sheets_items",
            workspace_id="T_X",
            fetched_at=_UTC_NOW,
            source="x",
            channels=[],
        )


def test_sheets_cache_rejects_wrong_kind():
    with pytest.raises(ValidationError):
        GoogleSheetsCache(
            kind="google_docs_items",
            account_id="x",
            fetched_at=_UTC_NOW,
            source="x",
            items=[],
        )


def test_docs_cache_rejects_wrong_kind():
    with pytest.raises(ValidationError):
        GoogleDocsCache(
            kind="slack_channels",
            account_id="x",
            fetched_at=_UTC_NOW,
            source="x",
            docs=[],
        )


def test_slack_cache_default_kind():
    assert _slack_cache().kind == "slack_channels"


def test_sheets_cache_default_kind():
    assert _sheets_cache().kind == "google_sheets_items"


def test_docs_cache_default_kind():
    assert _docs_cache().kind == "google_docs_items"


# ===========================================================================
# _require_utc validator — three sub-cases per cache model
# (L223 + L229)
# ===========================================================================


@pytest.mark.parametrize(
    "builder",
    [_slack_cache, _sheets_cache, _docs_cache],
    ids=["slack", "sheets", "docs"],
)
def test_naive_fetched_at_rejected(builder):
    with pytest.raises(ValidationError) as exc_info:
        builder(fetched_at=_NAIVE)
    assert "naive datetime" in str(exc_info.value)


@pytest.mark.parametrize(
    "builder",
    [_slack_cache, _sheets_cache, _docs_cache],
    ids=["slack", "sheets", "docs"],
)
def test_non_utc_offset_rejected(builder):
    with pytest.raises(ValidationError) as exc_info:
        builder(fetched_at=_TZ_NONUTC)
    msg = str(exc_info.value)
    assert "must be UTC" in msg
    assert "utcoffset" in msg


@pytest.mark.parametrize(
    "builder",
    [_slack_cache, _sheets_cache, _docs_cache],
    ids=["slack", "sheets", "docs"],
)
def test_utc_fetched_at_accepted_and_round_trips(builder):
    cache = builder(fetched_at=_UTC_NOW)

    payload = cache.model_dump_json()
    rebuilt = type(cache).model_validate_json(payload)

    assert rebuilt.fetched_at == _UTC_NOW
    assert rebuilt.fetched_at.utcoffset() == timedelta(0)


# ===========================================================================
# MIME-literal enforcement on Google entries (L398)
# ===========================================================================


def test_sheets_entry_rejects_docs_mime():
    with pytest.raises(ValidationError):
        GoogleSheetsEntry(
            id="x",
            name="x",
            mime_type="application/vnd.google-apps.document",
        )


def test_docs_entry_rejects_sheets_mime():
    with pytest.raises(ValidationError):
        GoogleDocsEntry(
            id="x",
            name="x",
            mime_type="application/vnd.google-apps.spreadsheet",
        )


def test_sheets_entry_default_mime_is_spreadsheet():
    entry = GoogleSheetsEntry(id="x", name="x")
    assert entry.mime_type == "application/vnd.google-apps.spreadsheet"


def test_docs_entry_default_mime_is_document():
    entry = GoogleDocsEntry(id="x", name="x")
    assert entry.mime_type == "application/vnd.google-apps.document"


# ===========================================================================
# Round-trip preserves every field
# ===========================================================================


def test_slack_cache_round_trip():
    cache = _slack_cache(etag="W/\"slack-etag\"")
    rebuilt = SlackChannelsCache.model_validate_json(cache.model_dump_json())

    assert rebuilt == cache


def test_sheets_cache_round_trip():
    cache = _sheets_cache()
    rebuilt = GoogleSheetsCache.model_validate_json(cache.model_dump_json())

    assert rebuilt == cache


def test_docs_cache_round_trip():
    cache = _docs_cache(etag="W/\"docs-etag\"")
    rebuilt = GoogleDocsCache.model_validate_json(cache.model_dump_json())

    assert rebuilt == cache


# ===========================================================================
# Empty entries list is valid
# ===========================================================================


def test_slack_cache_accepts_empty_channels():
    cache = _slack_cache(channels=[])
    assert cache.channels == []


def test_sheets_cache_accepts_empty_items():
    cache = _sheets_cache(items=[])
    assert cache.items == []


def test_docs_cache_accepts_empty_docs():
    cache = _docs_cache(docs=[])
    assert cache.docs == []


# ===========================================================================
# owner_id property (L295 rename)
# ===========================================================================


def test_slack_owner_id_returns_workspace_id():
    cache = _slack_cache(workspace_id="T_OWNED")
    assert cache.owner_id == "T_OWNED"


def test_sheets_owner_id_returns_account_id():
    cache = _sheets_cache(account_id="sa@x.iam")
    assert cache.owner_id == "sa@x.iam"


def test_docs_owner_id_returns_account_id():
    cache = _docs_cache(account_id="sa@x.iam")
    assert cache.owner_id == "sa@x.iam"


# ===========================================================================
# Optional etag defaults to None
# ===========================================================================


def test_slack_cache_default_etag_is_none():
    cache = SlackChannelsCache(
        workspace_id="T_X",
        fetched_at=_UTC_NOW,
        source="x",
        channels=[],
    )
    assert cache.etag is None


def test_sheets_cache_default_etag_is_none():
    cache = GoogleSheetsCache(
        account_id="x",
        fetched_at=_UTC_NOW,
        source="x",
        items=[],
    )
    assert cache.etag is None


def test_docs_cache_default_etag_is_none():
    cache = GoogleDocsCache(
        account_id="x",
        fetched_at=_UTC_NOW,
        source="x",
        docs=[],
    )
    assert cache.etag is None
