"""Tests for ``app.v2.toolsets.authoring``.

Phase 7 slice 6 per ``docs/PHASE_7_PLAN.md`` §5.7.

Pins:
- AuthoringToolset.get_tools returns a non-empty list of
  FunctionTool instances covering every documented tool name.
- Each tool's name / description non-empty.
- Toolset does NOT auto-register with any agent on import.
- ToolDescriptor tag matrix: every authoring tool registered
  with the exact §4 tag set (L676).
- New tags exist (L785 + L807): FILESYSTEM_READ, DB_WRITE.
- Read-only blocking policy update (L807):
  - is_blocked_by_read_only_reasoning({DB_WRITE}) → True.
  - is_blocked_by_read_only_reasoning({FILESYSTEM_READ}) → False.
  - Mixed set still blocks on DB_WRITE.
- Admin-approval policy unchanged for the new tags:
  - requires_admin_approval({DB_WRITE}) → False.
  - requires_admin_approval({FILESYSTEM_READ}) → False.
- Smoke: package imports no slack_sdk / googleapiclient
  at module load.
"""

from __future__ import annotations

import ast
import inspect
import sys

import pytest
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from app.v2.descriptors.tool import ToolDescriptor
from app.v2.registry import ToolRegistry
from app.v2.tool_tags import (
    ToolCapabilityTag,
    is_blocked_by_read_only_reasoning,
    requires_admin_approval,
)
from app.v2.toolsets import authoring as toolset_mod
from app.v2.toolsets.authoring import (
    AUTHORING_TOOL_DESCRIPTORS,
    AuthoringToolset,
    register_descriptors,
)


_EXPECTED_TAG_MATRIX: dict[str, set[ToolCapabilityTag]] = {
    # Draft setters → filesystem_write.
    "schedule_draft_start": {ToolCapabilityTag.FILESYSTEM_WRITE},
    "schedule_set_description": {ToolCapabilityTag.FILESYSTEM_WRITE},
    "schedule_set_owner": {ToolCapabilityTag.FILESYSTEM_WRITE},
    "schedule_set_cron": {ToolCapabilityTag.FILESYSTEM_WRITE},
    "schedule_set_one_off": {ToolCapabilityTag.FILESYSTEM_WRITE},
    "schedule_set_failure_policy": {
        ToolCapabilityTag.FILESYSTEM_WRITE
    },
    # Delivery → read_external + uses_oauth + filesystem_write.
    "schedule_set_delivery": {
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.USES_OAUTH,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    },
    # Compile / list → filesystem_read.
    "schedule_draft_compile": {ToolCapabilityTag.FILESYSTEM_READ},
    "schedule_draft_list": {ToolCapabilityTag.FILESYSTEM_READ},
    # Discard → filesystem_write.
    "schedule_draft_discard": {ToolCapabilityTag.FILESYSTEM_WRITE},
    # Phase 8 slice 5 additions:
    # Dry-run reads draft + writes handshake.
    "schedule_dry_run": {
        ToolCapabilityTag.FILESYSTEM_READ,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    },
    # Freeze reads draft + handshake; no mutation.
    "schedule_freeze": {ToolCapabilityTag.FILESYSTEM_READ},
    # Commit: DB insert + draft/handshake delete.
    "schedule_draft_commit": {
        ToolCapabilityTag.DB_WRITE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    },
    # Lifecycle → db_write.
    "schedule_pause": {ToolCapabilityTag.DB_WRITE},
    "schedule_resume": {ToolCapabilityTag.DB_WRITE},
    "schedule_archive": {ToolCapabilityTag.DB_WRITE},
    "schedule_revive": {ToolCapabilityTag.DB_WRITE},
    # Phase 9 slice 3 — template authoring tool.
    "schedule_create_reminder": {
        ToolCapabilityTag.DB_WRITE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.USES_OAUTH,
    },
}


# ===========================================================================
# Toolset surface
# ===========================================================================


@pytest.mark.asyncio
async def test_get_tools_returns_function_tools_for_every_name():
    """Every documented authoring tool surfaces as a
    FunctionTool with non-empty name + description."""
    toolset = AuthoringToolset(expected_owner_id="T_TEST")
    tools = await toolset.get_tools()

    assert len(tools) == len(_EXPECTED_TAG_MATRIX)
    for tool in tools:
        assert isinstance(tool, FunctionTool)
        # FunctionTool exposes `.name` and `.description` from
        # the wrapped function's metadata.
        assert tool.name, f"empty name on {tool!r}"
        assert tool.description, f"empty description on {tool!r}"


@pytest.mark.asyncio
async def test_get_tools_covers_exact_documented_names():
    toolset = AuthoringToolset(expected_owner_id="T_TEST")
    tools = await toolset.get_tools()

    names = {tool.name for tool in tools}
    assert names == set(_EXPECTED_TAG_MATRIX.keys())


def test_authoring_toolset_is_base_toolset_subclass():
    assert issubclass(AuthoringToolset, BaseToolset)


# ===========================================================================
# ToolDescriptor tag matrix (round-3 reviewer L676)
# ===========================================================================


def test_descriptors_cover_every_documented_tool():
    """One descriptor per tool; no extras."""
    declared_names = {d.name for d in AUTHORING_TOOL_DESCRIPTORS}
    assert declared_names == set(_EXPECTED_TAG_MATRIX.keys())


@pytest.mark.parametrize(
    "name, expected_tags",
    sorted(_EXPECTED_TAG_MATRIX.items()),
)
def test_descriptor_tags_match_expected_matrix(name, expected_tags):
    descriptor = next(
        d for d in AUTHORING_TOOL_DESCRIPTORS if d.name == name
    )
    assert descriptor.tags == expected_tags


def test_register_descriptors_registers_every_authoring_tool():
    registry = ToolRegistry()
    register_descriptors(registry)

    assert len(registry) == len(_EXPECTED_TAG_MATRIX)
    for name, expected_tags in _EXPECTED_TAG_MATRIX.items():
        descriptor = registry.require(name)
        assert isinstance(descriptor, ToolDescriptor)
        assert descriptor.tags == expected_tags


def test_register_descriptors_duplicate_raises():
    from app.v2.registry import DuplicateDescriptorError

    registry = ToolRegistry()
    register_descriptors(registry)
    with pytest.raises(DuplicateDescriptorError):
        register_descriptors(registry)


# ===========================================================================
# New tag enum values (round-3 reviewer L785 + L807)
# ===========================================================================


def test_filesystem_read_is_enum_value():
    assert ToolCapabilityTag.FILESYSTEM_READ.value == "filesystem_read"


def test_db_write_is_enum_value():
    assert ToolCapabilityTag.DB_WRITE.value == "db_write"


# ===========================================================================
# Read-only blocking policy (L807)
# ===========================================================================


def test_db_write_alone_blocks_read_only():
    assert (
        is_blocked_by_read_only_reasoning(
            {ToolCapabilityTag.DB_WRITE}
        )
        is True
    )


def test_filesystem_read_alone_does_not_block_read_only():
    assert (
        is_blocked_by_read_only_reasoning(
            {ToolCapabilityTag.FILESYSTEM_READ}
        )
        is False
    )


def test_mixed_filesystem_read_db_write_blocks():
    """Set intersection still hits via DB_WRITE."""
    assert (
        is_blocked_by_read_only_reasoning(
            {
                ToolCapabilityTag.FILESYSTEM_READ,
                ToolCapabilityTag.DB_WRITE,
            }
        )
        is True
    )


# ===========================================================================
# Admin-approval policy (L807 — unchanged for new tags)
# ===========================================================================


def test_db_write_alone_does_not_require_admin():
    assert (
        requires_admin_approval(
            {ToolCapabilityTag.DB_WRITE}
        )
        is False
    )


def test_filesystem_read_alone_does_not_require_admin():
    assert (
        requires_admin_approval(
            {ToolCapabilityTag.FILESYSTEM_READ}
        )
        is False
    )


# ===========================================================================
# Phase 8 slice 5 — 17 tool count + new descriptor pins
# ===========================================================================


@pytest.mark.asyncio
async def test_get_tools_returns_18_tools_phase_9():
    """Phase 7 shipped 14 tools; phase 8 slice 5 added 3
    (dry_run / freeze / commit) → 17. Phase 9 slice 3
    adds schedule_create_reminder → 18."""
    toolset = AuthoringToolset(expected_owner_id="T_TEST")
    tools = await toolset.get_tools()
    assert len(tools) == 18


@pytest.mark.asyncio
async def test_get_tools_includes_phase_8_names():
    toolset = AuthoringToolset(expected_owner_id="T_TEST")
    tools = await toolset.get_tools()
    names = {tool.name for tool in tools}
    assert "schedule_dry_run" in names
    assert "schedule_freeze" in names
    assert "schedule_draft_commit" in names
    assert "schedule_create_reminder" in names


def test_descriptors_count_matches_18():
    assert len(AUTHORING_TOOL_DESCRIPTORS) == 18


def test_dry_run_descriptor_tags():
    d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_dry_run"
    )
    assert d.tags == {
        ToolCapabilityTag.FILESYSTEM_READ,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    }


def test_freeze_descriptor_tags():
    d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_freeze"
    )
    assert d.tags == {ToolCapabilityTag.FILESYSTEM_READ}


def test_commit_descriptor_tags():
    d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_draft_commit"
    )
    assert d.tags == {
        ToolCapabilityTag.DB_WRITE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    }


def test_commit_tag_set_blocks_read_only_reasoning():
    """schedule_draft_commit carries DB_WRITE +
    FILESYSTEM_WRITE → both block read-only reasoning per
    the phase-7 policy. Pin against the set."""
    commit_d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_draft_commit"
    )
    assert is_blocked_by_read_only_reasoning(commit_d.tags) is True


def test_dry_run_tag_set_blocks_read_only_reasoning():
    """schedule_dry_run carries FILESYSTEM_WRITE (it writes
    the handshake file) → blocks read-only reasoning. The
    handshake file IS a mutation even though no DB or
    network touches happen."""
    dr_d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_dry_run"
    )
    assert is_blocked_by_read_only_reasoning(dr_d.tags) is True


def test_freeze_tag_set_does_not_block_read_only_reasoning():
    """schedule_freeze is read-only — only FILESYSTEM_READ.
    Read-only reasoning agents can run it freely."""
    fr_d = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == "schedule_freeze"
    )
    assert is_blocked_by_read_only_reasoning(fr_d.tags) is False


# ===========================================================================
# Module-level import hygiene
# ===========================================================================


def _module_imports(module) -> set[str]:
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


def test_toolset_module_does_not_import_slack_sdk():
    leaked = [
        n for n in _module_imports(toolset_mod) if "slack_sdk" in n
    ]
    assert not leaked, f"slack_sdk leaked: {leaked!r}"


def test_toolset_module_does_not_import_googleapiclient():
    leaked = [
        n
        for n in _module_imports(toolset_mod)
        if "googleapiclient" in n
    ]
    assert not leaked


def test_authoring_package_does_not_import_slack_sdk_at_load():
    """Belt-and-braces: importing the authoring package + the
    toolset module does not pull slack_sdk into sys.modules."""
    # Force a fresh check: the module was already imported by
    # the test harness; just assert no slack_sdk module
    # surfaces in sys.modules. (If some OTHER test imported
    # slack_sdk, this would still leak — but the AST pin
    # above catches the module's own imports decisively.)
    assert "slack_sdk" not in sys.modules


# ===========================================================================
# Constructor signature pins (round-2 L365 / Q10)
# ===========================================================================


def test_expected_owner_id_is_required_keyword_only():
    sig = inspect.signature(AuthoringToolset.__init__)
    p = sig.parameters["expected_owner_id"]
    assert p.default is inspect.Parameter.empty
    assert p.kind == inspect.Parameter.KEYWORD_ONLY


def test_slack_client_optional_defaults_none():
    sig = inspect.signature(AuthoringToolset.__init__)
    assert sig.parameters["slack_client"].default is None


# ===========================================================================
# Toolset does NOT auto-mount on any agent
# ===========================================================================


def test_toolset_module_does_not_import_agent_or_run_bot():
    """The toolset module must NOT import app.agent or
    run_bot at module load — phase 7 keeps the bundle
    structural; phase 9 cutover does the binding."""
    imports = _module_imports(toolset_mod)
    forbidden = {"app.agent", "run_bot"}
    leaked = [i for i in imports if i in forbidden]
    assert not leaked, f"unexpected agent/run_bot import: {leaked!r}"
