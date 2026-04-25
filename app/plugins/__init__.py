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
  6. PlanEnforcerPlugin           — inject active plan context into LLM call
  7. A2APrivacyPlugin             — block credential leaks in outbound A2A
  8. OutputSanitizerPlugin        — scan high-risk tool outputs (multilingual-safe)
  9. VerifyRetryPlugin            — 3-strike cap on evolution_verify_sandbox
 10. BinaryContentScannerPlugin   — validate inbound A2A binary parts
 11. ModelErrorHandlerPlugin      — translate raw LLM errors into user-visible
                                    messages (rate limit, auth, model-not-found, …)
"""

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
    "StateInitializerPlugin",
    "VerifyRetryPlugin",
]
