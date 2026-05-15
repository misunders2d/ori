"""V2 scheduler — registry cache refresh primitives.

Three per-kind functions build a fresh :mod:`schemas` snapshot
from a DI'd client. They are PURE over the client's iterable —
no disk I/O, no fallback to the network, no wrapping of client
errors into :class:`NoCacheAndNetworkDown` (round-1 reviewer
Q8 — that composite belongs to phase 7 authoring).

Phase 6 slice 3 per ``docs/PHASE_6_PLAN.md`` §3.5 + §5.5.

The package does NOT import ``slack_sdk`` /
``googleapiclient`` / ``app.v2.runtime._defaults`` at module
load (round-2 reviewer L347). Clients are typed via
:class:`typing.Protocol` so callers pass any object that
implements the documented method shape; the production wiring
that constructs real Slack / Drive clients lives outside this
package and lands in phase 7.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md`` §3.5 / §5.5
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


# ---------------------------------------------------------------------------
# Constants — MIME types + provenance strings
# ---------------------------------------------------------------------------


_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
_DOCS_MIME = "application/vnd.google-apps.document"

_SLACK_SOURCE = "slack.api.conversations.list"
_SHEETS_SOURCE = (
    "drive.api.files.list?mimeType=application/vnd.google-apps.spreadsheet"
)
_DOCS_SOURCE = (
    "drive.api.files.list?mimeType=application/vnd.google-apps.document"
)


# ---------------------------------------------------------------------------
# Client Protocols — typed DI, no vendor SDK imports
# ---------------------------------------------------------------------------


class SlackChannelsClient(Protocol):
    """Minimal Slack adapter surface this package depends on.

    Production callers (phase 7) wrap ``slack_sdk``'s
    ``conversations.list`` paginator under this Protocol;
    tests pass an in-memory stub.
    """

    def list_conversations(
        self, *, exclude_archived: bool = False
    ) -> Iterable[Mapping[str, Any]]:  # pragma: no cover - protocol stub
        ...


class GoogleDriveClient(Protocol):
    """Minimal Drive adapter surface this package depends on.

    Production callers wrap ``googleapiclient.discovery``'s
    ``files().list`` paginator under this Protocol.
    """

    def list_files(
        self, *, query: Optional[str] = None
    ) -> Iterable[Mapping[str, Any]]:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def refresh_slack_channels(
    client: SlackChannelsClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> SlackChannelsCache:
    """Build a fresh :class:`SlackChannelsCache` snapshot.

    ``expected_owner_id`` is recorded as the snapshot's
    ``workspace_id`` (caller-supplied; client iteration does
    NOT override it). ``clock()`` populates ``fetched_at``;
    the value must be UTC-only or the schema validator
    rejects on construction.

    NEVER writes to disk — the caller decides via
    :func:`app.v2.registry_cache.loader.save_cache`.

    Client errors propagate unchanged (no
    :class:`NoCacheAndNetworkDown` wrap; that composite is
    phase 7's job).
    """
    entries = [
        SlackChannelEntry(
            id=row["id"],
            name=row["name"],
            is_archived=row.get("is_archived", False),
            is_private=row.get("is_private", False),
        )
        for row in client.list_conversations()
    ]
    return SlackChannelsCache(
        workspace_id=expected_owner_id,
        fetched_at=clock(),
        source=_SLACK_SOURCE,
        etag=None,
        channels=entries,
    )


# ---------------------------------------------------------------------------
# Drive — Sheets + Docs
# ---------------------------------------------------------------------------


def _parent_id_from_row(row: Mapping[str, Any]) -> Optional[str]:
    """Drive API returns ``parents: [folder_id]`` (list) for
    non-root items; root items have an empty list or no key."""
    parents = row.get("parents") or []
    return parents[0] if parents else None


def refresh_google_sheets(
    client: GoogleDriveClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> GoogleSheetsCache:
    """Build a fresh :class:`GoogleSheetsCache` snapshot.

    Filters by ``mimeType='application/vnd.google-apps.spreadsheet'``
    inside the call. The schema's MIME literal will reject any
    cross-type payload at validation time — a forgotten filter
    on a custom client implementation surfaces as a
    :class:`pydantic.ValidationError` here (round-2 reviewer
    L398 defence in depth).
    """
    query = f"mimeType='{_SHEETS_MIME}'"
    entries = [
        GoogleSheetsEntry(
            id=row["id"],
            name=row["name"],
            mime_type=row["mimeType"],
            parent_id=_parent_id_from_row(row),
        )
        for row in client.list_files(query=query)
    ]
    return GoogleSheetsCache(
        account_id=expected_owner_id,
        fetched_at=clock(),
        source=_SHEETS_SOURCE,
        etag=None,
        items=entries,
    )


def refresh_google_docs(
    client: GoogleDriveClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> GoogleDocsCache:
    """Build a fresh :class:`GoogleDocsCache` snapshot.

    Filters by ``mimeType='application/vnd.google-apps.document'``
    inside the call. Same defence-in-depth pattern as
    :func:`refresh_google_sheets`.
    """
    query = f"mimeType='{_DOCS_MIME}'"
    entries = [
        GoogleDocsEntry(
            id=row["id"],
            name=row["name"],
            mime_type=row["mimeType"],
            parent_id=_parent_id_from_row(row),
        )
        for row in client.list_files(query=query)
    ]
    return GoogleDocsCache(
        account_id=expected_owner_id,
        fetched_at=clock(),
        source=_DOCS_SOURCE,
        etag=None,
        docs=entries,
    )


__all__ = [
    "GoogleDriveClient",
    "SlackChannelsClient",
    "refresh_google_docs",
    "refresh_google_sheets",
    "refresh_slack_channels",
]
