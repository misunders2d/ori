"""TelegramSkillsToolset — agent-facing bundle for the slice-7 tools.

Mounts the twelve Telegram skill tools on the agent that owns Telegram
transport (the CoordinatorAgent, wired in slice 10). Replacing the
direct `telegram_send_dm` import keeps `coordinator_agent.py` clean
and follows AI_EDITS rule 2 ("Extend existing toolsets — don't create
parallel raw tools"; here we ship a new toolset because there is no
pre-existing Telegram-domain bundle).
"""

from __future__ import annotations

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class TelegramSkillsToolset(BaseToolset):
    """Telegram skill tools — send-to-chat, alias CRUD, file forwarding,
    capability administration. Lazy-imports the underlying functions so
    that test-time import of this module does not drag in adapter /
    httpx setup."""

    async def get_tools(self, readonly_context=None):
        from app.tools.telegram import (
            telegram_delete_alias,
            telegram_forward,
            telegram_grant_capability,
            telegram_list_aliases,
            telegram_list_capabilities,
            telegram_list_cached_files,
            telegram_resolve_alias,
            telegram_revoke_capability,
            telegram_save_alias,
            telegram_save_last_forward_alias,
            telegram_send_dm,
            telegram_send_to_chat,
        )

        return [
            FunctionTool(func=telegram_send_dm),
            FunctionTool(func=telegram_send_to_chat),
            FunctionTool(func=telegram_save_alias),
            FunctionTool(func=telegram_save_last_forward_alias),
            FunctionTool(func=telegram_list_aliases),
            FunctionTool(func=telegram_delete_alias),
            FunctionTool(func=telegram_resolve_alias),
            FunctionTool(func=telegram_forward),
            FunctionTool(func=telegram_list_cached_files),
            FunctionTool(func=telegram_grant_capability),
            FunctionTool(func=telegram_revoke_capability),
            FunctionTool(func=telegram_list_capabilities),
        ]
