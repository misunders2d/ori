"""ModelConfigPlugin — runtime model hot-swap and thinking toggle.

Owns the per-LLM-call mutations driven by `state.model` (per-component
model overrides) and `state.use_thinking` (thinking_config gate). Runs
before_model_callback so changes take effect on the very next call.

Cross-provider hot-swap works for LiteLlm-backed agents because the agent
class is fixed at construction (LiteLlm) and only the model string changes.
For agents whose BaseLlm class is native (e.g., Gemini for google_search),
model overrides are pinned at construction time and ignored here — see
`PINNED_COMPONENTS` in `app/util/models.py`.
"""

from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin

from app.util.models import is_pinned

logger = logging.getLogger(__name__)


class ModelConfigPlugin(BasePlugin):
    """Per-call model swap + thinking toggle from session state."""

    def __init__(self) -> None:
        super().__init__(name="model_config")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        agent_name = callback_context.agent_name
        state = callback_context.state.to_dict() if callback_context.state else {}
        model_overrides = state.get("model") or {}

        # Hot-swap. Pinned components are skipped (they MUST stay on their
        # native BaseLlm class — e.g. google_search needs native Gemini).
        if not is_pinned(agent_name):
            override = model_overrides.get(agent_name)
            if override and override != getattr(llm_request, "model", None):
                # The string format `<provider>/<rest>` is canonical here too.
                # For LiteLlm agents the request model accepts the inner
                # `<lite_provider>/<model>` form (e.g. `gemini/gemini-2.5-flash`).
                # Strip the outer `litellm/` if present so the request talks to
                # litellm directly with the model id it expects.
                _, _, request_model = override.partition("/") if override.startswith("litellm/") else ("", "", override)
                llm_request.model = request_model or override
                logger.info(
                    "ModelConfigPlugin: hot-swapped %s -> %s",
                    agent_name, llm_request.model,
                )

        # Thinking toggle. When state.use_thinking is False, strip
        # thinking_config from the request config.
        use_thinking = bool(state.get("use_thinking", False))
        if not use_thinking:
            cfg = getattr(llm_request, "config", None)
            if cfg is not None and hasattr(cfg, "thinking_config"):
                cfg.thinking_config = None

        return None
