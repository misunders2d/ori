"""Model hot-swap and thinking-mode toggle tools.

Cross-provider hot-swap works without restart for any LiteLlm-routed agent
(LiteLlm is the BaseLlm class; the model string changes, the class doesn't).
Native-class agents (`google_search`, `embedding` — see PINNED_COMPONENTS)
ignore overrides; attempting to swap them returns a structured error.

`set_thinking_mode(on)` flips `state.use_thinking`. ModelConfigPlugin reads
this on `before_model_callback` and strips `thinking_config` per request
when off.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from google.adk.tools.tool_context import ToolContext

from app.util.models import (
    MODEL_DEFAULTS,
    PROVIDER_REGISTRY,
    VALID_COMPONENTS,
    format_model_assignments,
    get_default_model,
    get_model_string,
    is_pinned,
)

logger = logging.getLogger(__name__)


async def list_available_models(
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Show the current effective model per component, plus the registry of
    available providers and the per-component defaults."""
    state = tool_context.state.to_dict() if (tool_context and tool_context.state) else {}
    overrides = state.get("model") or {}
    rows: list[dict[str, str]] = []
    for component in sorted(MODEL_DEFAULTS):
        default = MODEL_DEFAULTS[component]
        env_str = get_model_string(component)
        override = overrides.get(component)
        effective = override or env_str
        source = "state" if override else ("env" if env_str != default else "default")
        if is_pinned(component):
            source += " (pinned)"
        rows.append({
            "component": component,
            "default": default,
            "effective": effective,
            "source": source,
        })
    return {
        "status": "success",
        "providers": sorted(PROVIDER_REGISTRY.keys()),
        "components": rows,
        "table": format_model_assignments(markdown=False),
    }


async def set_agent_model(
    component: Annotated[str, "Target agent or service (e.g. 'CoordinatorAgent', 'DeveloperAgent', 'summarizer')"],
    model: Annotated[str, "Model string in <provider>/<rest> form, e.g. 'litellm/anthropic/claude-3-5-sonnet-20241022' or 'gemini/gemini-2.5-flash'"],
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Hot-swap the model used by a component on the next LLM call.

    Pinned components (google_search, embedding) reject swap attempts —
    they require their native ADK class for protocol-specific features
    (Gemini-native search grounding, Gemini embedding endpoint).
    """
    if tool_context is None:
        return {"status": "error", "message": "set_agent_model requires tool_context"}
    if component not in VALID_COMPONENTS:
        return {
            "status": "error",
            "error_code": "UNKNOWN_COMPONENT",
            "message": (
                f"Unknown component '{component}'. "
                f"Valid: {sorted(VALID_COMPONENTS)}"
            ),
        }
    if is_pinned(component):
        return {
            "status": "error",
            "error_code": "COMPONENT_PINNED",
            "message": (
                f"Component '{component}' is pinned to its native model class — "
                "hot-swap is not supported. The pin is required for protocol "
                "features (e.g. native Google Search grounding, Gemini embedding)."
            ),
        }
    provider, _, _ = model.partition("/")
    if provider not in PROVIDER_REGISTRY:
        return {
            "status": "error",
            "error_code": "UNKNOWN_PROVIDER",
            "message": (
                f"Provider '{provider}' is not registered. "
                f"Available: {sorted(PROVIDER_REGISTRY.keys())}. "
                "Add a new one via app/util/models.py:PROVIDER_REGISTRY."
            ),
        }
    # Stage in session state. ModelConfigPlugin picks it up on the next
    # before_model_callback.
    overrides = (tool_context.state.to_dict() if tool_context.state else {}).get("model") or {}
    overrides[component] = model
    tool_context.state["model"] = overrides
    old = get_model_string(component)
    return {
        "status": "success",
        "message": f"Model for {component} changed: {old} -> {model}. Effect on next LLM call.",
        "old": old,
        "new": model,
    }


async def set_thinking_mode(
    on: Annotated[bool, "True enables thinking_config on LLM requests; False strips it"],
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Toggle the LLM's thinking/planner mode for this session.

    When off (default), ModelConfigPlugin strips `thinking_config` from
    every request. Useful when verbose reasoning isn't worth the latency.
    """
    if tool_context is None:
        return {"status": "error", "message": "set_thinking_mode requires tool_context"}
    tool_context.state["use_thinking"] = bool(on)
    state = "ON" if on else "OFF"
    return {
        "status": "success",
        "message": f"Thinking mode is now {state}. Effect on next LLM call.",
    }


async def reset_model_override(
    component: Annotated[str, "Component to reset; pass empty string to reset all"] = "",
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Clear a specific component's hot-swap override (or all overrides).

    Effect on the next LLM call: the component falls back to its
    `MODEL_<component>` env var, then to `MODEL_DEFAULTS`.
    """
    if tool_context is None:
        return {"status": "error", "message": "reset_model_override requires tool_context"}
    overrides = (tool_context.state.to_dict() if tool_context.state else {}).get("model") or {}
    if not component:
        cleared = list(overrides.keys())
        tool_context.state["model"] = {}
        if cleared:
            return {"status": "success", "message": f"Cleared {len(cleared)} override(s): {cleared}"}
        return {"status": "success", "message": "No overrides to clear."}
    if component in overrides:
        del overrides[component]
        tool_context.state["model"] = overrides
        return {
            "status": "success",
            "message": f"Cleared override for {component}. Falls back to {get_default_model(component)}.",
        }
    return {"status": "success", "message": f"No override set for {component}."}
