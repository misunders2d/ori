"""Typed session state schema for Ori on ADK 2.0.

Replaces the magic-dict state of the legacy implementation. Every key the
runtime, plugins, or tools read/write to `tool_context.state` should be
declared here with a default value, so absent keys never cause KeyErrors
and the contract is documented in one place.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OriSessionState(BaseModel):
    """Per-session state — Pydantic-validated, attached at App level via state_schema."""

    model_config = {"extra": "allow"}

    # ----- Identity ---------------------------------------------------------
    user_id: str | None = None
    """The actual platform user id of the current speaker. In group chats this
    differs from the session id (which is the chat id). Plugins read this to
    enforce admin / per-user permissions."""

    master_user_id: list[str] = Field(default_factory=list)
    """Admin user ids loaded from ADMIN_USER_IDS at session init."""

    bot_name: str = "Ori"
    """Display name; used in agent instructions and admin alerts."""

    # ----- User profile -----------------------------------------------------
    user_preferences: str = ""
    """Free-form markdown preferences (timezone, comm style, etc.). Loaded from
    disk by StateInitializerPlugin on session start; written by the
    save_user_preferences tool. Interpolated into agent instructions via
    `{user_preferences}`."""

    # ----- Model / generation -----------------------------------------------
    model: dict[str, str] = Field(default_factory=dict)
    """Per-component model overrides for hot-swap. Keys are agent names
    (e.g. 'CoordinatorAgent') or service names ('embedding', 'summarizer').
    Values are model strings in `<provider>/<rest>` form, e.g.
    `'litellm/anthropic/claude-3-5-sonnet-20241022'`. ModelConfigPlugin
    reads these per LLM call. Pinned components (e.g. google_search) ignore
    overrides — see app/util/models.py."""

    use_thinking: bool = False
    """When False, ModelConfigPlugin strips `thinking_config` from each
    LLM request. Toggled by the `set_thinking_mode` tool."""

    # ----- Plan / execution -------------------------------------------------
    plan_id: str | None = None
    """Session-scoped pointer into the durable plan store. Set by seed_plan
    and create_plan; cleared on plan completion or abandonment."""

    verify_failure_count: int = 0
    """Consecutive failures of evolution_verify_sandbox in this session.
    VerifyRetryPlugin caps at 3 and resets on success."""

    plan_workflow_iters: int = 0
    """Loop counter for plan_executor workflow (capped at MAX_ITERATIONS).
    Incremented in plan_completion_check; reset implicitly on new session."""

    evolution_cycle_active: bool = False
    """True while an evolution cycle is in progress (from
    evolution_stage_change through evolution_commit_and_push). Used by
    evolution tools to detect and prevent overlapping cycles."""

    use_planner: bool = False
    """When True, the agent prefers plan-and-execute over single-turn replies
    for non-trivial tasks. Toggled by the `set_use_planner` system tool."""
