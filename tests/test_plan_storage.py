"""Durable SQLite plan storage — round-trip + crash-resume."""
import os
from unittest.mock import patch

import pytest

from app.runtime import plan_storage


@pytest.fixture
def tmp_plan_db(tmp_path, monkeypatch):
    """Point plan_storage at a fresh DB per test (sqlite path is module-level)."""
    db = tmp_path / "plans.db"
    monkeypatch.setattr(plan_storage, "DB_PATH", str(db))
    return db


@pytest.mark.asyncio
async def test_seed_then_get_next(tmp_plan_db):
    await plan_storage.seed_plan("s1", "build x", ["step a", "step b"])
    s = await plan_storage.get_next_step("s1")
    assert s == {"step_index": 0, "description": "step a"}


@pytest.mark.asyncio
async def test_complete_then_next(tmp_plan_db):
    await plan_storage.seed_plan("s1", "build x", ["a", "b", "c"])
    await plan_storage.get_next_step("s1")
    res = await plan_storage.complete_step("s1", "result-of-a")
    assert res["status"] == "success"
    assert res["next_step"] == {"step_index": 1, "description": "b"}


@pytest.mark.asyncio
async def test_full_completion_marks_plan_completed(tmp_plan_db):
    await plan_storage.seed_plan("s1", "x", ["a", "b"])
    await plan_storage.get_next_step("s1")
    await plan_storage.complete_step("s1", "ra")
    await plan_storage.get_next_step("s1")
    res = await plan_storage.complete_step("s1", "rb")
    assert res["status"] == "success"
    assert "All steps completed" in res["message"]
    status = await plan_storage.get_plan_status("s1")
    assert status["status"] == "completed"


@pytest.mark.asyncio
async def test_has_pending_steps(tmp_plan_db):
    assert await plan_storage.has_pending_steps("s1") is False
    await plan_storage.seed_plan("s1", "x", ["a"])
    assert await plan_storage.has_pending_steps("s1") is True
    await plan_storage.get_next_step("s1")
    assert await plan_storage.has_pending_steps("s1") is True  # in_progress still pending
    await plan_storage.complete_step("s1", "done")
    assert await plan_storage.has_pending_steps("s1") is False


@pytest.mark.asyncio
async def test_crash_resume_via_in_progress(tmp_plan_db):
    """Mid-step crash: get_next_step on a fresh process returns the in_progress step."""
    await plan_storage.seed_plan("s1", "x", ["a", "b"])
    first = await plan_storage.get_next_step("s1")
    assert first["step_index"] == 0
    # Simulate restart: new process, same DB. plan_storage is stateless across
    # connections (no module cache), so just call again.
    again = await plan_storage.get_next_step("s1")
    assert again == first  # Same in_progress step, no double-claim


@pytest.mark.asyncio
async def test_complete_with_no_in_progress_returns_error(tmp_plan_db):
    await plan_storage.seed_plan("s1", "x", ["a"])
    # No get_next_step yet
    res = await plan_storage.complete_step("s1", "x")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_active_plan_context_text(tmp_plan_db):
    assert await plan_storage.get_active_plan_context("s1") is None
    await plan_storage.seed_plan("s1", "build the thing", ["step alpha", "step beta"])
    ctx = await plan_storage.get_active_plan_context("s1")
    assert "ACTIVE PLAN" in ctx
    assert "step alpha" not in ctx  # before claiming, no current step
    await plan_storage.get_next_step("s1")
    ctx2 = await plan_storage.get_active_plan_context("s1")
    assert "CURRENT STEP" in ctx2
    assert "step alpha" in ctx2


@pytest.mark.asyncio
async def test_abandon(tmp_plan_db):
    await plan_storage.seed_plan("s1", "x", ["a"])
    assert await plan_storage.abandon_plan("s1") is True
    assert await plan_storage.has_pending_steps("s1") is False
    # Abandoning twice is a no-op (returns False the second time)
    assert await plan_storage.abandon_plan("s1") is False


@pytest.mark.asyncio
async def test_create_plan_blocks_on_active(tmp_plan_db):
    res = await plan_storage.create_plan("s1", "first", ["a"])
    assert res["status"] == "success"
    res2 = await plan_storage.create_plan("s1", "second", ["b"])
    assert res2["status"] == "error"
    # After abandon, can create again
    await plan_storage.abandon_plan("s1")
    res3 = await plan_storage.create_plan("s1", "third", ["c"])
    assert res3["status"] == "success"


@pytest.mark.asyncio
async def test_session_isolation(tmp_plan_db):
    await plan_storage.seed_plan("alice", "x", ["a"])
    await plan_storage.seed_plan("bob", "y", ["b"])
    a = await plan_storage.get_next_step("alice")
    b = await plan_storage.get_next_step("bob")
    assert a["description"] == "a"
    assert b["description"] == "b"
