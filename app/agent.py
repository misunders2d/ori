"""Ori App — the single-source App definition.

Plan execution uses the legacy nudge-based pattern: the coordinator is
the root agent (no workflow wrapping), and `app/tasks.py`'s
`_drive_plan_to_completion` re-invokes the runner with a continuation
prompt while pending steps remain. The agent itself calls the planner
tools (`get_next_step`, `complete_step`, `abandon_plan`) — exposed by
`PlannerToolset` — to traverse the plan.

Why no workflow: ADK 2.0's `ctx.run_node` boundary swallows output for
chat-mode agents (returns None) and is unreliable for task-mode agents
that delegate via `transfer_to_agent`. Iterating `runner.run_async()`
events directly (legacy pattern) captures all sub-agent transfers and
tool results cleanly.

Wires together:
- Root agent: the coordinator LlmAgent (mode='chat').
- Plugins: ten in registration order.
- Events compaction: every 10 events.
- Resumability: enabled so OAuth flows can pause/resume.

Native ADK 2.0 services (memory_service, credential_service,
artifact_service) are wired at the Runner level in `run_bot.py`.
"""

from __future__ import annotations

import os

from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps import App
from google.adk.apps.app import EventsCompactionConfig, ResumabilityConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

from app.agents.coordinator import root_agent as coordinator_agent
from app.plugins import (
    A2APrivacyPlugin,
    AdminGatePlugin,
    BinaryContentScannerPlugin,
    ModelConfigPlugin,
    ModelErrorHandlerPlugin,
    OutputSanitizerPlugin,
    PerimeterAclPlugin,
    PlanEnforcerPlugin,
    PromptInjectionGuardPlugin,
    ReflectAndRetryToolPlugin,
    StateInitializerPlugin,
    VerifyRetryPlugin,
)
from app.util.models import get_model

app_name = os.environ.get("APP_NAME", "ori")


# Plugin order matters. Documented in app/plugins/__init__.py and
# enforced here. Each comment explains WHY this position.
PLUGINS = [
    # First: hard ACL. Reject perimeter outsiders before any state setup
    # or model call burns tokens.
    PerimeterAclPlugin(),
    # Admin gate runs before tool calls AND before agent invocation
    # (DeveloperAgent is admin-only). Returns ACT-XXXXXX on staged actions.
    AdminGatePlugin(),
    # Bootstrap session state — runs once on root agent start. Idempotent.
    StateInitializerPlugin(),
    # Hot-swap model + thinking config BEFORE prompt-injection check, so
    # the injection check runs against the post-config request.
    ModelConfigPlugin(),
    # Inject the system directive + run semantic injection check.
    PromptInjectionGuardPlugin(),
    # Plan enforcement: when an active plan exists for the session,
    # PlanEnforcerPlugin appends the plan-context directive to the
    # coordinator's system_instruction (via LlmRequest.append_instructions)
    # on every turn so the LLM is reminded to call get_next_step /
    # complete_step. PlannerToolset exposes those tools. tasks.py's
    # `_drive_plan_to_completion` re-invokes the runner with a
    # continuation prompt while pending steps remain so the LLM can't
    # drop the plan even if it forgets to traverse mid-turn.
    PlanEnforcerPlugin(),
    # ADK 2.0 built-in: intercept tool errors (e.g. "Tool 'X' not
    # found" when a sub-agent hallucinates a coordinator-only tool, or
    # any tool that raises) and return a structured reflection to the
    # LLM as the function_response. Without this, ValueError propagates
    # all the way out of runner.run_async, killing the whole turn;
    # with it, the LLM sees the error in-context and can correct
    # (e.g. call transfer_to_agent('CoordinatorAgent') instead, or
    # pick a different tool from its actual toolkit).
    ReflectAndRetryToolPlugin(
        max_retries=3,
        throw_exception_if_retry_exceeded=False,
    ),
    # Privacy check on outbound A2A tool calls + responses.
    A2APrivacyPlugin(),
    # Sanitize tool outputs from web_fetch / evolution_read_file.
    OutputSanitizerPlugin(),
    # 3-strike cap on evolution_verify_sandbox failures.
    VerifyRetryPlugin(),
    # Validate inbound A2A binary content (size, magic-bytes).
    BinaryContentScannerPlugin(),
    # Last: catch any model error and translate it to a user-visible
    # message. Position-last so other plugins can short-circuit (block,
    # sanitize) before this; if those don't return a value but the model
    # call itself errors, this hook substitutes a clean message.
    ModelErrorHandlerPlugin(),
]


app = App(
    name=app_name,
    root_agent=coordinator_agent,
    plugins=PLUGINS,
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=10,
        overlap_size=3,
        summarizer=LlmEventSummarizer(llm=get_model("summarizer")),
    ),
    # Cache stable instruction prefixes across LLM calls. Per-agent
    # selection isn't supported by ADK 2.0 (config is App-level), but
    # the min_tokens floor naturally excludes small prompts — so only
    # large instructions like the Developer agent's (~3K tokens) get
    # cached. Smaller agents fall below the floor and skip caching.
    # 30-minute TTL matches Gemini's max cache duration.
    context_cache_config=ContextCacheConfig(
        cache_intervals=10,
        ttl_seconds=1800,
        min_tokens=2048,
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
)


# Re-export the root for `app/a2a_server.py` and `run_bot.py`.
root_agent = app.root_agent


__all__ = ["PLUGINS", "app", "app_name", "root_agent"]
