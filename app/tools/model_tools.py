"""Tools for runtime model discovery and hot-swapping."""

import logging
from typing import Annotated

from google.adk.tools.tool_context import ToolContext

from app.app_utils.models import (
    VALID_COMPONENTS,
    get_all_assignments,
    get_model_string,
    set_model,
    validate_model,
    list_provider_models,
    _parse_model_str,
)

logger = logging.getLogger(__name__)


async def list_available_models(
    provider: Annotated[str, "Model provider to query (default 'google')"] = "google",
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
    }


async def set_agent_model(
    component_name: Annotated[str, "Target component (e.g. 'DeveloperAgent', 'channel_summarizer')"],
    model_name: Annotated[str, "Model identifier, e.g. 'google/gemini-2.5-pro' or bare 'gemini-2.5-pro'"],
    tool_context: ToolContext = None,
) -> dict:
    """Switch the model for a specific agent or component. Validates against the live API."""
    if component_name not in VALID_COMPONENTS:
        return {
            "status": "error",
            "message": f"Unknown component '{component_name}'. Valid: {sorted(VALID_COMPONENTS)}",
        }

    # Normalize: bare name -> "google/name"
    if "/" not in model_name:
        model_name = f"google/{model_name}"

    # Validate against live API
    info = await validate_model(model_name)
    if info is None:
        return {
            "status": "error",
            "message": f"Model '{model_name}' not found or unreachable on the provider API. Check spelling.",
        }

    old_model = get_model_string(component_name)

    # Persist to env + .env
    set_model(component_name, model_name)

    # Write to session state for immediate hot-swap (picked up by before_model_callback)
    if tool_context:
        tool_context.state[f"model:{component_name}"] = model_name

    return {
        "status": "success",
        "message": f"Model for {component_name} changed: {old_model} -> {model_name}",
        "model_info": info,
    }
