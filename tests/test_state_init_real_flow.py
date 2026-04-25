"""Real-flow regression test for StateInitializerPlugin.

The previous unit test only called the plugin function in isolation. ADK 2.0
bypasses `before_agent_callback` for LlmAgents running as Workflow nodes —
so the function fired correctly in the unit test but never ran at runtime,
leaving state empty and causing `{bot_name}` template substitution to
KeyError on inbound A2A and Telegram messages alike.

This test exercises the real path: full App + InMemoryRunner + a fake
plugin that observes state at message-receive time. If the
StateInitializerPlugin doesn't seed `bot_name` / `user_id` /
`master_user_id` before the workflow processes the message, the
instruction template will KeyError downstream — so the assertion is on
the state visible at the same hook ADK uses to read it.
"""

import os
import pytest

from google.adk.plugins import BasePlugin
from google.adk.runners import InMemoryRunner
from google.genai import types


class _StateProbe(BasePlugin):
    """Captures the session state visible at message-receive time."""

    def __init__(self) -> None:
        super().__init__(name="state_probe")
        self.captured: dict | None = None

    async def on_user_message_callback(self, *, invocation_context, user_message):
        sess = invocation_context.session
        if sess and sess.state is not None:
            self.captured = dict(sess.state)
        return None


@pytest.mark.asyncio
async def test_state_seeded_before_workflow_runs(monkeypatch):
    """At the point ADK reads state for instruction substitution,
    `bot_name`, `user_id`, and `master_user_id` MUST already be set.
    """
    monkeypatch.setenv("BOT_NAME", "RegressionBot")
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_1,tg_2")

    # Reload the app so PLUGINS list is rebuilt with the env in place.
    import importlib
    import app.agent as _agent_mod
    importlib.reload(_agent_mod)

    probe = _StateProbe()
    _agent_mod.app.plugins.append(probe)

    runner = InMemoryRunner(app=_agent_mod.app)
    sess = await runner.session_service.create_session(
        app_name=_agent_mod.app.name, user_id="tg_42",
    )
    msg = types.Content(role="user", parts=[types.Part.from_text(text="hello")])

    try:
        async for _ in runner.run_async(
            user_id="tg_42", session_id=sess.id, new_message=msg,
        ):
            pass
    except Exception:
        # We don't care about LLM errors here — only state at probe time.
        pass

    assert probe.captured is not None, (
        "on_user_message_callback never fired — workflow integration broken"
    )
    state = probe.captured
    assert state.get("bot_name") == "RegressionBot", (
        f"bot_name not seeded by StateInitializerPlugin. State: {state}"
    )
    assert state.get("user_id"), (
        f"user_id not seeded. State: {state}"
    )
    assert state.get("master_user_id"), (
        f"master_user_id not seeded. State: {state}"
    )
