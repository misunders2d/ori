"""Slice 9 — TelegramSkillsToolset bundles all twelve telegram tools.

Confirms the toolset:
    - exports the twelve canonical tool names (matches app/tools/telegram).
    - is reachable via app.toolsets public API (re-exported in __all__).
    - lazy-imports — constructing the toolset does NOT import
      `app.tools.telegram` until `get_tools()` runs.
"""

from __future__ import annotations

import sys

import pytest

from app.toolsets import TelegramSkillsToolset


_EXPECTED_TOOL_NAMES = {
    "telegram_send_dm",
    "telegram_send_to_chat",
    "telegram_save_alias",
    "telegram_save_last_forward_alias",
    "telegram_list_aliases",
    "telegram_delete_alias",
    "telegram_resolve_alias",
    "telegram_forward",
    "telegram_list_cached_files",
    "telegram_grant_capability",
    "telegram_revoke_capability",
    "telegram_list_capabilities",
}


@pytest.mark.asyncio
async def test_toolset_returns_all_twelve_tools():
    toolset = TelegramSkillsToolset()
    tools = await toolset.get_tools()
    names = {t.name for t in tools}
    assert names == _EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_toolset_tool_count():
    toolset = TelegramSkillsToolset()
    tools = await toolset.get_tools()
    assert len(tools) == 12


def test_toolset_reexported_in_toolsets_init():
    """The new toolset must be reachable via the package import surface
    so consumer agents can `from app.toolsets import TelegramSkillsToolset`."""
    import app.toolsets as toolsets_pkg

    assert "TelegramSkillsToolset" in toolsets_pkg.__all__
    assert hasattr(toolsets_pkg, "TelegramSkillsToolset")
    # Same identity check.
    assert toolsets_pkg.TelegramSkillsToolset is TelegramSkillsToolset


def test_constructor_does_not_eager_import_telegram_tools(monkeypatch):
    """get_tools is lazy: the constructor must not import
    `app.tools.telegram` (avoids dragging in httpx setup at module
    import time)."""
    # Drop any cached import.
    sys.modules.pop("app.tools.telegram", None)
    TelegramSkillsToolset()
    assert "app.tools.telegram" not in sys.modules


@pytest.mark.asyncio
async def test_get_tools_imports_telegram_tools_lazily():
    """First call to get_tools should populate sys.modules."""
    sys.modules.pop("app.tools.telegram", None)
    toolset = TelegramSkillsToolset()
    assert "app.tools.telegram" not in sys.modules
    await toolset.get_tools()
    assert "app.tools.telegram" in sys.modules
