"""Ori App — the single-source App definition.

Wires together:
- Root agent: the plan-executor Workflow (which has the coordinator as a
  node and a completion-check loop edge).
- Plugins: ten in registration order. AdminGate → PerimeterAcl runs
  *first* so denied calls short-circuit before state init or the model.
  ModelConfig runs before PromptInjection so hot-swapped models see the
  injected system directive.
- State schema: OriSessionState (Pydantic).
- Events compaction: every 10 events, summarized via the configured
  summarizer; last 3 kept raw.
- Resumability: enabled so OAuth flows (RequestCredential / RequestInput)
  can pause and resume cleanly mid-workflow.

Native ADK 2.0 services (memory_service, credential_service,
artifact_service) are wired at the Runner level in `run_bot.py` (Phase G).
"""

from __future__ import annotations

import os

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
    PlanEnforcerPlugin,
    PromptInjectionGuardPlugin,
    StateInitializerPlugin,
    VerifyRetryPlugin,
)
from app.util.models import get_model
from app.workflows.plan_executor import plan_executor_workflow

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
    # Inject active-plan context. Root agent only (workflow's coord node).
    PlanEnforcerPlugin(),
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
        compaction_interval=10,
        overlap_size=3,
        summarizer=LlmEventSummarizer(llm=get_model("summarizer")),
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
)


# Re-export the root for `app/a2a_server.py` and `run_bot.py`. Both should
# import from here so swapping the workflow vs the bare coordinator (e.g.
# for debugging) is a one-line change in this file.
root_agent = app.root_agent


__all__ = ["app", "app_name", "root_agent", "PLUGINS"]
