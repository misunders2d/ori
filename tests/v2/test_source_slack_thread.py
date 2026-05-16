"""Phase 10 slice 6 — ``source_slack_thread`` loader.

Per ``docs/PHASE_10_PLAN.md`` §1.1 / §3.5 / §5 + Q3
(Protocol DI). A fake :class:`SlackThreadReader` (no live
Slack); the Slack error-code → typed-taxonomy mapping
pinned exhaustively, with the slice-5 non-fallback /
fallback-eligible classification asserted per class.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import DuplicateDescriptorError, SourceRegistry
from app.v2.sources.contract import SourceLoader, content_hash_for
from app.v2.sources.errors import (
    SourceAuthError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
)
from app.v2.sources.slack_thread import (
    SOURCE_SLACK_THREAD_ID,
    SlackThreadReader,
    SlackThreadSource,
    register_slack_thread,
    slack_thread_source,
)
from app.v2.sources.registry import SourceLoaderRegistry
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _T0


class _FakeReader:
    """Configurable fake SlackThreadReader: returns a
    canned mapping, or raises a transport exception."""

    def __init__(self, *, resp=None, raises=None):
        self.calls = 0
        self._resp = resp
        self._raises = raises

    async def read_thread(self, *, channel, thread_ts):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._resp


def _src(reader) -> SlackThreadSource:
    return SlackThreadSource(reader=reader)


async def _load(src, **extra):
    args = {
        "source_id": "in1",
        "channel": "C123",
        "thread_ts": "1700000000.000100",
        **extra,
    }
    return await src.load(
        args=args, as_of_datetime=None, clock=_clock
    )


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_thread_read_verified_result():
    msgs = [
        {"ts": "1.0", "text": "hi"},
        {"ts": "2.0", "text": "there"},
    ]
    reader = _FakeReader(resp={"ok": True, "messages": msgs})
    r = await _load(_src(reader))

    assert r.content == msgs
    assert r.source_kind == SOURCE_SLACK_THREAD_ID
    assert r.source_id == "in1"
    assert r.item_count == 2
    assert r.selection_method is SelectionMethod.CONTENT_HASH
    assert r.fetched_at == _T0
    assert r.content_hash == content_hash_for(r.content_bytes)
    # canonical list bytes (sorted-key JSON)
    assert r.content_bytes == (
        b'[{"text":"hi","ts":"1.0"},{"text":"there","ts":"2.0"}]'
    )
    assert reader.calls == 1


@pytest.mark.asyncio
async def test_empty_thread_is_ok_zero_items():
    reader = _FakeReader(resp={"ok": True, "messages": []})
    r = await _load(_src(reader))
    assert r.item_count == 0
    assert r.content_bytes == b"[]"


# ===========================================================================
# Slack error-code → typed taxonomy + fallback classification
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "not_authed",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "missing_scope",
    ],
)
async def test_auth_errors_map_to_non_fallback_auth(error):
    reader = _FakeReader(resp={"ok": False, "error": error})
    with pytest.raises(SourceAuthError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is False
    assert ei.value.payload_code == f"slack_{error}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "ratelimited",
        "service_unavailable",
        "internal_error",
        "fatal_error",
        "request_timeout",
        "channel_not_found",
        "thread_not_found",
        "not_in_channel",
        "some_unknown_code",
    ],
)
async def test_transient_and_missing_and_unknown_map_to_fetch(error):
    reader = _FakeReader(resp={"ok": False, "error": error})
    with pytest.raises(SourceFetchError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is True


@pytest.mark.asyncio
async def test_unconfigured_reader_maps_to_non_fallback_policy():
    """The safe-default singleton's reader → SourcePolicyError
    (NON-fallback): fails closed, never serves a stale
    snapshot until a real reader is wired."""
    with pytest.raises(SourcePolicyError) as ei:
        await _load(slack_thread_source)
    assert ei.value.payload_code == "slack_reader_unconfigured"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_transport_exception_maps_to_fetch():
    reader = _FakeReader(raises=RuntimeError("connection reset"))
    with pytest.raises(SourceFetchError) as ei:
        await _load(_src(reader))
    assert ei.value.payload_code == "slack_transport_error"
    assert ei.value.fallback_eligible is True


# ===========================================================================
# Malformed reader responses → non-fallback SourceParseError
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp",
    [
        "not-a-mapping",
        {"ok": True},  # missing messages
        {"ok": True, "messages": "not-a-list"},
        {"ok": True, "messages": {"k": "v"}},
    ],
)
async def test_malformed_response_maps_to_parse_error(resp):
    reader = _FakeReader(resp=resp)
    with pytest.raises(SourceParseError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_ok_false_without_error_string_is_fetch():
    reader = _FakeReader(resp={"ok": False})
    with pytest.raises(SourceFetchError):
        await _load(_src(reader))


@pytest.mark.asyncio
async def test_strict_ok_truthy_non_bool_is_not_ok():
    """`ok: 1` (truthy non-bool) is NOT success — strict
    identity (codex slice-6 🔵 / phase-9 parity). With no
    error string → conservative fetch branch."""
    reader = _FakeReader(
        resp={"ok": 1, "messages": [{"ts": "1.0"}]}
    )
    with pytest.raises(SourceFetchError):
        await _load(_src(reader))


# ===========================================================================
# Args
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"channel": None},
        {"thread_ts": ""},
        {"source_id": ""},
    ],
)
async def test_bad_args_raise_parse_error(extra):
    reader = _FakeReader(resp={"ok": True, "messages": []})
    # build args dict and override
    args = {
        "source_id": "in1",
        "channel": "C1",
        "thread_ts": "1.0",
    }
    args.update(extra)
    with pytest.raises(SourceParseError) as ei:
        await _src(reader).load(
            args=args, as_of_datetime=None, clock=_clock
        )
    assert ei.value.payload_code == "source_slack_thread_args_invalid"


@pytest.mark.asyncio
async def test_missing_required_arg_raises_parse_error():
    reader = _FakeReader(resp={"ok": True, "messages": []})
    with pytest.raises(SourceParseError):
        await _src(reader).load(
            args={"source_id": "in1", "channel": "C1"},
            as_of_datetime=None,
            clock=_clock,
        )


# ===========================================================================
# Protocol / descriptor / registration
# ===========================================================================


def test_reader_protocol_runtime_checkable():
    assert isinstance(_FakeReader(), SlackThreadReader)

    class _Bad:
        pass

    assert not isinstance(_Bad(), SlackThreadReader)


def test_loader_satisfies_source_loader_protocol():
    assert isinstance(slack_thread_source, SourceLoader)
    assert isinstance(_src(_FakeReader()), SourceLoader)


def test_descriptor_read_only_valid():
    d = slack_thread_source.descriptor
    assert isinstance(d, SourceDescriptor)
    assert d.id == SOURCE_SLACK_THREAD_ID
    assert ToolCapabilityTag.READ_EXTERNAL in d.tags
    assert not (
        d.tags
        & {
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        }
    )


def test_register_into_fresh_registries():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    register_slack_thread(sources=sources, loaders=loaders)
    assert sources.lookup(SOURCE_SLACK_THREAD_ID) is (
        slack_thread_source.descriptor
    )
    assert loaders.require(SOURCE_SLACK_THREAD_ID) is slack_thread_source
    with pytest.raises(DuplicateDescriptorError):
        register_slack_thread(sources=sources, loaders=loaders)


def test_builtin_registered_on_import():
    import app.v2.sources as src

    assert SOURCE_SLACK_THREAD_ID in src.SOURCE_LOADERS
    assert src.SOURCES.lookup(SOURCE_SLACK_THREAD_ID) is not None
