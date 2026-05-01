"""Ori App — the single-source App definition.

ADK 2.0 native: root_agent is a `Workflow` using the dynamic-workflow
pattern (per https://adk.dev/workflows/dynamic/). Single `@node`-
decorated async function runs the coordinator once, then loops with a
continuation prompt while a plan has pending steps. All control flow is
Python — cancellation propagates through asyncio await semantics
(no re-firing after task.cancel()).

Wires together:
- Root agent: the plan_executor_workflow (Workflow with one @node).
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

from app.plugins import (
    A2APrivacyPlugin,
    AdminGatePlugin,
    BinaryContentScannerPlugin,
    ModelConfigPlugin,
    ModelErrorHandlerPlugin,
    OutputSanitizerPlugin,
    PerimeterAclPlugin,
    PromptInjectionGuardPlugin,
    StateInitializerPlugin,
    VerifyRetryPlugin,
)
from app.util.models import get_model
from app.workflows.plan_executor import plan_executor_workflow

# Custom summarizer prompt — the default collapses code/SQL/task definitions
# into prose, which has caused the agent to reconstruct missing details from
# unrelated memory recalls (e.g. pulling a stale MSRP task into an FBA-task
# scheduling call). The verbatim-preservation clauses force the summarizer to
# keep literal blocks intact so downstream turns don't have to guess.
_SUMMARIZER_PROMPT_TEMPLATE = (
    "Summarize the following conversation between a user and an AI agent. "
    "Capture key information, decisions, and unresolved tasks.\n\n"
    "Begin your output with this exact marker on its own line:\n"
    "[CONVERSATION SUMMARY — older turns were compacted; if the user references "
    "specific text that is not preserved verbatim below, ASK them to repeat it "
    "rather than guess]\n\n"
    "STRICT PRESERVATION RULES — you MUST copy these verbatim into the summary, "
    "never paraphrasing or omitting them:\n"
    "1. Code blocks — any text fenced with triple backticks (```), in any language.\n"
    "2. SQL queries, even unfenced.\n"
    "3. URLs, IDs, channel handles (sl_*, tg_*, mem_*, ASIN codes), file paths.\n"
    "4. Tool-call invocations with their full arguments.\n"
    "5. The most recently approved task_prompt for any schedule_*_task / "
    "edit_scheduled_task discussion — copy the user's exact phrasing AND the "
    "agent's full task description that the user approved.\n"
    "6. Any text the user explicitly told the agent to remember verbatim.\n\n"
    "For everything else, be concise.\n\n"
    "Conversation:\n{conversation_history}"
)

# state_schema is attached at the Workflow level (root_agent), not on App —
# ADK 2.0's App doesn't carry a state_schema field directly.


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
    # Plan enforcement is now done in code by the workflow itself
    # (app/workflows/plan_executor.py drives the step loop deterministically),
    # so no PlanEnforcerPlugin nudge is needed.
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
    root_agent=plan_executor_workflow,
    plugins=PLUGINS,
    events_compaction_config=EventsCompactionConfig(
        # Less aggressive than the previous 10/3. Gemini Flash has a 1M-token
        # context — compacting every 10 events is wasteful and was the root
        # cause of the FBA→MSRP scheduling contamination (overlap=3 left the
        # agent with too few raw turns to ground on, forcing memory-recall
        # reconstruction). 30/10 means most conversations never hit compaction;
        # those that do still have 10 raw turns of anchor.
        compaction_interval=30,
        overlap_size=10,
        summarizer=LlmEventSummarizer(
            llm=get_model("summarizer"),
            prompt_template=_SUMMARIZER_PROMPT_TEMPLATE,
        ),
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
