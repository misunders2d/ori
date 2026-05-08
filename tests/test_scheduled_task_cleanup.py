from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.agent_executor import AgentResponse
from app.tasks import run_scheduled_task
from app.tools import scratchpad


@pytest.mark.asyncio
async def test_scheduled_task_wipes_stale_session_state_before_run(tmp_path, monkeypatch):
    monkeypatch.setattr(scratchpad, "_SCRATCHPAD_DIR", str(tmp_path / "scratchpads"))

    task_id = "sched_cleanup_test"
    monkeypatch.chdir(tmp_path)
    plan_dir = tmp_path / "tmp" / "plans"
    (plan_dir / f"{task_id}.json").parent.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{task_id}.json").write_text('{"status": "active"}')
    stale_pad = tmp_path / "scratchpads" / task_id / "old.md"
    stale_pad.parent.mkdir(parents=True, exist_ok=True)
    stale_pad.write_text("stale")

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service.delete_session = AsyncMock()
    runner.session_service.create_session = AsyncMock()

    async def fake_extract(*args, **kwargs):
        return AgentResponse(text="done")

    async def fake_deliver(*args, **kwargs):
        return None

    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", side_effect=fake_extract),
        patch("app.tasks._deliver_with_fallback", side_effect=fake_deliver),
    ):
        await run_scheduled_task(
            task_prompt="do the thing",
            notify={"type": "slack", "channel": "C123"},
            owner_user_id="user@example.com",
            task_id=task_id,
        )

    first_delete = runner.session_service.delete_session.await_args_list[0].kwargs
    assert first_delete == {
        "app_name": "ori",
        "user_id": "system_scheduler",
        "session_id": task_id,
    }
    assert runner.session_service.create_session.await_count == 1
    assert not stale_pad.exists()
    assert not os.path.exists(plan_dir / f"{task_id}.json")
