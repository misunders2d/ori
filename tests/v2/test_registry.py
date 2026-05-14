"""Tests for ``app.v2.registry``.

Pins per ``docs/PHASE_2_PLAN.md`` slice 2 scope:

- Each registry accepts only its own descriptor type.
- Duplicate keys rejected with ``DuplicateDescriptorError``.
- ``lookup`` returns ``None`` for unknown keys; ``require``
  raises the registry-specific subclass of ``KeyError``.
- ``ToolRegistry.tags_for(unknown)`` returns the fail-safe set
  ``{WRITE_EXTERNAL}`` (design §5.4) — which then composes with
  ``is_blocked_by_read_only_reasoning`` to block read-only
  reasoning from invoking unknowns.
- ``SourceRegistry.register`` re-asserts the read-only invariant
  at the boundary (belt against ``model_construct`` bypass).
- ``EmitRegistry.register`` re-asserts the side-effect invariant.
- Module is metadata only: no I/O imports, no dispatch surface.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4 (fail-safe default tags)
- ``docs/PHASE_2_PLAN.md`` slice 2
"""

from __future__ import annotations

import inspect

import pytest

from app.v2.descriptors.emit import EmitDescriptor
from app.v2.descriptors.source import SourceDescriptor
from app.v2.descriptors.tool import ToolDescriptor
from app.v2.enums import SelectionMethod
from app.v2 import registry as registry_mod
from app.v2.registry import (
    EMITS,
    EmitRegistry,
    FAIL_SAFE_UNKNOWN_TAGS,
    InvalidEmitDescriptorError,
    InvalidSourceDescriptorError,
    DuplicateDescriptorError,
    SOURCES,
    SourceRegistry,
    TOOLS,
    ToolRegistry,
    UnknownEmitError,
    UnknownSourceError,
    UnknownToolError,
)
from app.v2.tool_tags import (
    ToolCapabilityTag,
    is_blocked_by_read_only_reasoning,
    requires_admin_approval,
)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _tool(name: str = "slack_post_message") -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="post a message into a Slack channel",
        tags={
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
        },
        module="app.v2.adapters.slack",
    )


def _source(id_: str = "source_drive_file") -> SourceDescriptor:
    return SourceDescriptor(
        id=id_,
        description="load a Google Drive file by id",
        tags={ToolCapabilityTag.READ_EXTERNAL, ToolCapabilityTag.USES_OAUTH},
        supports_versioning=True,
        supported_selection_methods=[SelectionMethod.STABLE_ID],
    )


def _emit(id_: str = "slack_post_message") -> EmitDescriptor:
    return EmitDescriptor(
        id=id_,
        description="post a message into a Slack channel",
        tags={
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
        },
        target_kind="slack",
        supports_native_dedup=True,
    )


# ===========================================================================
# ToolRegistry
# ===========================================================================


def test_tool_register_then_lookup():
    r = ToolRegistry()
    d = _tool()
    r.register(d)
    assert r.lookup("slack_post_message") is d
    assert r.require("slack_post_message") is d


def test_tool_lookup_returns_none_for_unknown():
    r = ToolRegistry()
    assert r.lookup("ghost_tool") is None


def test_tool_require_raises_for_unknown():
    r = ToolRegistry()
    with pytest.raises(UnknownToolError):
        r.require("ghost_tool")


def test_tool_register_duplicate_raises():
    r = ToolRegistry()
    r.register(_tool())
    with pytest.raises(DuplicateDescriptorError):
        r.register(_tool())


def test_tool_register_rejects_wrong_type():
    """A SourceDescriptor passed to ToolRegistry must be rejected
    so the typing contract holds at runtime, not just in mypy."""
    r = ToolRegistry()
    with pytest.raises(TypeError):
        r.register(_source())  # type: ignore[arg-type]


def test_tool_tags_for_returns_descriptor_tags_when_known():
    r = ToolRegistry()
    r.register(_tool())
    tags = r.tags_for("slack_post_message")
    assert tags == frozenset(
        {ToolCapabilityTag.WRITE_EXTERNAL, ToolCapabilityTag.SEND_MESSAGE}
    )


def test_tool_tags_for_returns_fail_safe_when_unknown():
    r = ToolRegistry()
    assert r.tags_for("ghost_tool") == FAIL_SAFE_UNKNOWN_TAGS


def test_fail_safe_tags_block_read_only_reasoning():
    """Design §5.4: an unknown tool gets WRITE_EXTERNAL, which
    is precisely a tag the read-only-reasoning gate blocks. So
    the registry's fail-safe behavior immediately enforces the
    intended policy without needing additional plumbing."""
    assert is_blocked_by_read_only_reasoning(FAIL_SAFE_UNKNOWN_TAGS) is True


def test_fail_safe_tags_do_not_trigger_admin_gate():
    """The fail-safe is for the read-only guard, NOT the admin
    approval gate. ``Previously-unused adapter`` friction
    (design §5.9) is a separate runtime concern from
    ``unrecognised tool name``."""
    assert requires_admin_approval(FAIL_SAFE_UNKNOWN_TAGS) is False


def test_tool_membership_and_iteration():
    r = ToolRegistry()
    r.register(_tool("a"))
    r.register(_tool("b"))
    assert "a" in r
    assert "b" in r
    assert "ghost" not in r
    assert {d.name for d in r} == {"a", "b"}
    assert r.keys() == ["a", "b"]
    assert len(r) == 2


def test_tool_clear_empties_registry():
    r = ToolRegistry()
    r.register(_tool())
    r.clear()
    assert r.lookup("slack_post_message") is None
    assert len(r) == 0


# ===========================================================================
# SourceRegistry
# ===========================================================================


def test_source_register_then_lookup():
    r = SourceRegistry()
    d = _source()
    r.register(d)
    assert r.lookup("source_drive_file") is d
    assert r.require("source_drive_file") is d


def test_source_require_raises_for_unknown():
    r = SourceRegistry()
    with pytest.raises(UnknownSourceError):
        r.require("source_ghost")


def test_source_register_duplicate_raises():
    r = SourceRegistry()
    r.register(_source())
    with pytest.raises(DuplicateDescriptorError):
        r.register(_source())


def test_source_register_rejects_wrong_type():
    r = SourceRegistry()
    with pytest.raises(TypeError):
        r.register(_tool())  # type: ignore[arg-type]


def test_source_register_belt_rejects_model_construct_bypass():
    """``model_construct`` bypasses Pydantic validators. The
    registry's defensive check re-asserts the read-only
    invariant at the registration boundary so a smuggled
    write-tagged descriptor still fails closed."""
    bad = SourceDescriptor.model_construct(
        id="source_evil",
        description="bypasses validators",
        tags={
            ToolCapabilityTag.READ_EXTERNAL,
            ToolCapabilityTag.WRITE_EXTERNAL,
        },
        supports_versioning=False,
        supported_selection_methods=[SelectionMethod.STABLE_ID],
    )
    r = SourceRegistry()
    with pytest.raises(InvalidSourceDescriptorError, match="write-side"):
        r.register(bad)


def test_source_register_belt_rejects_missing_read_external():
    """The Pydantic validator catches this at construction, but
    the registry belt repeats the check for model_construct
    bypass scenarios."""
    bad = SourceDescriptor.model_construct(
        id="source_evil",
        description="bypasses validators",
        tags={ToolCapabilityTag.USES_OAUTH},
        supports_versioning=False,
        supported_selection_methods=[SelectionMethod.STABLE_ID],
    )
    r = SourceRegistry()
    with pytest.raises(InvalidSourceDescriptorError, match="READ_EXTERNAL"):
        r.register(bad)


# ===========================================================================
# EmitRegistry
# ===========================================================================


def test_emit_register_then_lookup():
    r = EmitRegistry()
    d = _emit()
    r.register(d)
    assert r.lookup("slack_post_message") is d
    assert r.require("slack_post_message") is d


def test_emit_require_raises_for_unknown():
    r = EmitRegistry()
    with pytest.raises(UnknownEmitError):
        r.require("emit_ghost")


def test_emit_register_duplicate_raises():
    r = EmitRegistry()
    r.register(_emit())
    with pytest.raises(DuplicateDescriptorError):
        r.register(_emit())


def test_emit_register_rejects_wrong_type():
    r = EmitRegistry()
    with pytest.raises(TypeError):
        r.register(_tool())  # type: ignore[arg-type]


def test_emit_register_belt_rejects_model_construct_bypass():
    """An emit descriptor that doesn't carry a side-effecting
    tag must be rejected even when constructed via
    ``model_construct``."""
    bad = EmitDescriptor.model_construct(
        id="emit_noop",
        description="produces no side effect",
        tags={ToolCapabilityTag.READ_EXTERNAL},
        target_kind="ghost",
        supports_native_dedup=False,
    )
    r = EmitRegistry()
    with pytest.raises(
        InvalidEmitDescriptorError, match="SEND_MESSAGE or WRITE_EXTERNAL"
    ):
        r.register(bad)


# ===========================================================================
# Cross-registry isolation
# ===========================================================================


def test_registries_are_independent_instances():
    a = ToolRegistry()
    b = ToolRegistry()
    a.register(_tool())
    assert "slack_post_message" in a
    assert "slack_post_message" not in b


def test_module_level_singletons_exposed():
    """Production code uses the module-level singletons. Tests
    should construct fresh instances rather than mutating these."""
    assert isinstance(TOOLS, ToolRegistry)
    assert isinstance(SOURCES, SourceRegistry)
    assert isinstance(EMITS, EmitRegistry)


# ===========================================================================
# No-execution invariant: the registry module must not import any
# I/O-side libraries. If a future refactor accidentally adds one
# this fails loudly. The list is the set of libs an adapter
# implementation would plausibly pull in.
# ===========================================================================


def test_registry_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for name, member in vars(registry_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"app.v2.registry imports I/O libs that suggest runtime "
        f"dispatch was added: {sorted(leaked)}. Phase 2 is metadata "
        "only — keep this layer execution-free."
    )


def test_registry_module_has_no_dispatch_methods():
    """The registry surface is intentionally narrow: register +
    lookup + require + tags_for + dunder methods + keys/clear.
    Any new method whose name suggests dispatch (``call``,
    ``invoke``, ``dispatch``, ``execute``) is a phase-boundary
    violation."""
    forbidden_method_names = {"call", "invoke", "dispatch", "execute", "run", "send"}
    for cls in (ToolRegistry, SourceRegistry, EmitRegistry):
        method_names = {
            name
            for name, _ in inspect.getmembers(cls, callable)
            if not name.startswith("_")
        }
        leaked = method_names & forbidden_method_names
        assert not leaked, (
            f"{cls.__name__} exposes execution-suggestive methods: "
            f"{sorted(leaked)}. The registry is metadata-only in phase 2."
        )
