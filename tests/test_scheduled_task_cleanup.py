from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.agent_executor import AgentResponse
from app.tasks import run_scheduled_task
from app.tools import scratchpad


# ---------------------------------------------------------------------------
# v1fix Slice-1 behavior change (documented, intentional):
#
# PRE-v1fix this test pinned the EPHEMERAL contract — the fire path called
# delete_session(app="ori", user="system_scheduler", session_id=task_id),
# then create_session, then delete again in `finally`, wiping the sidecar
# every run. That made cross-run progress / crash-resume / never-double-send
# structurally impossible and is exactly the unreliability Slice-1 fixes.
#
# POST-v1fix the run is DURABLE: a side-session LINKED to the creating chat
# ("<chat>::job::<job_id>"), get-or-create, NEVER deleted (the cursor in
# session.state must survive across fires). The per-fire plan/scratchpad
# SIDECAR is still reset each fire (steps tasks start from their frozen
# seeded plan) — that sub-behavior is preserved and is what this test now
# pins, alongside the no-delete durability guarantee.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scheduled_task_durable_session_but_resets_per_fire_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(scratchpad, "_SCRATCHPAD_DIR", str(tmp_path / "scratchpads"))
    monkeypatch.chdir(tmp_path)

    origin = "tg_555"
    job_id = "oneoff_cleanup"
    side_session_id = f"{origin}::job::{job_id}"

    # Stale per-fire sidecar keyed by the durable side-session id.
    plan_dir = tmp_path / "tmp" / "plans"
    (plan_dir / f"{side_session_id}.json").parent.mkdir(parents=True, exist_ok=True)
    (plan_dir / f"{side_session_id}.json").write_text('{"status": "active"}')
    stale_pad = tmp_path / "scratchpads" / side_session_id / "old.md"
    stale_pad.parent.mkdir(parents=True, exist_ok=True)
    stale_pad.write_text("stale")

    durable = SimpleNamespace(
        state={}, events=[], app_name="ori", user_id=origin, id=side_session_id
    )
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service.get_session = AsyncMock(return_value=None)
    runner.session_service.create_session = AsyncMock(return_value=durable)
    runner.session_service.delete_session = AsyncMock()

    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="done")),
        ),
        patch("app.core.agent_executor.update_session_state", new=AsyncMock()),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock()),
    ):
        await run_scheduled_task(
            task_prompt="do the thing",
            notify={
                "type": "telegram",
                "chat_id": 555,
                "origin_session_id": origin,
                "target_session_id": origin,
            },
            owner_user_id=origin,
            task_id="sched_cleanup_test",
            job_id=job_id,
        )

    # DURABLE: the ephemeral delete/recreate/finally-delete is GONE.
    runner.session_service.delete_session.assert_not_awaited()
    # Resolved under (boot app_name, chat user_id, linked side-session id).
    create_kwargs = runner.session_service.create_session.await_args.kwargs
    assert create_kwargs == {
        "app_name": "ori",
        "user_id": origin,
        "session_id": side_session_id,
    }
    # Per-fire sidecar STILL reset (frozen-plan-per-fire behavior preserved).
    assert not stale_pad.exists()
    assert not os.path.exists(plan_dir / f"{side_session_id}.json")
