"""Phase 10 slice 2 — ``source_literal`` loader + registration.

Per ``docs/PHASE_10_PLAN.md`` §1.1 / §5. Zero-I/O frozen
literal: verbatim bytes, verified SourceResult, typed
SourceParseError on bad args, registered into both the
descriptor and loader registries.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import (
    DuplicateDescriptorError,
    SourceRegistry,
    UnknownSourceError,
)
from app.v2.sources.contract import SourceLoader, content_hash_for
from app.v2.sources.errors import SourceParseError
from app.v2.sources.literal import (
    SOURCE_LITERAL_ID,
    LiteralSource,
    literal_source,
    register_literal,
)
from app.v2.sources.registry import (
    SourceLoaderRegistry,
    register_source_loader,
)
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _T0


# ===========================================================================
# Loader behaviour
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_returns_verified_source_result():
    r = await literal_source.load(
        args={"source_id": "reminder_body", "text": "stand-up at 9"},
        as_of_datetime=None,
        clock=_clock,
    )
    assert r.content == "stand-up at 9"
    assert r.content_bytes == b"stand-up at 9"
    assert r.source_kind == SOURCE_LITERAL_ID
    assert r.source_id == "reminder_body"
    assert r.item_count == 1
    assert r.source_version is None
    assert r.selection_method is SelectionMethod.CONTENT_HASH
    assert r.fetched_at == _T0
    # content_hash is the DERIVED hash (slice-1 model
    # validator already enforces it; assert it explicitly).
    assert r.content_hash == content_hash_for(b"stand-up at 9")


@pytest.mark.asyncio
async def test_text_is_verbatim_newline_sensitive():
    a = await literal_source.load(
        args={"source_id": "x", "text": "hello"},
        as_of_datetime=None,
        clock=_clock,
    )
    b = await literal_source.load(
        args={"source_id": "x", "text": "hello\n"},
        as_of_datetime=None,
        clock=_clock,
    )
    assert a.content_bytes == b"hello"
    assert b.content_bytes == b"hello\n"
    assert a.content_hash != b.content_hash


@pytest.mark.asyncio
async def test_as_of_datetime_is_ignored_zero_io():
    """A literal is already frozen — as_of_datetime must
    not change the result (and there is no I/O to do)."""
    common = {"source_id": "x", "text": "frozen"}
    r1 = await literal_source.load(
        args=common, as_of_datetime=None, clock=_clock
    )
    r2 = await literal_source.load(
        args=common,
        as_of_datetime=datetime(2020, 1, 1, tzinfo=timezone.utc),
        clock=_clock,
    )
    assert r1.content_hash == r2.content_hash
    assert r1.content_bytes == r2.content_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"text": "no source id"},
        {"source_id": "x"},
        {"source_id": "", "text": "t"},
        {"source_id": "x", "text": ""},
        {"source_id": "x", "text": 123},
        {"source_id": 5, "text": "t"},
        {},
    ],
)
async def test_bad_args_raise_source_parse_error(args):
    with pytest.raises(SourceParseError) as ei:
        await literal_source.load(
            args=args, as_of_datetime=None, clock=_clock
        )
    assert ei.value.payload_code == "source_literal_args_invalid"
    # non-fallback (a misconfigured literal must never serve
    # a stale cached snapshot).
    assert ei.value.fallback_eligible is False


def test_literal_source_satisfies_protocol():
    assert isinstance(literal_source, SourceLoader)
    assert isinstance(LiteralSource(), SourceLoader)


# ===========================================================================
# Descriptor
# ===========================================================================


def test_descriptor_is_read_only_valid():
    d = literal_source.descriptor
    assert isinstance(d, SourceDescriptor)
    assert d.id == SOURCE_LITERAL_ID
    assert ToolCapabilityTag.READ_EXTERNAL in d.tags
    assert not (
        d.tags
        & {
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        }
    )
    assert d.supports_versioning is False
    assert d.supported_selection_methods == [
        SelectionMethod.CONTENT_HASH
    ]


# ===========================================================================
# Registration (fresh registries — no singleton mutation)
# ===========================================================================


def test_register_into_fresh_registries():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    register_literal(sources=sources, loaders=loaders)

    assert sources.lookup(SOURCE_LITERAL_ID) is literal_source.descriptor
    assert loaders.require(SOURCE_LITERAL_ID) is literal_source
    assert SOURCE_LITERAL_ID in loaders


def test_duplicate_registration_rejected():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    register_literal(sources=sources, loaders=loaders)
    with pytest.raises(DuplicateDescriptorError):
        register_literal(sources=sources, loaders=loaders)


def test_no_half_registration_when_descriptor_preexists():
    """If the descriptor id is already in the descriptor
    registry, register_source_loader refuses BEFORE adding
    the loader instance — no half-registered state."""
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    sources.register(literal_source.descriptor)  # descriptor only

    with pytest.raises(DuplicateDescriptorError):
        register_source_loader(
            literal_source, sources=sources, loaders=loaders
        )
    # loader registry stayed empty.
    assert SOURCE_LITERAL_ID not in loaders
    assert len(loaders) == 0


def test_unknown_loader_require_raises():
    loaders = SourceLoaderRegistry()
    with pytest.raises(UnknownSourceError):
        loaders.require("source_nope")


# ===========================================================================
# Paired registration is ATOMIC (codex slice-2 🟡 #1)
# ===========================================================================


def test_register_rolls_back_loader_when_descriptor_step_fails():
    """If the descriptor-side step raises AFTER the
    loader-side step succeeded, the loader insert is rolled
    back — NEITHER side is left registered (no permanent
    split-brain)."""

    class _BoomSources:
        # lookup() -> None so the dup pre-check passes and
        # we actually reach step 2; register() blows up
        # AFTER loaders.register(loader) has run.
        def lookup(self, key):
            return None

        def register(self, descriptor):
            raise RuntimeError("descriptor step boom")

    loaders = SourceLoaderRegistry()
    with pytest.raises(RuntimeError, match="descriptor step boom"):
        register_source_loader(
            literal_source, sources=_BoomSources(), loaders=loaders
        )

    # Step 1 rolled back: loader registry is empty.
    assert SOURCE_LITERAL_ID not in loaders
    assert len(loaders) == 0


# ===========================================================================
# Import-hook split-brain detection (codex slice-2 🟡 #2)
# ===========================================================================


def _builtin_hook():
    import app.v2.sources as src

    return src._register_builtin_sources


def test_register_builtins_both_absent_registers_paired():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    _builtin_hook()(sources=sources, loaders=loaders)
    assert sources.lookup(SOURCE_LITERAL_ID) is not None
    assert SOURCE_LITERAL_ID in loaders


def test_register_builtins_both_present_is_idempotent():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    hook = _builtin_hook()
    hook(sources=sources, loaders=loaders)
    # Second call against the now-paired registries — no raise.
    hook(sources=sources, loaders=loaders)
    assert SOURCE_LITERAL_ID in loaders


def test_register_builtins_descriptor_only_half_state_raises():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    sources.register(literal_source.descriptor)  # descriptor ONLY
    with pytest.raises(RuntimeError, match="split-brain"):
        _builtin_hook()(sources=sources, loaders=loaders)


def test_register_builtins_loader_only_half_state_raises():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    loaders.register(literal_source)  # loader ONLY
    with pytest.raises(RuntimeError, match="split-brain"):
        _builtin_hook()(sources=sources, loaders=loaders)


# ===========================================================================
# Production singletons — registered on package import, idempotent
# ===========================================================================


def test_builtin_registered_on_import():
    import app.v2.sources as src

    assert SOURCE_LITERAL_ID in src.SOURCE_LOADERS
    assert src.SOURCE_LOADERS.require(SOURCE_LITERAL_ID) is literal_source
    assert src.SOURCES.lookup(SOURCE_LITERAL_ID) is not None


def test_register_builtins_is_idempotent():
    """Re-running the registration hook must NOT raise even
    though the singletons already carry source_literal."""
    import app.v2.sources as src

    src._register_builtin_sources()  # second call — no-op
    assert SOURCE_LITERAL_ID in src.SOURCE_LOADERS
