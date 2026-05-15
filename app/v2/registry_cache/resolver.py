"""V2 scheduler — registry cache lookup helpers.

Phase 6 slice 4 per ``docs/PHASE_6_PLAN.md`` §3.6 + §5.6.

Three pure functions over a loaded cache:

- :func:`resolve_channel` — find a Slack channel by id or
  by name (with the ``#`` prefix stripped). Filters
  archived channels by default; raises
  :class:`ChannelAmbiguous` when a name lookup hits two or
  more active entries (IDs stay canonical per Q6).
- :func:`resolve_sheet` — find a Google Sheets item by id.
- :func:`resolve_doc` — find a Google Docs item by id.

None of these fall back to the network. The authoring layer
(phase 7) wraps a refresh attempt around any
:class:`NoCacheAvailable` / :class:`CacheMiss` it catches.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md`` §3.6 / §5.6
"""

from __future__ import annotations

from typing import Optional

from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAvailable,
)
from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def resolve_channel(
    lookup: str,
    cache: Optional[SlackChannelsCache],
    *,
    include_archived: bool = False,
) -> SlackChannelEntry:
    """Find a Slack channel by ``id`` OR by ``name``.

    The lookup string may use the Slack ``#name`` convention;
    a leading ``#`` is stripped before matching. ID matching
    runs first (canonical) then name matching.

    Archive policy (per Q6 + round-3 slice-3 closure):

    - ``include_archived=False`` (default): archived
      channels are filtered out BEFORE the lookup. A name
      lookup that matches two or more ACTIVE entries raises
      :class:`ChannelAmbiguous` so authoring re-prompts the
      user for an explicit id.
    - ``include_archived=True``: the full cache participates.
      A name lookup with multiple matches returns the first
      hit (no ambiguity check); id lookup remains
      deterministic because Slack ids are unique.

    Raises:
    - :class:`NoCacheAvailable` when ``cache is None`` (with
      ``path=None`` — the resolver has no on-disk path to
      report).
    - :class:`CacheMiss` when ``lookup`` matches neither id
      nor name in the candidate set.
    - :class:`ChannelAmbiguous` per the archive policy above.
    """
    if cache is None:
        raise NoCacheAvailable("slack_channels")

    candidates = (
        list(cache.channels)
        if include_archived
        else [c for c in cache.channels if not c.is_archived]
    )

    stripped = lookup[1:] if lookup.startswith("#") else lookup

    # 1. Canonical id match.
    for entry in candidates:
        if entry.id == stripped:
            return entry

    # 2. Name match against the candidate set.
    matches = [entry for entry in candidates if entry.name == stripped]

    if not matches:
        raise CacheMiss("slack_channels", lookup)
    if len(matches) == 1:
        return matches[0]

    # 2+ matches.
    if include_archived:
        # No ambiguity check in include_archived mode; return
        # whichever the iteration hits first. Callers that
        # need a specific entry pass the id directly.
        return matches[0]
    raise ChannelAmbiguous(stripped, [m.id for m in matches])


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


def resolve_sheet(
    spreadsheet_id: str,
    cache: Optional[GoogleSheetsCache],
) -> GoogleSheetsEntry:
    """Find a sheets entry by id.

    MIME enforcement is intrinsic to :class:`GoogleSheetsEntry`
    (the schema's Literal rejects any non-spreadsheet payload
    at validation time), so the resolver never has to
    cross-check.

    Raises :class:`NoCacheAvailable` when ``cache is None``,
    :class:`CacheMiss` when the id is not present.
    """
    if cache is None:
        raise NoCacheAvailable("google_sheets_items")

    for entry in cache.items:
        if entry.id == spreadsheet_id:
            return entry

    raise CacheMiss("google_sheets_items", spreadsheet_id)


# ---------------------------------------------------------------------------
# Google Docs
# ---------------------------------------------------------------------------


def resolve_doc(
    document_id: str,
    cache: Optional[GoogleDocsCache],
) -> GoogleDocsEntry:
    """Find a docs entry by id.

    Same schema-level MIME enforcement as
    :func:`resolve_sheet`.

    Raises :class:`NoCacheAvailable` when ``cache is None``,
    :class:`CacheMiss` when the id is not present.
    """
    if cache is None:
        raise NoCacheAvailable("google_docs_items")

    for entry in cache.docs:
        if entry.id == document_id:
            return entry

    raise CacheMiss("google_docs_items", document_id)


__all__ = ["resolve_channel", "resolve_doc", "resolve_sheet"]
