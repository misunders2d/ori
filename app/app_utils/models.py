"""Centralized model registry and factory.

Every agent/component resolves its model through this module.
Call sites never import provider-specific classes (e.g. Gemini) directly.

Format: "provider/model_name"  (e.g. "google/gemini-3-flash-preview")
Bare strings without '/' default to the "google" provider.

Auth modes:
  - API key: GOOGLE_API_KEY / ANTHROPIC_API_KEY (direct provider APIs)
  - Vertex AI ADC: gcloud auth application-default login + GOOGLE_GENAI_USE_VERTEXAI=TRUE
    (covers both Gemini and Claude via Vertex AI Model Garden — no per-provider API keys needed)
  - Service account: GOOGLE_APPLICATION_CREDENTIALS + GOOGLE_GENAI_USE_VERTEXAI=TRUE
"""

import logging
import os

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default model assignments per component
# ---------------------------------------------------------------------------
MODEL_DEFAULTS: dict[str, str] = {
    "CoordinatorAgent":   "google/gemini-3-flash-preview",
    "DeveloperAgent":     "google/gemini-3-flash-preview",
    "KnowledgeAgent":     "google/gemini-3-flash-preview",
    "google_search":      "google/gemini-3-flash-preview",
    "summarizer":         "google/gemini-3.1-flash-lite-preview",
    "session_summarizer": "google/gemini-3.1-flash-lite-preview",
    "channel_summarizer": "google/gemini-3.1-flash-lite-preview",
    "embedding":          "google/gemini-embedding-001",
}

VALID_COMPONENTS = frozenset(MODEL_DEFAULTS.keys())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _is_vertex_mode() -> bool:
    """Check if running in Vertex AI mode (ADC/service account auth)."""
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"


def get_auth_mode() -> dict:
    """Return the current authentication mode and status."""
    vertex = _is_vertex_mode()
    mode = {
        "vertex_ai": vertex,
        "google_cloud_project": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        "google_cloud_location": os.environ.get("GOOGLE_CLOUD_LOCATION", ""),
    }
    if vertex:
        mode["auth_method"] = "Vertex AI (ADC / service account)"
        mode["google_api_key"] = False
        mode["anthropic_api_key"] = False
        svc_acct = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        mode["service_account"] = bool(svc_acct)
    else:
        mode["auth_method"] = "Direct API keys"
        mode["google_api_key"] = bool(os.environ.get("GOOGLE_API_KEY"))
        mode["anthropic_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return mode


def _parse_model_str(model_str: str) -> tuple[str, str]:
    """Split "provider/model_name" into (provider, model_name).

    Bare strings (no '/') default to the "google" provider.
    Strips quotes that leak from .env files (Docker --env-file doesn't strip them).
    """
    model_str = model_str.strip().strip("\"'")
    if "/" in model_str:
        provider, model_name = model_str.split("/", 1)
        return provider.lower().strip(), model_name.strip()
    return "google", model_str


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _default_retry_options():
    """Default retry config for all LLM calls — handles 429s with exponential backoff."""
    from google.genai import types
    return types.HttpRetryOptions(
        attempts=4,
        initialDelay=2.0,
        maxDelay=60.0,
        expBase=2.0,
        jitter=1.0,
        httpStatusCodes=[429, 503, 529],
    )


def _build_model(provider: str, model_name: str, **kwargs):
    """Construct a provider-specific LLM model object.

    Returns a BaseLlm instance (e.g. Gemini, Claude, LiteLlm).
    Raises ValueError for unsupported providers.

    All models get default retry options (4 attempts, exponential backoff)
    unless explicitly overridden via kwargs.

    In Vertex AI mode, both Google and Anthropic models use ADC credentials.
    In API key mode, Google uses GOOGLE_API_KEY and Anthropic uses ANTHROPIC_API_KEY via LiteLlm.
    """
    # Apply default retry options if not explicitly provided
    if "retry_options" not in kwargs:
        kwargs["retry_options"] = _default_retry_options()

    if provider == "google":
        from google.adk.models import Gemini
        return Gemini(model=model_name, **kwargs)
    if provider == "anthropic":
        # Prefer direct Anthropic API (via LiteLlm) when API key is available.
        # Only use Vertex AI Claude class when explicitly in Vertex mode AND no direct key.
        has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        if has_anthropic_key:
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(model=f"anthropic/{model_name}", **kwargs)
        elif _is_vertex_mode():
            # Vertex AI Model Garden — requires Claude to be enabled in the project
            from google.adk.models.anthropic_llm import Claude
            return Claude(model=model_name, **kwargs)
        else:
            raise ValueError(
                "Anthropic models require either ANTHROPIC_API_KEY or Vertex AI mode "
                "with Claude enabled in the Model Garden."
            )
    raise ValueError(
        f"Unsupported model provider: '{provider}'. Currently supported: google, anthropic"
    )


SUPPORTED_PROVIDERS = frozenset({"google", "anthropic"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_model_string(component: str) -> str:
    """Return the effective "provider/model" string for a component."""
    env_key = f"MODEL_{component.upper()}"
    return os.environ.get(env_key, MODEL_DEFAULTS.get(component, ""))


def get_model_name(component: str) -> str:
    """Return just the model name (no provider prefix) for direct API calls."""
    _, model_name = _parse_model_str(get_model_string(component))
    return model_name


def get_model(component: str, **kwargs):
    """Return a fully constructed model object for the given component.

    Resolves env override -> default, parses provider, builds model.
    Extra kwargs (e.g. retry_options) are forwarded to the provider constructor.
    """
    model_str = get_model_string(component)
    if not model_str:
        raise ValueError(f"Unknown model component: '{component}'")
    provider, model_name = _parse_model_str(model_str)
    return _build_model(provider, model_name, **kwargs)


def set_model(component: str, model_str: str) -> None:
    """Persist a model assignment to runtime config."""
    if component not in VALID_COMPONENTS:
        raise ValueError(f"Invalid component: '{component}'. Valid: {sorted(VALID_COMPONENTS)}")

    from app.app_utils.runtime_config import set_config
    env_key = f"MODEL_{component.upper()}"
    set_config(env_key, model_str)
    logger.info("Model for %s set to %s", component, model_str)


def get_all_assignments() -> dict[str, str]:
    """Return {component: "provider/model"} for all components."""
    return {c: get_model_string(c) for c in MODEL_DEFAULTS}


# ---------------------------------------------------------------------------
# Live provider validation & discovery
# ---------------------------------------------------------------------------

async def validate_model(model_str: str) -> dict | None:
    """Validate that a model exists on the provider's API.

    Returns a dict with model info on success, None on failure.
    """
    provider, model_name = _parse_model_str(model_str)

    if provider == "google":
        from google import genai
        client = genai.Client()
        try:
            model = await client.aio.models.get(model=model_name)
            return {
                "name": model.name,
                "display_name": getattr(model, "display_name", ""),
                "description": getattr(model, "description", ""),
                "input_token_limit": getattr(model, "input_token_limit", None),
                "output_token_limit": getattr(model, "output_token_limit", None),
                "supported_actions": getattr(model, "supported_actions", []),
            }
        except Exception as e:
            logger.warning("Model validation failed for '%s': %s", model_str, e)
            return None

    if provider == "anthropic":
        if _is_vertex_mode():
            # In Vertex mode, Claude models are accessed via Google Cloud.
            # Validate by checking the model is in the ADK registry.
            try:
                from google.adk.models.anthropic_llm import Claude
                supported = Claude.supported_models()
                import re
                if any(re.fullmatch(p, model_name) for p in supported):
                    return {"name": model_name, "display_name": model_name, "via": "vertex_ai"}
            except Exception as e:
                logger.warning("Vertex Claude validation failed for '%s': %s", model_str, e)
            return None
        else:
            import anthropic
            client = anthropic.AsyncAnthropic()
            try:
                model = await client.models.retrieve(model_name)
                return {
                    "name": model.id,
                    "display_name": getattr(model, "display_name", ""),
                    "input_token_limit": getattr(model, "max_input_tokens", None),
                    "output_token_limit": getattr(model, "max_tokens", None),
                }
            except Exception as e:
                logger.warning("Model validation failed for '%s': %s", model_str, e)
                return None

    logger.warning("Cannot validate model for unsupported provider: %s", provider)
    return None


async def list_provider_models(provider: str = "google", filter_str: str = "") -> list[dict]:
    """List available models from a provider's API.

    Returns a list of dicts with model metadata.
    """
    results = []

    if provider == "google":
        from google import genai
        client = genai.Client()
        try:
            async for model in await client.aio.models.list():
                name = getattr(model, "name", "")
                display = getattr(model, "display_name", "")
                if filter_str and filter_str.lower() not in (name + display).lower():
                    continue
                results.append({
                    "name": name,
                    "display_name": display,
                    "input_token_limit": getattr(model, "input_token_limit", None),
                    "output_token_limit": getattr(model, "output_token_limit", None),
                    "supported_actions": getattr(model, "supported_actions", []),
                })
        except Exception as e:
            logger.error("Failed to list models from %s: %s", provider, e)
            return [{"error": str(e)}]
    elif provider == "anthropic":
        if _is_vertex_mode():
            # In Vertex mode, list known Claude model patterns from ADK registry
            try:
                from google.adk.models.anthropic_llm import Claude
                patterns = Claude.supported_models()
                results.append({
                    "note": "Vertex AI mode — Claude models matched by patterns",
                    "supported_patterns": patterns,
                    "via": "vertex_ai",
                })
            except Exception as e:
                logger.error("Failed to get Vertex Claude models: %s", e)
                return [{"error": str(e)}]
        else:
            import anthropic
            client = anthropic.AsyncAnthropic()
            try:
                page = await client.models.list(limit=100)
                for model in page.data:
                    name = model.id
                    display = getattr(model, "display_name", "")
                    if filter_str and filter_str.lower() not in (name + display).lower():
                        continue
                    results.append({
                        "name": name,
                        "display_name": display,
                        "input_token_limit": getattr(model, "max_input_tokens", None),
                        "output_token_limit": getattr(model, "max_tokens", None),
                    })
            except Exception as e:
                logger.error("Failed to list models from %s: %s", provider, e)
                return [{"error": str(e)}]
    else:
        return [{"error": f"Unsupported provider: {provider}. Supported: {sorted(SUPPORTED_PROVIDERS)}"}]

    return results
