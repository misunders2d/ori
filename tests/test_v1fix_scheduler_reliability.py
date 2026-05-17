"""v1fix Slice-1 — scheduled-task reliability CORE acceptance + contract tests.

Slice-1 re-homes the scheduled fire path off the per-fire EPHEMERAL session
(deleted+recreated+wiped every run) onto a DURABLE side-session LINKED to the
creating chat, run through the SAME boot Runner (app=ori_app) so the scheduled
turn resolves under the SAME ADK app_name the live chat uses, with a logical-
occurrence cursor in the durable session state that makes catch-up after
downtime never-skip + never-double-send.

These tests are deterministic (no live LLM, no network — agent execution and
delivery are mocked) and are runnable head-less:

    uv run python -m pytest -q tests/test_v1fix_scheduler_reliability.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.agent_executor import AgentResponse
from app.tasks import (
    _chat_identity_from_notify,
    _compute_occurrences,
    _linked_side_session_id,
    _occ_key,
    _stable_job_key,
    run_scheduled_task,
)


# ---------------------------------------------------------------------------
# Fakes — a minimal (app_name, user_id, session_id)-keyed durable store that
# behaves like ADK's DatabaseSessionService for the parts the fire path uses.
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self, app_name, user_id, session_id, state=None):
        self.app_name = app_name
        self.user_id = user_id
        self.id = session_id
        self.session_id = session_id
        self.state = dict(state or {})
        self.events = []


class _FakeSessionService:
    """Tracks every session op so tests can assert on identity + lifecycle."""

    def __init__(self):
        self.store: dict[tuple, _FakeSession] = {}
        self.deletes: list[tuple] = []
        self.creates: list[tuple] = []
        self.appends: list[tuple] = []

    async def get_session(self, app_name, user_id, session_id):
        return self.store.get((app_name, user_id, session_id))

    async def create_session(self, app_name, user_id, session_id, state=None):
        key = (app_name, user_id, session_id)
        sess = _FakeSession(app_name, user_id, session_id, state)
        self.store[key] = sess
        self.creates.append(key)
        return sess

    async def delete_session(self, app_name, user_id, session_id):
        self.deletes.append((app_name, user_id, session_id))
        self.store.pop((app_name, user_id, session_id), None)

    async def append_event(self, session, event):
        self.appends.append((session.app_name, session.user_id, session.id, event))
        # Mirror state_delta so the cursor round-trips like the real service.
        actions = getattr(event, "actions", None)
        delta = getattr(actions, "state_delta", None) if actions else None
        if delta:
            session.state.update(delta)


def _make_runner(session_service, app_name="ori"):
    runner = MagicMock()
    runner.app_name = app_name
    runner.session_service = session_service
    return runner


def _notify(origin="tg_555"):
    # _stamp_ownership shape: origin_session_id is the creating chat session.
    return {
        "type": "telegram",
        "chat_id": 555,
        "origin_session_id": origin,
        "target_session_id": origin,
    }


# ---------------------------------------------------------------------------
# Pure-helper unit checks (no I/O).
# ---------------------------------------------------------------------------
def test_stable_job_key_prefers_job_id_else_deterministic_fallback():
    assert _stable_job_key("cron_abc", "u", {}, "p") == "cron_abc"
    a = _stable_job_key(None, "u@x", _notify("tg_1"), "do thing")
    b = _stable_job_key(None, "u@x", _notify("tg_1"), "do thing")
    c = _stable_job_key(None, "u@x", _notify("tg_2"), "do thing")
    assert a == b and a.startswith("legacy_") and a != c  # stable + discriminating


def test_linked_side_session_id_is_chat_linked_but_isolated():
    sid = _linked_side_session_id("tg_555", "cron_abc")
    assert sid == "tg_555::job::cron_abc"
    assert sid != "tg_555"  # NEVER the live chat session id (no collision)


def test_chat_identity_recovered_from_origin_only():
    assert _chat_identity_from_notify(_notify("tg_9")) == "tg_9"
    assert _chat_identity_from_notify({"type": "telegram"}) is None


# ---------------------------------------------------------------------------
# DURABLE linked side-session — the ephemeral create/delete/wipe is gone.
# (Pre-v1fix behavior: delete_session(system_scheduler, task_id) then
#  create then delete-in-finally every fire. That is INTENTIONALLY removed.)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_durable_side_session_never_deleted_identity_is_chat_linked():
    svc = _FakeSessionService()
    runner = _make_runner(svc, app_name="ori")

    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="done")),
        ),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock()),
    ):
        await run_scheduled_task(
            task_prompt="check ASIN",
            notify=_notify("tg_555"),
            owner_user_id="tg_555",
            task_id="sched_x",
            job_id="oneoff_jjjj",
        )

    expected_sid = "tg_555::job::oneoff_jjjj"
    # D5: every session op resolves under the BOOT runner's app_name, the
    # chat's user_id, and the linked side-session id.
    assert svc.creates == [("ori", "tg_555", expected_sid)]
    # Durable: the ephemeral delete/recreate/finally-delete is GONE.
    assert svc.deletes == []


@pytest.mark.asyncio
async def test_d5_app_name_is_load_bearing_and_fails_loud_on_divergence():
    """D5 acceptance: the scheduled turn MUST resolve under the SAME
    (app_name, user_id, session_id)-family the live chat uses. A divergent
    app_name silently writes an orphan row and the in-chat follow-up breaks
    with no error — this test asserts equality on match AND inequality on
    divergence, so the bug fails LOUD, not silently."""
    chat_app, chat_sid = "ori", "tg_555"

    # Match: boot runner app_name == the chat's app_name.
    svc = _FakeSessionService()
    runner = _make_runner(svc, app_name=chat_app)
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="ok")),
        ),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock()),
    ):
        await run_scheduled_task(
            task_prompt="p", notify=_notify(chat_sid),
            owner_user_id=chat_sid, task_id="t1", job_id="cron_aaaa",
        )
    (run_app, run_user, _run_sid) = svc.creates[0]
    assert (run_app, run_user) == (chat_app, chat_sid)  # resolves to chat identity

    # Divergence: a per-fire runner with a different app_name MUST be detected.
    bad_svc = _FakeSessionService()
    bad_runner = _make_runner(bad_svc, app_name="orphan_app")
    with (
        patch("run_bot.get_runner", return_value=bad_runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="ok")),
        ),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock()),
    ):
        await run_scheduled_task(
            task_prompt="p", notify=_notify(chat_sid),
            owner_user_id=chat_sid, task_id="t2", job_id="cron_bbbb",
        )
    (bad_app, _bad_user, _bad_sid) = bad_svc.creates[0]
    assert bad_app != chat_app  # FAIL-LOUD: orphan app_name is observable


# ---------------------------------------------------------------------------
# Never-double-send (QD) — re-firing an already-delivered occurrence is an
# idempotent skip: no second agent run, no second delivery.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_never_double_send_idempotent_skip():
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    job_key = "oneoff_dupe"
    sid = f"tg_555::job::{job_key}"
    # Seed the durable session as if this occurrence was already delivered.
    now_key = _occ_key(datetime.now(timezone.utc))
    svc.store[("ori", "tg_555", sid)] = _FakeSession(
        "ori", "tg_555", sid,
        state={f"job:{job_key}:delivered_occurrences": [now_key],
               f"job:{job_key}:cursor": now_key},
    )

    extract = AsyncMock(return_value=AgentResponse(text="should not run"))
    deliver = AsyncMock()
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", new=extract),
        patch("app.tasks._deliver_with_fallback", new=deliver),
        patch("app.tasks._compute_occurrences", return_value=[datetime.now(timezone.utc)]),
    ):
        await run_scheduled_task(
            task_prompt="p", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="sched_d", job_id=job_key,
        )

    extract.assert_not_awaited()   # agent did NOT re-run
    deliver.assert_not_awaited()   # message NOT re-sent
    assert svc.deletes == []       # still durable


# ---------------------------------------------------------------------------
# Outage catch-up (QD) — N missed occurrences => ONE consolidated message,
# cursor advances, every missed occurrence recorded (nothing collapsed away).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_outage_catchup_one_consolidated_message_and_cursor_advance():
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    job_key = "cron_catchup"
    sid = f"tg_555::job::{job_key}"
    # Cursor 5 minutes in the past; an every-minute cron => several missed.
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    svc.store[("ori", "tg_555", sid)] = _FakeSession(
        "ori", "tg_555", sid,
        state={f"job:{job_key}:cursor": past.isoformat(),
               f"job:{job_key}:delivered_occurrences": []},
    )

    from apscheduler.triggers.cron import CronTrigger
    fake_job = MagicMock()
    fake_job.trigger = CronTrigger.from_crontab("* * * * *", timezone=timezone.utc)
    fake_sched = MagicMock()
    fake_sched.get_job.return_value = fake_job

    seen_query = {}

    async def _capture_extract(runner, user_id, session_id, message, actual_caller_id=None):
        seen_query["q"] = message
        return AgentResponse(text="consolidated result")

    deliver = AsyncMock()
    upd = AsyncMock()
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", new=_capture_extract),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=deliver),
        patch("app.scheduler_instance.scheduler", fake_sched),
    ):
        await run_scheduled_task(
            task_prompt="daily digest", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="sched_c", job_id=job_key,
        )

    assert "[CATCH-UP]" in seen_query["q"]            # consolidated, not N runs
    assert deliver.await_count == 1                   # exactly ONE message
    # Cursor advanced + every missed occurrence recorded in delivered set.
    upd.assert_awaited_once()
    delta = upd.await_args.args[3]
    cur = delta[f"job:{job_key}:cursor"]
    delivered = delta[f"job:{job_key}:delivered_occurrences"]
    assert len(delivered) >= 2 and cur == max(delivered)


# ---------------------------------------------------------------------------
# In-chat visibility (QA) — the clean result lands in the CHAT session (so the
# user's next chat turn sees it) while the RUN stays in the linked
# side-session (no collision with the user typing).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_in_chat_followup_visibility_and_run_isolation():
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    chat_sid = "tg_555"
    # The live chat already has its own ADK session.
    svc.store[("ori", chat_sid, chat_sid)] = _FakeSession("ori", chat_sid, chat_sid)

    class _Adapter:
        platform_name = "telegram"
        def __init__(self):
            self.sent = []
        def parse_notify_info(self, session_id):
            return {}
        async def send_message(self, target, text):
            self.sent.append((target, text))
        async def send_media(self, *a, **k):
            pass

    adapter = _Adapter()
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="DAY-12 LESSON")),
        ),
        patch("app.core.transport.get_adapter", return_value=adapter),
    ):
        await run_scheduled_task(
            task_prompt="deliver day 12", notify=_notify(chat_sid),
            owner_user_id=chat_sid, task_id="sched_v", job_id="cron_vis",
        )

    # Delivered as a clean message to the chat channel.
    assert adapter.sent and adapter.sent[0][1] == "DAY-12 LESSON"
    # Visible for the next chat turn: a model event appended into the CHAT
    # session under the live chat identity (NOT the side-session).
    chat_appends = [a for a in svc.appends if a[2] == chat_sid]
    assert chat_appends, "scheduled result must be visible in the chat session"
    # The agent RUN itself used the isolated linked side-session.
    assert ("ori", chat_sid, f"{chat_sid}::job::cron_vis") in svc.store


# ---------------------------------------------------------------------------
# Per-add_job reliability kwargs on the USER sites; the process-GLOBAL default
# in scheduler_instance.py is left UNTOUCHED (hard constraint — it also
# governs contract/system jobs).
# ---------------------------------------------------------------------------
def _ctx(user="tg_555", sid="tg_555"):
    ctx = MagicMock()
    ctx.session = MagicMock()
    ctx.session.state = {"user_id": user}
    ctx.session.session_id = sid
    ctx.session.id = sid
    return ctx


def test_user_add_job_carries_per_job_reliability_kwargs_and_job_id():
    from app.tools import scheduling

    captured = {}

    def _fake_add_job(func, *a, **kw):
        captured["a"] = a
        captured["kw"] = kw
        job = MagicMock()
        job.next_run_time = "soon"
        return job

    fake_sched = MagicMock()
    fake_sched.add_job.side_effect = _fake_add_job
    with (
        patch("app.scheduler_instance.scheduler", fake_sched),
        patch("app.tools.scheduling._resolve_notify", return_value=_notify("tg_555")),
        patch("app.tools.scheduling._get_user_id", return_value="tg_555"),
        patch("app.tools.scheduling._get_session_id", return_value="tg_555"),
    ):
        res = scheduling.schedule_recurring_task(
            "do x", "0 9 * * MON", "UTC", _ctx(),
        )
    assert res["status"] == "success"
    kw = captured["kw"]
    assert kw["misfire_grace_time"] is None   # a late wake STILL fires
    assert kw["coalesce"] is True             # cursor fans out the collapse
    assert kw["max_instances"] == 1           # cursor RMW single-writer guard
    assert kw["kwargs"]["job_id"] == kw["id"] # stable id threaded through


# ---------------------------------------------------------------------------
# Delivery-failure ordering (reviewer 🟡) — the cursor is advanced ONLY after
# the channel accepted the message. A recoverable send failure must NOT mark
# the occurrence delivered, so a RECURRING job re-attempts it next wake.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delivery_failure_recurring_reattempts_next_wake():
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    job_key = "cron_retry"
    fixed = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)

    extract = AsyncMock(return_value=AgentResponse(text="the reminder"))
    upd = AsyncMock()
    deliver = AsyncMock()

    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", new=extract),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=deliver),
        patch("app.tasks._compute_occurrences", return_value=[fixed]),
    ):
        # FIRE 1 — transient channel failure.
        deliver.return_value = False
        await run_scheduled_task(
            task_prompt="daily", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="f1", job_id=job_key,
        )
        assert extract.await_count == 1
        upd.assert_not_awaited()  # cursor NOT advanced — occurrence NOT lost

        # FIRE 2 — same occurrence, channel now healthy: it RE-ATTEMPTS
        # (not idempotent-skipped, because it was never marked delivered).
        deliver.return_value = True
        await run_scheduled_task(
            task_prompt="daily", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="f2", job_id=job_key,
        )
        assert extract.await_count == 2          # re-attempted
        upd.assert_awaited_once()                # now marked delivered
        delta = upd.await_args.args[3]
        assert _occ_key(fixed) in delta[f"job:{job_key}:delivered_occurrences"]


@pytest.mark.asyncio
async def test_delivery_failure_oneoff_logged_not_advanced(caplog):
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    upd = AsyncMock()

    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(text="one-off reminder")),
        ),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock(return_value=False)),
        patch("app.tasks._compute_occurrences",
               return_value=[datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)]),
        caplog.at_level("ERROR", logger="app.tasks"),
    ):
        await run_scheduled_task(
            task_prompt="ping me once", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="o1", job_id="oneoff_x",
        )

    upd.assert_not_awaited()  # cursor NOT advanced
    assert any("cursor NOT advanced" in r.getMessage() for r in caplog.records)  # not silent (Rule 13)


# ---------------------------------------------------------------------------
# Agent-failure ordering (reviewer 🟡 round 2) — a delivered failure NOTICE is
# NOT a delivered occurrence. A transient agent failure (raise / terminal
# AgentResponse error) on a RECURRING job, even with a healthy channel, must
# NOT advance the cursor — it re-attempts next wake (restores the
# at-least-once-on-agent-failure property of the first slice).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_agent_exception_channel_healthy_recurring_reattempts(caplog):
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    job_key = "cron_agentfail"
    fixed = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    upd = AsyncMock()
    deliver = AsyncMock(return_value=True)  # channel HEALTHY

    raising = AsyncMock(side_effect=RuntimeError("LLM 503"))
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", new=raising),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=deliver),
        patch("app.tasks._compute_occurrences", return_value=[fixed]),
        caplog.at_level("ERROR", logger="app.tasks"),
    ):
        await run_scheduled_task(
            task_prompt="daily digest", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="af1", job_id=job_key,
        )
    assert raising.await_count == 1          # agent attempted
    assert deliver.await_count == 1          # failure NOTICE delivered
    upd.assert_not_awaited()                 # occurrence NOT consumed
    assert any("agent FAILED" in r.getMessage() for r in caplog.records)

    # Next wake (channel + agent healthy): same occurrence RE-ATTEMPTS.
    ok = AsyncMock(return_value=AgentResponse(text="the digest"))
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response", new=ok),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=deliver),
        patch("app.tasks._compute_occurrences", return_value=[fixed]),
    ):
        await run_scheduled_task(
            task_prompt="daily digest", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="af2", job_id=job_key,
        )
    ok.assert_awaited_once()                  # re-attempted (not skipped)
    upd.assert_awaited_once()                 # now consumed
    assert _occ_key(fixed) in upd.await_args.args[3][f"job:{job_key}:delivered_occurrences"]


@pytest.mark.asyncio
async def test_terminal_agent_response_error_does_not_consume_occurrence():
    """An AgentResponse with .error set (rate/context limit) is a failure
    notice, not a result — even delivered, the occurrence is not consumed."""
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    job_key = "cron_rl"
    upd = AsyncMock()
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch(
            "app.core.agent_executor.extract_agent_response",
            new=AsyncMock(return_value=AgentResponse(
                text="⚠️ Rate Limit Exceeded", error="rate_limit_exhausted")),
        ),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock(return_value=True)),
        patch("app.tasks._compute_occurrences",
               return_value=[datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)]),
    ):
        await run_scheduled_task(
            task_prompt="p", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="rl1", job_id=job_key,
        )
    upd.assert_not_awaited()  # terminal error => occurrence NOT consumed


@pytest.mark.asyncio
async def test_agent_exception_oneoff_logged_not_advanced(caplog):
    svc = _FakeSessionService()
    runner = _make_runner(svc)
    upd = AsyncMock()
    with (
        patch("run_bot.get_runner", return_value=runner),
        patch("app.core.agent_executor.extract_agent_response",
              new=AsyncMock(side_effect=RuntimeError("tool timeout"))),
        patch("app.core.agent_executor.update_session_state", new=upd),
        patch("app.tasks._deliver_with_fallback", new=AsyncMock(return_value=True)),
        patch("app.tasks._compute_occurrences",
               return_value=[datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)]),
        caplog.at_level("ERROR", logger="app.tasks"),
    ):
        await run_scheduled_task(
            task_prompt="ping once", notify=_notify("tg_555"),
            owner_user_id="tg_555", task_id="ao1", job_id="oneoff_x",
        )
    upd.assert_not_awaited()  # not advanced (one-off: no retry vehicle, logged)
    assert any("agent FAILED" in r.getMessage() for r in caplog.records)


def test_global_scheduler_default_is_unchanged():
    """Hard constraint: scheduler_instance.py job_defaults must NOT be flipped
    (it governs every job in the process, incl. contracts/system)."""
    import app.scheduler_instance as si

    assert si.job_defaults == {
        "misfire_grace_time": 3600,
        "coalesce": True,
        "max_instances": 1,
    }
