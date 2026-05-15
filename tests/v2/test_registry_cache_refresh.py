"""Tests for ``app.v2.registry_cache.refresh``.

Phase 6 slice 3 per ``docs/PHASE_6_PLAN.md`` §5.5.

Pins:
- ``clock`` is a REQUIRED keyword-only parameter on each
  refresh function (no default — round-2 reviewer L347).
- The refresh module does NOT import ``slack_sdk`` /
  ``googleapiclient`` / ``app.v2.runtime._defaults`` at
  module load (AST pin on import statements).
- Slack: stub client → cache with entries in order;
  ``fetched_at`` matches injected clock; empty iterable →
  empty cache; ``expected_owner_id`` recorded as
  ``workspace_id``.
- Sheets / Docs: stub Drive client filters by the expected
  MIME (spy assertion on ``query=...`` parameter); the
  schema's MIME literal rejects a cross-type payload
  (round-2 reviewer L398 defence in depth).
- Client errors (``RuntimeError``, etc.) propagate
  unchanged; refresh does NOT wrap them into
  :class:`NoCacheAndNetworkDown` (per Q8 — phase 7 owns the
  composite).
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.registry_cache import refresh as refresh_mod
from app.v2.registry_cache.refresh import (
    GoogleDriveClient,
    SlackChannelsClient,
    refresh_google_docs,
    refresh_google_sheets,
    refresh_slack_channels,
)
from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleSheetsCache,
    SlackChannelsCache,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSlackClient:
    """In-memory replacement for the
    :class:`SlackChannelsClient` Protocol."""

    def __init__(self, rows, *, exc=None) -> None:
        self._rows = rows
        self._exc = exc
        self.calls: list[dict] = []

    def list_conversations(self, *, exclude_archived=False):
        self.calls.append({"exclude_archived": exclude_archived})
        if self._exc is not None:
            raise self._exc
        return list(self._rows)


class _StubDriveClient:
    """In-memory replacement for the
    :class:`GoogleDriveClient` Protocol."""

    def __init__(self, rows, *, exc=None) -> None:
        self._rows = rows
        self._exc = exc
        self.queries: list[str | None] = []

    def list_files(self, *, query=None):
        self.queries.append(query)
        if self._exc is not None:
            raise self._exc
        return list(self._rows)


# ===========================================================================
# Clock-required pin (L347)
# ===========================================================================


@pytest.mark.parametrize(
    "fn",
    [refresh_slack_channels, refresh_google_sheets, refresh_google_docs],
    ids=["slack", "sheets", "docs"],
)
def test_clock_parameter_has_no_default(fn):
    sig = inspect.signature(fn)
    assert "clock" in sig.parameters, f"{fn.__name__} missing clock param"
    assert sig.parameters["clock"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "fn",
    [refresh_slack_channels, refresh_google_sheets, refresh_google_docs],
    ids=["slack", "sheets", "docs"],
)
def test_clock_parameter_is_keyword_only(fn):
    """Pin so callers cannot accidentally pass clock
    positionally — the explicit keyword surfaces in every
    call site."""
    sig = inspect.signature(fn)
    assert (
        sig.parameters["clock"].kind == inspect.Parameter.KEYWORD_ONLY
    )


@pytest.mark.parametrize(
    "fn",
    [refresh_slack_channels, refresh_google_sheets, refresh_google_docs],
    ids=["slack", "sheets", "docs"],
)
def test_expected_owner_id_is_keyword_only(fn):
    sig = inspect.signature(fn)
    assert "expected_owner_id" in sig.parameters
    assert (
        sig.parameters["expected_owner_id"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )


# ===========================================================================
# No vendor-SDK / _defaults module-level imports (AST pin)
# ===========================================================================


def _module_imports(module) -> set[str]:
    """Walk the module's AST and return every imported name
    (``import x`` and ``from x import …``)."""
    src = inspect.getsource(module)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


@pytest.mark.parametrize(
    "forbidden",
    [
        "slack_sdk",
        "googleapiclient",
        "googleapiclient.discovery",
        "app.v2.runtime._defaults",
    ],
)
def test_refresh_does_not_import(forbidden):
    """Round-2 reviewer L347 — package stays decoupled from
    the runtime / vendor SDKs. The Protocol-typed DI is the
    seam; production wiring sits outside this module."""
    names = _module_imports(refresh_mod)
    leaked = [n for n in names if forbidden in n]
    assert not leaked, (
        f"refresh.py imports forbidden module {forbidden!r}: {leaked!r}"
    )


def test_refresh_module_does_not_bind_prod_clock():
    """Belt-and-braces: even if some other module imported
    `_defaults` and put it in sys.modules, the refresh module
    must not expose `prod_clock`."""
    assert not hasattr(refresh_mod, "prod_clock")


# ===========================================================================
# Slack — happy paths
# ===========================================================================


def test_slack_two_channels_in_order():
    client = _StubSlackClient(
        [
            {"id": "C001", "name": "general", "is_private": False},
            {"id": "C002", "name": "alerts", "is_archived": True},
        ]
    )
    cache = refresh_slack_channels(
        client,
        expected_owner_id="T_TEST",
        clock=_fixed_clock,
    )

    assert isinstance(cache, SlackChannelsCache)
    assert cache.workspace_id == "T_TEST"
    assert cache.fetched_at == _UTC_NOW
    assert cache.source == "slack.api.conversations.list"
    assert cache.etag is None
    assert [(c.id, c.name) for c in cache.channels] == [
        ("C001", "general"),
        ("C002", "alerts"),
    ]
    assert cache.channels[0].is_private is False
    assert cache.channels[1].is_archived is True


def test_slack_empty_iterable_yields_empty_channels():
    client = _StubSlackClient([])
    cache = refresh_slack_channels(
        client, expected_owner_id="T_OWNER", clock=_fixed_clock
    )

    assert cache.channels == []
    assert cache.workspace_id == "T_OWNER"


def test_slack_missing_is_archived_defaults_to_false():
    client = _StubSlackClient([{"id": "C001", "name": "x"}])
    cache = refresh_slack_channels(
        client, expected_owner_id="T_X", clock=_fixed_clock
    )

    assert cache.channels[0].is_archived is False
    assert cache.channels[0].is_private is False


def test_slack_expected_owner_id_not_overridden_by_client():
    """Pin so a future refactor that resolves workspace_id
    from the client payload is a deliberate change."""
    client = _StubSlackClient(
        [{"id": "C001", "name": "x"}]
    )
    cache = refresh_slack_channels(
        client,
        expected_owner_id="T_CALLER",
        clock=_fixed_clock,
    )

    assert cache.workspace_id == "T_CALLER"


def test_slack_clock_called_once():
    """The clock is the sole authority for ``fetched_at``;
    pin via a counter clock that records each call."""
    calls = {"i": 0}

    def _counter_clock() -> datetime:
        calls["i"] += 1
        return _UTC_NOW

    client = _StubSlackClient([])
    refresh_slack_channels(
        client, expected_owner_id="T", clock=_counter_clock
    )

    assert calls["i"] == 1


# ===========================================================================
# Slack — client errors propagate
# ===========================================================================


def test_slack_client_error_propagates_unchanged():
    """Round-1 reviewer Q8: refresh.py does NOT wrap into
    :class:`NoCacheAndNetworkDown`; that composite belongs
    to phase 7. The original exception type + message must
    reach the caller verbatim."""
    client = _StubSlackClient([], exc=RuntimeError("network down"))

    with pytest.raises(RuntimeError, match="network down"):
        refresh_slack_channels(
            client, expected_owner_id="T", clock=_fixed_clock
        )


# ===========================================================================
# Sheets — happy paths + filter spy
# ===========================================================================


_SHEET_ROW_OK = {
    "id": "1aBcSheet",
    "name": "weekly metrics",
    "mimeType": "application/vnd.google-apps.spreadsheet",
    "parents": ["folder_root"],
}

_DOC_ROW_OK = {
    "id": "1xYzDoc",
    "name": "weekly notes",
    "mimeType": "application/vnd.google-apps.document",
    "parents": ["folder_root"],
}


def test_sheets_two_entries_in_order():
    client = _StubDriveClient(
        [
            _SHEET_ROW_OK,
            {
                "id": "1xx",
                "name": "another",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [],
            },
        ]
    )
    cache = refresh_google_sheets(
        client,
        expected_owner_id="acct@x.iam",
        clock=_fixed_clock,
    )

    assert isinstance(cache, GoogleSheetsCache)
    assert cache.account_id == "acct@x.iam"
    assert cache.fetched_at == _UTC_NOW
    assert [(it.id, it.name) for it in cache.items] == [
        ("1aBcSheet", "weekly metrics"),
        ("1xx", "another"),
    ]
    assert cache.items[0].parent_id == "folder_root"
    # Empty parents list → None.
    assert cache.items[1].parent_id is None


def test_sheets_filter_query_spies_correct_mime():
    """Round-2 reviewer L398 — defence in depth. The refresh
    function MUST pass the spreadsheet MIME filter inside the
    list_files call; pin via the stub's recorded queries."""
    client = _StubDriveClient([_SHEET_ROW_OK])
    refresh_google_sheets(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )

    assert client.queries == [
        "mimeType='application/vnd.google-apps.spreadsheet'"
    ]


def test_sheets_empty_yields_empty_items():
    client = _StubDriveClient([])
    cache = refresh_google_sheets(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )
    assert cache.items == []


def test_sheets_wrong_mime_in_row_raises_validation_error():
    """L398 defence in depth — if a buggy client returns a
    docs row in a sheets refresh, the schema's Literal
    rejects it instead of silently storing the wrong kind."""
    client = _StubDriveClient(
        [
            {
                "id": "1aBc",
                "name": "x",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [],
            }
        ]
    )
    with pytest.raises(ValidationError):
        refresh_google_sheets(
            client,
            expected_owner_id="acct@x.iam",
            clock=_fixed_clock,
        )


def test_sheets_missing_parents_key_defaults_to_none():
    """Drive returns ``parents`` for non-root items only; the
    refresh path must tolerate either an absent key or an
    empty list."""
    client = _StubDriveClient(
        [
            {
                "id": "1aBc",
                "name": "root",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                # No 'parents' key.
            }
        ]
    )
    cache = refresh_google_sheets(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )
    assert cache.items[0].parent_id is None


def test_sheets_client_error_propagates_unchanged():
    client = _StubDriveClient([], exc=RuntimeError("403 quota"))
    with pytest.raises(RuntimeError, match="403 quota"):
        refresh_google_sheets(
            client, expected_owner_id="acct@x.iam", clock=_fixed_clock
        )


# ===========================================================================
# Docs — happy paths + filter spy
# ===========================================================================


def test_docs_two_entries_in_order():
    client = _StubDriveClient(
        [
            _DOC_ROW_OK,
            {
                "id": "2yyy",
                "name": "another",
                "mimeType": "application/vnd.google-apps.document",
                "parents": [],
            },
        ]
    )
    cache = refresh_google_docs(
        client,
        expected_owner_id="acct@x.iam",
        clock=_fixed_clock,
    )

    assert isinstance(cache, GoogleDocsCache)
    assert cache.account_id == "acct@x.iam"
    assert cache.fetched_at == _UTC_NOW
    assert [(d.id, d.name) for d in cache.docs] == [
        ("1xYzDoc", "weekly notes"),
        ("2yyy", "another"),
    ]


def test_docs_filter_query_spies_correct_mime():
    client = _StubDriveClient([_DOC_ROW_OK])
    refresh_google_docs(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )

    assert client.queries == [
        "mimeType='application/vnd.google-apps.document'"
    ]


def test_docs_empty_yields_empty_docs():
    client = _StubDriveClient([])
    cache = refresh_google_docs(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )
    assert cache.docs == []


def test_docs_wrong_mime_in_row_raises_validation_error():
    client = _StubDriveClient(
        [
            {
                "id": "x",
                "name": "x",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [],
            }
        ]
    )
    with pytest.raises(ValidationError):
        refresh_google_docs(
            client, expected_owner_id="acct@x.iam", clock=_fixed_clock
        )


def test_docs_client_error_propagates_unchanged():
    client = _StubDriveClient([], exc=ConnectionError("DNS down"))
    with pytest.raises(ConnectionError, match="DNS down"):
        refresh_google_docs(
            client, expected_owner_id="acct@x.iam", clock=_fixed_clock
        )


# ===========================================================================
# Cross-kind isolation pin
# ===========================================================================


def test_sheets_refresh_filters_only_sheets_mime_in_query():
    """Pin the query string explicitly so a future refactor
    that drops the MIME constant + uses the generic
    list_files(query=None) is a surfaced failure."""
    client = _StubDriveClient([_SHEET_ROW_OK])
    refresh_google_sheets(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )
    assert client.queries[0] is not None
    assert "spreadsheet" in client.queries[0]
    assert "document" not in client.queries[0]


def test_docs_refresh_filters_only_docs_mime_in_query():
    client = _StubDriveClient([_DOC_ROW_OK])
    refresh_google_docs(
        client, expected_owner_id="acct@x.iam", clock=_fixed_clock
    )
    assert client.queries[0] is not None
    assert "document" in client.queries[0]
    assert "spreadsheet" not in client.queries[0]


# ===========================================================================
# Protocol exported surface
# ===========================================================================


def test_protocols_exported():
    """Phase 7 callers (and tests) import the Protocols by
    name; pin so a future refactor that hides them is a
    deliberate change."""
    assert hasattr(refresh_mod, "SlackChannelsClient")
    assert hasattr(refresh_mod, "GoogleDriveClient")
    # Both are Protocol subclasses.
    from typing import Protocol

    # Protocol classes have a `_is_protocol` attribute set by
    # the typing module.
    assert getattr(
        SlackChannelsClient, "_is_protocol", False
    )
    assert getattr(
        GoogleDriveClient, "_is_protocol", False
    )
