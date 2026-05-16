"""Phase 10 slice 1 — source return contract + canonical bytes.

Per ``docs/PHASE_10_PLAN.md`` §3.1 / §3.6 / §5. The
``content_hash`` + the on-disk artifact (slice 4) are
derived from ``content_bytes`` (the canonical encoding)
ONLY — so the hash is stable across serializers and
``sha256(file)`` re-verifies it with zero parsing.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.tool_tags import ToolCapabilityTag
from app.v2.sources.contract import (
    SourceLoader,
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import SourceParseError


# ===========================================================================
# canonical_bytes — §3.6 per-kind rules
# ===========================================================================


def test_text_is_verbatim_no_normalisation():
    """Text/markdown/yaml: utf-8 of the str VERBATIM. A
    trailing-newline delta MUST change the bytes (→ hash)."""
    a = canonical_bytes("text", "hello world")
    b = canonical_bytes("text", "hello world\n")
    assert a == b"hello world"
    assert a != b
    # bytes pass through unchanged
    assert canonical_bytes("markdown", b"# H\r\n") == b"# H\r\n"
    assert canonical_bytes("yaml", "k: v\n") == b"k: v\n"


def test_json_is_canonicalised_key_order_and_whitespace_invariant():
    """Two semantically-equal JSON payloads (differing key
    order) yield IDENTICAL canonical bytes → identical
    hash."""
    x = canonical_bytes("json", {"b": 1, "a": [3, 2]})
    y = canonical_bytes("json", {"a": [3, 2], "b": 1})
    assert x == y
    assert x == b'{"a":[3,2],"b":1}'
    assert canonical_bytes("dict", {"k": "v"}) == b'{"k":"v"}'
    assert canonical_bytes("list", [1, 2]) == b"[1,2]"


def test_binary_passes_raw_bytes():
    blob = bytes(range(256))
    assert canonical_bytes("binary", blob) == blob


def test_canonical_bytes_type_mismatch_raises_parse_error():
    with pytest.raises(SourceParseError):
        canonical_bytes("text", 123)
    with pytest.raises(SourceParseError):
        canonical_bytes("binary", "not-bytes")
    with pytest.raises(SourceParseError):
        canonical_bytes("json", {1, 2, 3})  # set not JSON


def test_unknown_kind_raises_parse_error():
    with pytest.raises(SourceParseError):
        canonical_bytes("pdf", b"x")


# ===========================================================================
# content_hash_for — the sole hash function
# ===========================================================================


def test_content_hash_shape_and_value():
    cb = b"payload"
    h = content_hash_for(cb)
    assert h == "sha256:" + hashlib.sha256(cb).hexdigest()
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_hash_stable_across_equal_json_serializations():
    h1 = content_hash_for(canonical_bytes("json", {"b": 1, "a": 2}))
    h2 = content_hash_for(canonical_bytes("json", {"a": 2, "b": 1}))
    assert h1 == h2


def test_hash_differs_on_text_newline_delta():
    h1 = content_hash_for(canonical_bytes("text", "x"))
    h2 = content_hash_for(canonical_bytes("text", "x\n"))
    assert h1 != h2


# ===========================================================================
# SourceResult model
# ===========================================================================


def _ok_kwargs(**ov):
    cb = canonical_bytes("text", "hi")
    base = dict(
        content="hi",
        content_bytes=cb,
        source_kind="source_literal",
        source_id="sales_30d",
        fetched_at=datetime(2026, 5, 16, tzinfo=timezone.utc),
        content_hash=content_hash_for(cb),
        item_count=1,
        source_version=None,
        selection_method=SelectionMethod.CONTENT_HASH,
    )
    base.update(ov)
    return base


def test_source_result_round_trip():
    r = SourceResult(**_ok_kwargs())
    assert r.content_hash == content_hash_for(r.content_bytes)
    assert r.selection_method is SelectionMethod.CONTENT_HASH


def test_source_result_naive_fetched_at_rejected():
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(fetched_at=datetime(2026, 5, 16)))


def test_source_result_fetched_at_normalised_to_utc():
    from datetime import timedelta

    tz = timezone(timedelta(hours=3))
    r = SourceResult(
        **_ok_kwargs(fetched_at=datetime(2026, 5, 16, 3, tzinfo=tz))
    )
    assert r.fetched_at.utcoffset() == timedelta(0)


def test_source_result_bad_content_hash_rejected():
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(content_hash="deadbeef"))
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(content_hash="sha256:XYZ"))
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(content_hash="sha256:" + "a" * 63))


def test_source_result_negative_item_count_rejected():
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(item_count=-1))


def test_source_result_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SourceResult(**_ok_kwargs(surprise=1))


# ===========================================================================
# SourceLoader protocol
# ===========================================================================


def test_source_loader_is_runtime_checkable_protocol():
    class _Good:
        descriptor = SourceDescriptor(
            id="source_literal",
            description="frozen literal text source",
            tags={ToolCapabilityTag.READ_EXTERNAL},
            supports_versioning=False,
            supported_selection_methods=[SelectionMethod.CONTENT_HASH],
        )

        async def load(self, *, args, as_of_datetime, clock):
            return SourceResult(**_ok_kwargs())

    assert isinstance(_Good(), SourceLoader)

    class _Bad:  # no descriptor / no load
        pass

    assert not isinstance(_Bad(), SourceLoader)


def test_source_loader_load_is_async_and_keyword_only():
    sig = inspect.signature(SourceLoader.load)
    params = list(sig.parameters)
    assert params == ["self", "args", "as_of_datetime", "clock"]
    for p in ("args", "as_of_datetime", "clock"):
        assert (
            sig.parameters[p].kind
            is inspect.Parameter.KEYWORD_ONLY
        )
