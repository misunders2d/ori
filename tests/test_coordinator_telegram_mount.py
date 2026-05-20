"""Slice 10 — CoordinatorAgent mounts TelegramSkillsToolset and drops
the direct `telegram_send_dm` import."""

from __future__ import annotations

import pytest


def test_coordinator_imports_cleanly():
    """The agent module must still import after the toolset swap."""
    from app.sub_agents import coordinator_agent  # noqa: F401

    assert coordinator_agent.root_agent.name == "CoordinatorAgent"


@pytest.mark.asyncio
async def test_coordinator_tools_include_telegram_skills_toolset_tools():
    """The mounted toolset surfaces all twelve telegram tools to the
    agent's resolved tool list. Confirms the swap (TelegramSkillsToolset()
    in place of the bare `telegram_send_dm` import) wired the full
    twelve tools, not just the original one."""
    from app.sub_agents import coordinator_agent
    from app.toolsets import TelegramSkillsToolset

    expected = {
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

    # Find the toolset instance.
    toolset_instances = [
        t
        for t in coordinator_agent.root_agent.tools
        if isinstance(t, TelegramSkillsToolset)
    ]
    assert len(toolset_instances) == 1, (
        "Coordinator must mount exactly one TelegramSkillsToolset"
    )
    tools = await toolset_instances[0].get_tools()
    assert {t.name for t in tools} == expected


def test_coordinator_no_longer_directly_imports_telegram_send_dm():
    """The `from app.tools.telegram import telegram_send_dm` line must
    be gone — the function is exposed via the toolset, not as a raw
    import in coordinator_agent.py."""
    import pathlib

    src = pathlib.Path(
        "app/sub_agents/coordinator_agent.py"
    ).read_text()
    assert "from app.tools.telegram import telegram_send_dm" not in src
