"""Tools for runtime model discovery, hot-swapping, and provider switching."""

import logging
import os
from typing import Annotated, Literal

from google.adk.tools.tool_context import ToolContext

from app.app_utils.models import (
    SUPPORTED_PROVIDERS,
    VALID_COMPONENTS,
    get_all_assignments,
    get_auth_mode,
    get_model_string,
    set_model,
    list_provider_models,
    _parse_model_str,
)

logger = logging.getLogger(__name__)


async def list_available_models(
    provider: Annotated[str, "Model provider to query: 'google' or 'anthropic' (default 'google')"] = "google",
    filter: Annotated[str, "Optional substring filter (e.g. 'flash', 'pro')"] = "",
    tool_context: ToolContext = None,
) -> dict:
    """List available models from a provider and show current agent assignments."""
    models = await list_provider_models(provider=provider, filter_str=filter)
    assignments = get_all_assignments()

    return {
        "available_models": models,
        "current_assignments": assignments,
        "valid_components": sorted(VALID_COMPONENTS),
        "auth_mode": get_auth_mode(),
    }


async def set_agent_model(
    component_name: Annotated[str, "Target component (e.g. 'DeveloperAgent', 'channel_summarizer')"],
    model_name: Annotated[str, (
        "Model identifier. MUST call list_available_models first and pick from the returned list. "
        "Do NOT guess or invent model names. "
        "Use 'google/' or 'anthropic/' prefix, or bare name (auto-inferred)."
    )],
    tool_context: ToolContext = None,
) -> dict:
    """Switch the model for a specific agent or component.

    IMPORTANT: You MUST call list_available_models first, then pick a model
    name from the returned list. Arbitrary model names are rejected.
    """
    if component_name not in VALID_COMPONENTS:
        return {
            "status": "error",
            "message": f"Unknown component '{component_name}'. Valid: {sorted(VALID_COMPONENTS)}",
        }

    # Normalize: require provider prefix
    if "/" not in model_name:
        if model_name.startswith("claude"):
            model_name = f"anthropic/{model_name}"
        else:
            model_name = f"google/{model_name}"

    provider, bare_name = _parse_model_str(model_name)

    # Fetch the live model list and validate against it
    available = await list_provider_models(provider=provider)
    if not available or (len(available) == 1 and "error" in available[0]):
        return {
            "status": "error",
            "message": f"Could not fetch model list from '{provider}'. Try again later.",
        }

    valid_names = set()
    for m in available:
        name = m.get("name", "")
        valid_names.add(name)
        valid_names.add(name.replace("models/", ""))

    if bare_name not in valid_names and f"models/{bare_name}" not in valid_names:
        # Show a few similar names to help
        suggestions = sorted(n.replace("models/", "") for n in valid_names if any(
            k in n.lower() for k in bare_name.lower().split("-")[:2]
        ))[:5]
        hint = f" Similar: {suggestions}" if suggestions else " Use list_available_models to see valid options."
        return {
            "status": "error",
            "message": f"Model '{bare_name}' not found on {provider}.{hint}",
        }

    old_model = get_model_string(component_name)

    # Persist to env + vault
    set_model(component_name, model_name)

    # Write to session state for immediate hot-swap (picked up by before_model_callback)
    if tool_context:
        tool_context.state[f"model:{component_name}"] = model_name

    old_provider, _ = _parse_model_str(old_model)
    new_provider, _ = _parse_model_str(model_name)
    cross_provider = old_provider != new_provider

    msg = f"Model for {component_name} changed: {old_model} -> {model_name}"
    if cross_provider:
        msg += " (cross-provider change — takes full effect after restart)"

    return {
        "status": "success",
        "message": msg,
        "restart_required": cross_provider,
    }


def get_llm_provider(tool_context: ToolContext = None) -> dict:
    """Show the current LLM provider mode (API key vs Google One / Vertex AI) and its status.

    Use this when the user asks what mode they're in, or before switching providers.
    """
    auth = get_auth_mode()
    mode_label = "Google One / Vertex AI" if auth["vertex_ai"] else "Direct API keys"

    result = {
        "current_mode": mode_label,
        "details": auth,
    }

    # Check readiness for the OTHER mode (what's needed to switch)
    if auth["vertex_ai"]:
        # Currently Vertex — check if API key mode is available
        has_api_key = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
        result["can_switch_to_api_key"] = has_api_key
        if not has_api_key:
            result["missing_for_api_key"] = ["GOOGLE_API_KEY"]
    else:
        # Currently API key — check if Vertex/Google One is available
        has_project = bool(os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip())
        has_adc = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())
        result["can_switch_to_vertex"] = has_project
        missing = []
        if not has_project:
            missing.append("GOOGLE_CLOUD_PROJECT (GCP project ID)")
        if not has_adc:
            result["adc_note"] = (
                "No service account configured (GOOGLE_APPLICATION_CREDENTIALS). "
                "This is fine if ADC is set up via 'gcloud auth application-default login' on the server."
            )
        if missing:
            result["missing_for_vertex"] = missing

    return result


def switch_llm_provider(
    target: Literal["api_key", "vertex"],
    tool_context: ToolContext = None,
) -> dict:
    """Switch between Direct API key mode and Google One / Vertex AI mode.

    Checks that all prerequisites are met before switching. The change takes
    effect on the next message (the runner auto-recreates).

    Args:
        target: 'api_key' for Direct API key mode, 'vertex' for Google One / Vertex AI mode.
    """
    from deploy.vault import set as vault_set

    current_auth = get_auth_mode()
    current_is_vertex = current_auth["vertex_ai"]

    if target == "vertex" and current_is_vertex:
        return {"status": "no_change", "message": "Already in Google One / Vertex AI mode."}
    if target == "api_key" and not current_is_vertex:
        return {"status": "no_change", "message": "Already in Direct API key mode."}

    if target == "vertex":
        # Switching TO Vertex/Google One
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            return {
                "status": "error",
                "message": "Cannot switch to Vertex AI: GOOGLE_CLOUD_PROJECT is not configured. "
                           "Ask the user to provide their GCP project ID first "
                           "(via /init or configure_integration).",
            }
        vault_set("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        return {
            "status": "success",
            "message": f"Switched to Google One / Vertex AI mode (project: {project}). "
                       "Change takes effect on the next message.",
        }

    if target == "api_key":
        # Switching TO API key mode
        has_key = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
        if not has_key:
            return {
                "status": "error",
                "message": "Cannot switch to API key mode: GOOGLE_API_KEY is not configured. "
                           "Ask the user to provide their API key first "
                           "(via /init or configure_integration).",
            }
        vault_set("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
        return {
            "status": "success",
            "message": "Switched to Direct API key mode. Change takes effect on the next message.",
        }
