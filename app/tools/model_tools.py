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

import asyncio
import logging
import os
from typing import Annotated, Any

from google.adk.models import LlmRequest
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.util.models import (
    MODEL_DEFAULTS,
    PROVIDER_REGISTRY,
    VALID_COMPONENTS,
    format_model_assignments,
    get_auth_mode,
    get_default_model,
    get_model_string,
    is_pinned,
)

logger = logging.getLogger(__name__)

# Probe budget: model swap is human-in-the-loop, so we can wait a bit, but
# 30s feels long. Most reachable models respond to a 1-token "ping" in <5s.
_PROBE_TIMEOUT_SECONDS = 15


async def _probe_model(model: str) -> tuple[bool, str]:
    """Construct and ping a model. Returns (ok, message).

    Used by set_agent_model and verify_model_reachable to fail-fast before
    persisting a swap that would brick the agent. The probe sends a 1-token
    completion request — cheap, fast, and exercises the full provider path
    (auth, model name, network). Any exception => not reachable.
    """
    provider, _, remainder = model.partition("/")
    factory = PROVIDER_REGISTRY.get(provider)
    if factory is None:
        return False, (
            f"Provider '{provider}' is not registered. "
            f"Available: {sorted(PROVIDER_REGISTRY.keys())}."
        )
    try:
        llm = factory(remainder, {})
    except Exception as exc:
        return False, f"Could not construct model: {type(exc).__name__}: {exc}"

    # Critical: use `llm.model` (the string the underlying client expects),
    # NOT the outer Ori-prefixed `model` (e.g. "litellm/gemini/..."). The
    # factory already stripped the prefix when constructing the LLM —
    # passing the unstripped string into LlmRequest makes litellm see
    # "litellm/" as a provider name and bail with "LLM Provider NOT
    # provided", which broke probes for every litellm/openrouter swap.
    req = LlmRequest(
        model=llm.model,
        contents=[types.Content(role="user", parts=[types.Part.from_text(text="ping")])],
        config=types.GenerateContentConfig(max_output_tokens=4),
    )

    async def _run():
        async for _resp in llm.generate_content_async(req):
            return  # first response is enough — provider is reachable
    try:
        await asyncio.wait_for(_run(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False, f"Probe timed out after {_PROBE_TIMEOUT_SECONDS}s — provider unreachable or model very slow."
    except Exception as exc:
        # Surface the underlying error: missing API key, unknown model name,
        # provider down, malformed model string, etc.
        return False, f"Probe failed: {type(exc).__name__}: {exc}"
    return True, "ok"


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


async def verify_model_reachable(
    model: Annotated[str, "Model string in <provider>/<rest> form, e.g. 'openrouter/deepseek/deepseek-chat'"],
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Probe a model with a 1-token 'ping' request. Does NOT persist anything.

    Use this before set_agent_model when trying an unfamiliar model — confirms
    the provider's API key is valid, the model name exists, and the endpoint
    is reachable. Returns success/failure with the underlying error if any.
    """
    ok, msg = await _probe_model(model)
    if ok:
        return {"status": "success", "model": model, "message": f"Model '{model}' is reachable."}
    return {"status": "error", "error_code": "MODEL_UNREACHABLE", "model": model, "message": msg}


async def set_agent_model(
    component: Annotated[str, "Target agent or service (e.g. 'CoordinatorAgent', 'DeveloperAgent', 'summarizer')"],
    model: Annotated[str, "Model string in <provider>/<rest> form, e.g. 'litellm/anthropic/claude-3-5-sonnet-20241022' or 'gemini/gemini-2.5-flash'"],
    skip_probe: Annotated[bool, "Skip the reachability probe. Default False — only set True when probing isn't possible (e.g. known offline)."] = False,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Hot-swap the model used by a component on the next LLM call.

    BEFORE persisting, probes the model with a 1-token request to verify it's
    reachable (provider configured, model exists, network up). If the probe
    fails, the override is NOT persisted — the agent keeps its working model.
    Set `skip_probe=True` to bypass (only when you know the probe will fail
    transiently but want to swap anyway).

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

    # Pre-flight probe — refuse to persist a model we can't actually reach.
    # This is the fail-safe against bricking the agent's own LLM access.
    if not skip_probe:
        ok, probe_msg = await _probe_model(model)
        if not ok:
            return {
                "status": "error",
                "error_code": "MODEL_UNREACHABLE",
                "message": (
                    f"Refusing to swap {component}: probe of '{model}' failed. "
                    f"State unchanged — current model still active. Reason: {probe_msg}"
                ),
                "probe_error": probe_msg,
            }

    # Stage in session state. ModelConfigPlugin picks it up on the next
    # before_model_callback.
    overrides = (tool_context.state.to_dict() if tool_context.state else {}).get("model") or {}
    overrides[component] = model
    tool_context.state["model"] = overrides
    old = get_model_string(component)
    return {
        "status": "success",
        "message": f"Model for {component} changed: {old} -> {model}. Probe passed. Effect on next LLM call.",
        "old": old,
        "new": model,
        "probe": "passed" if not skip_probe else "skipped",
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


def get_llm_provider(tool_context: ToolContext = None) -> dict:
    """Show the current LLM provider mode (API key vs Vertex AI) and its status.

    Use this when the user asks what mode they're in, or before switching
    providers. Returns the active mode plus readiness flags for switching
    to the other mode.
    """
    auth = get_auth_mode()
    mode_label = "Google One / Vertex AI" if auth["vertex_ai"] else "Direct API keys"
    result: dict[str, Any] = {
        "status": "success",
        "current_mode": mode_label,
        "details": auth,
    }
    if auth["vertex_ai"]:
        has_api_key = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
        result["can_switch_to_api_key"] = has_api_key
        if not has_api_key:
            result["missing_for_api_key"] = ["GOOGLE_API_KEY"]
    else:
        has_project = bool(os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip())
        has_adc = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())
        result["can_switch_to_vertex"] = has_project
        missing = []
        if not has_project:
            missing.append("GOOGLE_CLOUD_PROJECT (GCP project ID)")
        if not has_adc:
            result["adc_note"] = (
                "No service account configured (GOOGLE_APPLICATION_CREDENTIALS). "
                "This is fine if ADC is set up via "
                "'gcloud auth application-default login' on the server."
            )
        if missing:
            result["missing_for_vertex"] = missing
    return result


def switch_llm_provider(
    target: Annotated[str, "Either 'api_key' or 'vertex'"],
    tool_context: ToolContext = None,
) -> dict:
    """Switch between Direct API key mode and Vertex AI mode.

    Validates prerequisites BEFORE flipping the vault key. The change takes
    effect on the next LLM call (the runner's model resolution re-reads the
    env at request time).

    Args:
        target: 'api_key' for Direct API key mode, 'vertex' for Vertex AI
            (Google One / ADC) mode.
    """
    from deploy.vault import set as vault_set

    target = (target or "").strip().lower()
    if target not in ("api_key", "vertex"):
        return {
            "status": "error",
            "error_code": "INVALID_TARGET",
            "message": "target must be 'api_key' or 'vertex'.",
        }

    current = get_auth_mode()
    current_is_vertex = current["vertex_ai"]

    if target == "vertex" and current_is_vertex:
        return {"status": "no_change", "message": "Already in Vertex AI mode."}
    if target == "api_key" and not current_is_vertex:
        return {"status": "no_change", "message": "Already in Direct API key mode."}

    if target == "vertex":
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            return {
                "status": "error",
                "error_code": "MISSING_GCP_PROJECT",
                "message": (
                    "Cannot switch to Vertex AI: GOOGLE_CLOUD_PROJECT is not "
                    "configured. Use configure_integration to set it first."
                ),
            }
        vault_set("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
        return {
            "status": "success",
            "message": (
                f"Switched to Vertex AI mode (project: {project}). "
                "Effect on next LLM call."
            ),
        }

    # target == "api_key"
    has_key = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
    if not has_key:
        return {
            "status": "error",
            "error_code": "MISSING_API_KEY",
            "message": (
                "Cannot switch to API key mode: GOOGLE_API_KEY is not "
                "configured. Use configure_integration to set it first."
            ),
        }
    vault_set("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
    return {
        "status": "success",
        "message": "Switched to Direct API key mode. Effect on next LLM call.",
    }
