"""Durable plan storage for Ori's plan-and-execute workflow.

Replaces the legacy ephemeral JSON-on-disk plan store with a SQLite backend
that survives bot crashes and process restarts. The agent-facing tools in
`app/tools/planner.py` are thin wrappers over this module.

Schema migration runs lazily on first connect — no separate migration step
required. Each session has at most one active plan; plans are completed
or abandoned, never deleted (history is kept for the channel logger and
post-hoc audits). On a fresh process boot, an interrupted plan with a
step in `in_progress` status can be resumed by the next `get_next_step`
call (the step stays in_progress; complete_step finishes it).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import aiosqlite

DB_PATH = os.path.abspath("./data/plans.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    session_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_steps (
    session_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (session_id, step_index),
    FOREIGN KEY (session_id) REFERENCES plans(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_plan_steps_status ON plan_steps(session_id, status);
"""


async def _connect() -> aiosqlite.Connection:
    """Open a connection with foreign keys + WAL mode + schema ensured."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.executescript(_SCHEMA)
    await conn.commit()
    conn.row_factory = aiosqlite.Row
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public API — mirrors the legacy `app/tools/planner.py` semantics
# ---------------------------------------------------------------------------

async def seed_plan(session_id: str, task: str, steps: list[str]) -> None:
    """Seed a fresh plan for `session_id`. Used by the scheduler before the
    agent's first turn. Replaces any existing plan on the same session id
    (last-writer-wins; scheduled tasks own their session).
    """
    if not steps:
        raise ValueError("seed_plan requires at least one step")
    conn = await _connect()
    try:
        await conn.execute("DELETE FROM plans WHERE session_id = ?", (session_id,))
        await conn.execute(
            "INSERT INTO plans(session_id, task, status, current_step, created_at, updated_at) "
            "VALUES (?, ?, 'active', 0, ?, ?)",
            (session_id, task, _now(), _now()),
        )
        await conn.executemany(
            "INSERT INTO plan_steps(session_id, step_index, description, status) "
            "VALUES (?, ?, ?, 'pending')",
            [(session_id, idx, desc) for idx, desc in enumerate(steps)],
        )
        await conn.commit()
    finally:
        await conn.close()


async def create_plan(session_id: str, task: str, steps: list[str]) -> dict[str, Any]:
    """Agent-facing variant: fails if an active plan already exists."""
    conn = await _connect()
    try:
        async with conn.execute(
            "SELECT status FROM plans WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["status"] == "active":
            return {"status": "error", "message": "A plan is already active for this session."}
        # Replace any completed/abandoned plan on this session id.
        await conn.execute("DELETE FROM plans WHERE session_id = ?", (session_id,))
        await conn.execute(
            "INSERT INTO plans(session_id, task, status, current_step, created_at, updated_at) "
            "VALUES (?, ?, 'active', 0, ?, ?)",
            (session_id, task, _now(), _now()),
        )
        await conn.executemany(
            "INSERT INTO plan_steps(session_id, step_index, description, status) "
            "VALUES (?, ?, ?, 'pending')",
            [(session_id, idx, desc) for idx, desc in enumerate(steps)],
        )
        await conn.commit()
        return {
            "status": "success",
            "task": task,
            "step_count": len(steps),
        }
    finally:
        await conn.close()


async def has_pending_steps(session_id: str) -> bool:
    """True iff there's at least one step in pending or in_progress for an active plan."""
    conn = await _connect()
    try:
        async with conn.execute(
            """
            SELECT 1 FROM plan_steps s
            JOIN plans p ON p.session_id = s.session_id
            WHERE s.session_id = ?
              AND p.status = 'active'
              AND s.status IN ('pending', 'in_progress')
            LIMIT 1
            """,
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return row is not None
    finally:
        await conn.close()


async def get_next_step(session_id: str) -> dict[str, Any] | None:
    """Atomically claim the next pending step (or return the in_progress one
    if there is one already — that's how a crashed-mid-step plan resumes).

    Returns None if no plan or all steps complete.
    """
    conn = await _connect()
    try:
        # Already-in-progress step takes priority (resume case).
        async with conn.execute(
            "SELECT step_index, description FROM plan_steps "
            "WHERE session_id = ? AND status = 'in_progress' "
            "ORDER BY step_index ASC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return {"step_index": row["step_index"], "description": row["description"]}
        async with conn.execute(
            "SELECT step_index, description FROM plan_steps "
            "WHERE session_id = ? AND status = 'pending' "
            "ORDER BY step_index ASC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        await conn.execute(
            "UPDATE plan_steps SET status = 'in_progress' "
            "WHERE session_id = ? AND step_index = ?",
            (session_id, row["step_index"]),
        )
        await conn.execute(
            "UPDATE plans SET current_step = ?, updated_at = ? WHERE session_id = ?",
            (row["step_index"], _now(), session_id),
        )
        await conn.commit()
        return {"step_index": row["step_index"], "description": row["description"]}
    finally:
        await conn.close()


async def complete_step(session_id: str, result: str) -> dict[str, Any]:
    """Mark the current in_progress step as done with `result`.

    Returns a dict describing what's next (next pending step, or
    'all complete' if the plan finished — which also flips plan status).
    """
    conn = await _connect()
    try:
        async with conn.execute(
            "SELECT step_index FROM plan_steps "
            "WHERE session_id = ? AND status = 'in_progress' "
            "ORDER BY step_index ASC LIMIT 1",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"status": "error", "message": "No step in progress."}
        await conn.execute(
            "UPDATE plan_steps SET status = 'done', result = ? "
            "WHERE session_id = ? AND step_index = ?",
            (result, session_id, row["step_index"]),
        )
        async with conn.execute(
            "SELECT step_index, description FROM plan_steps "
            "WHERE session_id = ? AND status = 'pending' "
            "ORDER BY step_index ASC LIMIT 1",
            (session_id,),
        ) as cur:
            next_row = await cur.fetchone()
        if next_row is None:
            await conn.execute(
                "UPDATE plans SET status = 'completed', updated_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )
            await conn.commit()
            return {"status": "success", "message": "All steps completed!"}
        await conn.execute(
            "UPDATE plans SET current_step = ?, updated_at = ? WHERE session_id = ?",
            (next_row["step_index"], _now(), session_id),
        )
        await conn.commit()
        return {
            "status": "success",
            "next_step": {
                "step_index": next_row["step_index"],
                "description": next_row["description"],
            },
        }
    finally:
        await conn.close()


async def abandon_plan(session_id: str) -> bool:
    """Mark an active plan as abandoned. Returns True if a plan was changed."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "UPDATE plans SET status = 'abandoned', updated_at = ? "
            "WHERE session_id = ? AND status = 'active'",
            (_now(), session_id),
        )
        changed = (cur.rowcount or 0) > 0
        await conn.commit()
        return changed
    finally:
        await conn.close()


async def get_active_plan_context(session_id: str) -> str | None:
    """Returns a short text summary of the active plan for injection by
    `PlanEnforcerPlugin` into the LLM prompt. Returns None when there's no
    active plan.
    """
    conn = await _connect()
    try:
        async with conn.execute(
            "SELECT task, current_step FROM plans "
            "WHERE session_id = ? AND status = 'active'",
            (session_id,),
        ) as cur:
            plan_row = await cur.fetchone()
        if plan_row is None:
            return None
        async with conn.execute(
            "SELECT step_index, description, status FROM plan_steps "
            "WHERE session_id = ? ORDER BY step_index ASC",
            (session_id,),
        ) as cur:
            all_rows = await cur.fetchall()
        if not all_rows:
            return None
        total = len(all_rows)
        done = sum(1 for r in all_rows if r["status"] == "done")
        in_progress = next((r for r in all_rows if r["status"] == "in_progress"), None)
        if in_progress:
            return (
                f"[ACTIVE PLAN: {plan_row['task'][:120]}] Progress {done}/{total}. "
                f"CURRENT STEP ({in_progress['step_index']}): {in_progress['description']}. "
                f"Focus ONLY on this step. Call complete_step(result=...) when finished."
            )
        return (
            f"[ACTIVE PLAN: {plan_row['task'][:120]}] Progress {done}/{total}. "
            f"No step in progress. Call get_next_step to claim the next one."
        )
    finally:
        await conn.close()


async def get_plan_status(session_id: str) -> dict[str, Any] | None:
    """Full plan dump for the agent-facing `get_plan_status` tool.

    Returns None when no plan exists for the session.
    """
    conn = await _connect()
    try:
        async with conn.execute(
            "SELECT task, status, current_step, created_at, updated_at "
            "FROM plans WHERE session_id = ?",
            (session_id,),
        ) as cur:
            plan_row = await cur.fetchone()
        if plan_row is None:
            return None
        async with conn.execute(
            "SELECT step_index, description, status, result FROM plan_steps "
            "WHERE session_id = ? ORDER BY step_index ASC",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {
            "task": plan_row["task"],
            "status": plan_row["status"],
            "current_step": plan_row["current_step"],
            "created_at": plan_row["created_at"],
            "updated_at": plan_row["updated_at"],
            "steps": [
                {
                    "index": r["step_index"],
                    "description": r["description"],
                    "status": r["status"],
                    "result": r["result"],
                }
                for r in rows
            ],
        }
    finally:
        await conn.close()
