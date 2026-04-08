"""Tools for runtime model discovery and hot-swapping."""

import logging
from typing import Annotated

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
