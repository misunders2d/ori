"""ADK 2.0 plugins for Ori.

All guardrails attach at the App level via `App(plugins=[...])`. Order
matters — plugins fire in registration order, and the first to return a
value short-circuits the rest. The canonical order is documented here
and enforced in `app/agent.py`:

  1. PerimeterAclPlugin           — perimeter ACL (drop unwhitelisted users)
  2. AdminGatePlugin              — admin role gating + ACT-XXXXXX staging
  3. StateInitializerPlugin       — session state bootstrap (idempotent)
  4. ModelConfigPlugin            — runtime model hot-swap + thinking toggle
  5. PromptInjectionGuardPlugin   — semantic injection check + system directive
  6. PlanEnforcerPlugin           — inject active-plan directive into the
                                    coordinator's system_instruction so the
                                    LLM is reminded to call get_next_step /
                                    complete_step on every turn
  7. SubAgentPopperPlugin         — emit end_of_agent=True on every
                                    sub-agent's after_agent_callback so
                                    ADK's _find_agent_to_run resume walk
                                    skips sub-agent events and pops back
                                    to the coordinator (root). Fixes the
                                    chat-mode "sub-agent stays active
                                    after transfer_to_agent" bug.
  8. ReflectAndRetryToolPlugin    — ADK 2.0 built-in: when a tool call
                                    raises (e.g. 'Tool X not found'),
                                    intercept the error and return a
                                    structured reflection as the
                                    function_response, so the LLM sees
                                    the failure in context and can
                                    self-correct without crashing the turn
  9. A2APrivacyPlugin             — block credential leaks in outbound A2A
 10. OutputSanitizerPlugin        — scan high-risk tool outputs (multilingual-safe)
 11. VerifyRetryPlugin            — 3-strike cap on evolution_verify_sandbox
 12. BinaryContentScannerPlugin   — validate inbound A2A binary parts
 13. ModelErrorHandlerPlugin      — translate raw LLM errors into user-visible
                                    messages (rate limit, auth, model-not-found, …)
"""

from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin

from app.plugins.a2a_privacy import A2APrivacyPlugin
from app.plugins.admin_gate import AdminGatePlugin
from app.plugins.binary_content_scanner import BinaryContentScannerPlugin
from app.plugins.model_config import ModelConfigPlugin
from app.plugins.model_error_handler import ModelErrorHandlerPlugin
from app.plugins.output_sanitizer import OutputSanitizerPlugin
from app.plugins.perimeter import PerimeterAclPlugin
from app.plugins.plan_enforcer import PlanEnforcerPlugin
from app.plugins.prompt_injection import PromptInjectionGuardPlugin
from app.plugins.state_initializer import StateInitializerPlugin
from app.plugins.sub_agent_popper import SubAgentPopperPlugin
from app.plugins.verify_retry import VerifyRetryPlugin

__all__ = [
    "A2APrivacyPlugin",
    "AdminGatePlugin",
    "BinaryContentScannerPlugin",
    "ModelConfigPlugin",
    "ModelErrorHandlerPlugin",
    "OutputSanitizerPlugin",
    "PerimeterAclPlugin",
    "PlanEnforcerPlugin",
    "PromptInjectionGuardPlugin",
    "ReflectAndRetryToolPlugin",
    "StateInitializerPlugin",
    "SubAgentPopperPlugin",
    "VerifyRetryPlugin",
]
