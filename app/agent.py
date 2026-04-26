"""Ori App — the single-source App definition.

Replicates the legacy amazon_manager flow: the Coordinator is the root
agent directly (no Workflow wrapper). Plan-and-execute continuation, when
needed for scheduled tasks, is driven by the legacy
`_drive_plan_to_completion` re-prompt loop in `app/tasks.py`. Interactive
chat returns one response per user message — the agent doesn't auto-loop.

The Workflow wrapper experiment caused two production bugs:
- The loop edge re-fired the coordinator after `task.cancel()` had killed
  the runner task, leading to image-gen tools running multiple times for
  one user request even after the user said "stop".
- The plan_completion_check ran on every turn, occasionally re-routing
  back to the coordinator even when no plan was active.

Legacy used `App(root_agent=coordinator)` directly. We do the same.

Wires together:
- Root agent: the Coordinator agent directly (replicates legacy).
- Plugins: ten in registration order. AdminGate → PerimeterAcl runs
  *first* so denied calls short-circuit before state init or the model.
  ModelConfig runs before PromptInjection so hot-swapped models see the
  injected system directive.
- Events compaction: every 10 events, summarized via the configured
  summarizer; last 3 kept raw.
- Resumability: enabled so OAuth flows (RequestCredential / RequestInput)
  can pause and resume cleanly.

Native ADK 2.0 services (memory_service, credential_service,
artifact_service) are wired at the Runner level in `run_bot.py`.
"""

from __future__ import annotations

import os

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
    StateInitializerPlugin,
    VerifyRetryPlugin,
)
from app.util.models import get_model

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
    root_agent=coordinator_agent,
    plugins=PLUGINS,
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=10,
        overlap_size=3,
        summarizer=LlmEventSummarizer(llm=get_model("summarizer")),
    ),
    resumability_config=ResumabilityConfig(is_resumable=True),
)


# Re-export the root for `app/a2a_server.py` and `run_bot.py`.
root_agent = app.root_agent


__all__ = ["PLUGINS", "app", "app_name", "root_agent"]
