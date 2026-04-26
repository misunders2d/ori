"""ModelErrorHandlerPlugin — translate raw LLM errors into user-visible messages.

Without this plugin, model errors (rate limits, auth failures, bad model
names, transient network) propagate up as raw stack traces in the logs and
either crash the turn or get silently swallowed by the runner. The user
sees nothing useful and the operator drowns in tracebacks.

This plugin's `on_model_error_callback`:
  - Classifies the exception (rate-limit, auth, not-found, network, other)
  - Logs a single-line WARNING (no traceback unless DEBUG is on)
  - Returns a synthetic LlmResponse so the agent's turn ends with a
    human-readable explanation of WHAT failed and WHAT to do next, rather
    than crashing.

Substituting the response (vs. re-raising None) is deliberate: a 4-attempt
retry storm on a rate-limited model bounces the agent for 30+ seconds while
the user waits in the dark. A single clear message is friendlier and lets
them swap models or retry on their own terms.
"""

from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

logger = logging.getLogger(__name__)


# Map error class-name fragments → (short label, hint to give the user)
# Match by lowercase class name fragments because providers wrap errors
# inconsistently (litellm.RateLimitError, openai.RateLimitError, etc.).
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    ("ratelimit",          "rate limited",
        "Try again in a moment, or swap to another model: "
        "`set_agent_model(\"<Component>\", \"litellm/gemini/gemini-3.1-flash-lite-preview\")`."),
    ("authentication",     "authentication failed",
        "The provider rejected the API key. Check the relevant credential in vault "
        "(GOOGLE_API_KEY / ANTHROPIC_API_KEY / OPENROUTER_API_KEY)."),
    ("apikey",             "API key invalid",
        "Re-set the provider's API key via configure_integration."),
    ("notfound",           "model not found",
        "The model name doesn't exist on the provider. List available models or "
        "swap to a known one with `set_agent_model`."),
    ("badrequest",         "bad request",
        "Provider rejected the request shape. Check the model string and parameters."),
    ("permissiondenied",   "permission denied",
        "Provider denied the request. Verify the API key has access to this model."),
    ("timeout",            "timed out",
        "Network or provider timeout. Retry; if it persists, swap models."),
    ("contextlengthexceeded", "context too long",
        "The conversation is too long for this model's context window. Try /reset "
        "or swap to a model with larger context."),
    ("connection",         "connection error",
        "Network blip reaching the provider. Retry."),
]


def _classify(exc: Exception) -> tuple[str, str]:
    """(label, hint) for the given exception. Falls back to generic."""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    for fragment, label, hint in _ERROR_PATTERNS:
        if fragment in name or fragment in msg:
            return label, hint
    return "model error", "Retry, or swap models with `set_agent_model`."


def _short_error(exc: Exception) -> str:
    """Single-line summary of an exception for log + user message."""
    msg = str(exc)
    # Some provider errors are JSON blobs hundreds of chars long. Trim.
    if len(msg) > 280:
        msg = msg[:277] + "..."
    # Drop linebreaks so it stays on one log line.
    msg = msg.replace("\n", " ").replace("\r", " ").strip()
    return f"{type(exc).__name__}: {msg}"


class ModelErrorHandlerPlugin(BasePlugin):
    """Translate raw LLM errors to clean user-visible messages."""

    def __init__(self) -> None:
        super().__init__(name="model_error_handler")

    async def on_model_error_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
        error: Exception,
    ) -> LlmResponse | None:
        agent = getattr(callback_context, "agent_name", "?")
        model = getattr(llm_request, "model", "?")
        label, hint = _classify(error)
        short = _short_error(error)

        # One concise WARNING — no traceback at INFO level. The full traceback
        # is still on the exception object if a future plugin wants it.
        logger.warning(
            "Model error on %s (model=%s): [%s] %s",
            agent, model, label, short,
        )

        # Build a friendly response the user actually sees.
        text = (
            f"⚠ The current model ({model}) failed: {label}.\n"
            f"Detail: {short}\n\n"
            f"{hint}"
        )
        return LlmResponse(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=text)],
            ),
        )
