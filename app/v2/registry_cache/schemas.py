"""V2 scheduler — registry cache schemas.

Pydantic v2 models for the three cache files under
``data/cache/registry/``: Slack channels, Google Sheets items,
Google Docs items. Each model carries the provenance fields
design §5.7 lists (owner id, fetched_at, source, etag) plus an
items list of MIME-literal entries.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §3.2.

The shared :func:`_require_utc` field validator enforces that
every ``fetched_at`` is tz-aware AND UTC (utcoffset == 0). The
"UTC-only" tightening came in round-2 reviewer L229 — naive
datetime AND non-UTC offsets both raise so the staleness
predicate (phase 6 slice 2) and the round-trip
(serialise → deserialise → compare) stay safe across tz
boundaries.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md`` §3.2
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Shared UTC validator — phase 6 slice 1.
# ---------------------------------------------------------------------------


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes AND non-UTC offsets.

    The cache invariant is UTC-only (``utcoffset() ==
    timedelta(0)``), not just tz-aware. Mirrors the runtime
    invariant from phases 4-5 and keeps the phase-6 slice-2
    ``is_stale`` arithmetic safe across tz boundaries.

    Two distinct error messages so the cause is obvious in
    logs:

    - Naive (``tzinfo is None``) → "must be tz-aware UTC (got
      naive datetime)".
    - Non-UTC (``utcoffset() != 0``) → "must be UTC (got
      utcoffset=<delta>); convert via value.astimezone(timezone.utc)".
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "registry_cache fetched_at must be tz-aware UTC "
            "(got naive datetime)"
        )
    if value.utcoffset() != timedelta(0):
        raise ValueError(
            "registry_cache fetched_at must be UTC "
            f"(got utcoffset={value.utcoffset()!r}); convert via "
            "value.astimezone(timezone.utc) before persistence"
        )
    return value


# ---------------------------------------------------------------------------
# Slack channels
# ---------------------------------------------------------------------------


class SlackChannelEntry(BaseModel):
    """One row inside :class:`SlackChannelsCache.channels`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    is_archived: bool = False
    is_private: bool = False


class SlackChannelsCache(BaseModel):
    """On-disk shape of ``slack_channels.json``.

    Provenance fields per design §5.7:
    ``workspace_id`` / ``fetched_at`` / ``source`` / ``etag``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["slack_channels"] = "slack_channels"
    workspace_id: str
    fetched_at: datetime
    source: str
    etag: Optional[str] = None
    channels: list[SlackChannelEntry]

    @field_validator("fetched_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        """Uniform name across kinds (round-1 reviewer L295
        rename). Returns ``workspace_id``."""
        return self.workspace_id


# ---------------------------------------------------------------------------
# Google Sheets items
# ---------------------------------------------------------------------------


class GoogleSheetsEntry(BaseModel):
    """One row inside :class:`GoogleSheetsCache.items`.

    ``mime_type`` is a ``Literal`` so a docs payload could
    never reach a sheets resolver (round-1 reviewer L398 fix).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    mime_type: Literal[
        "application/vnd.google-apps.spreadsheet"
    ] = "application/vnd.google-apps.spreadsheet"
    parent_id: Optional[str] = None


class GoogleSheetsCache(BaseModel):
    """On-disk shape of ``google_sheets_items.json``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_sheets_items"] = "google_sheets_items"
    account_id: str
    fetched_at: datetime
    source: str
    etag: Optional[str] = None
    items: list[GoogleSheetsEntry]

    @field_validator("fetched_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        """Returns ``account_id``."""
        return self.account_id


# ---------------------------------------------------------------------------
# Google Docs items
# ---------------------------------------------------------------------------


class GoogleDocsEntry(BaseModel):
    """One row inside :class:`GoogleDocsCache.docs`.

    Same MIME-literal defence-in-depth as
    :class:`GoogleSheetsEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    mime_type: Literal[
        "application/vnd.google-apps.document"
    ] = "application/vnd.google-apps.document"
    parent_id: Optional[str] = None


class GoogleDocsCache(BaseModel):
    """On-disk shape of ``google_docs_items.json``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["google_docs_items"] = "google_docs_items"
    account_id: str
    fetched_at: datetime
    source: str
    etag: Optional[str] = None
    docs: list[GoogleDocsEntry]

    @field_validator("fetched_at")
    @classmethod
    def _utc_only(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        """Returns ``account_id``."""
        return self.account_id


# ---------------------------------------------------------------------------
# Type aliases for callers
# ---------------------------------------------------------------------------


CacheKind = Literal[
    "slack_channels",
    "google_sheets_items",
    "google_docs_items",
]
"""Discriminator literal naming each on-disk cache file."""


CacheFile = Union[
    SlackChannelsCache, GoogleSheetsCache, GoogleDocsCache
]
"""Union of the three cache shapes; the ``kind`` discriminator
is the safe way to narrow at the call site."""


__all__ = [
    "CacheFile",
    "CacheKind",
    "GoogleDocsCache",
    "GoogleDocsEntry",
    "GoogleSheetsCache",
    "GoogleSheetsEntry",
    "SlackChannelEntry",
    "SlackChannelsCache",
]
