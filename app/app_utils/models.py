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
    "CoordinatorAgent":         "google/gemini-3-flash-preview",
    # Self-evolution is irreversible code-modifying-code — pay for the
    # safer answer by default. Opus 4.7 leads MCP-Atlas (multi-turn
    # tool-calling) and cut multi-step task abandonment ~60% vs 4.6 in
    # Anthropic's internal data. Routed via OpenRouter so a single
    # OPENROUTER_API_KEY in vault covers it (no separate Anthropic key
    # or Vertex Model Garden setup required). Hot-swap is wired (see
    # docs/HOT_SWAP.md) — switch back to Flash anytime with `/models set`.
    "DeveloperAgent":           "openrouter/anthropic/claude-opus-4.7",
    "KnowledgeAgent":           "google/gemini-3-flash-preview",
    "ClickUpAgent":             "google/gemini-3-flash-preview",
    "AmazonHeadAgent":          "google/gemini-3-flash-preview",
    "AmazonAgent":              "google/gemini-3-flash-preview",
    "AmazonMemoryAgent":        "google/gemini-3-flash-preview",
    "AmazonWorkspaceAgent":     "google/gemini-3-flash-preview",
    "AmazonDataAnalystAgent":   "google/gemini-3-flash-preview",
    "BigQueryAgent":            "google/gemini-3-flash-preview",
    "google_search":            "google/gemini-3-flash-preview",
    "summarizer":               "google/gemini-3.1-flash-lite-preview",
    "session_summarizer":       "google/gemini-3.1-flash-lite-preview",
    "embedding":                "google/gemini-embedding-001",
    "youtube_summarizer":       "google/gemini-3-flash-preview",
}

VALID_COMPONENTS = frozenset(MODEL_DEFAULTS.keys())

PROVIDER_API_KEYS = {
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

SUPPORTED_PROVIDERS = frozenset(PROVIDER_API_KEYS.keys())


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
        mode["openrouter_api_key"] = False
        svc_acct = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        mode["service_account"] = bool(svc_acct)
    else:
        mode["auth_method"] = "Direct API keys"
        mode["google_api_key"] = bool(os.environ.get("GOOGLE_API_KEY"))
        mode["anthropic_api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
        mode["openrouter_api_key"] = bool(os.environ.get("OPENROUTER_API_KEY"))
    return mode


def _parse_model_str(model_str: str) -> tuple[str, str]:
    """Split "provider/model_name" into (provider, model_name).

    Bare strings (no '/') default to the "google" provider.
    Handles Google API format "models/gemini-..." (strip prefix, default to google).
    Strips quotes that leak from .env files (Docker --env-file doesn't strip them).
    """
    model_str = model_str.strip().strip("\"'")
    # Google API returns "models/model-name" — not a provider prefix
    if model_str.startswith("models/"):
        return "google", model_str.removeprefix("models/")
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


def _without_litellm_unsupported_kwargs(kwargs: dict) -> dict:
    """Drop google-genai-only kwargs before constructing LiteLlm."""
    filtered = dict(kwargs)
    filtered.pop("retry_options", None)
    return filtered


def _build_model(provider: str, model_name: str, **kwargs):
    """Construct a provider-specific LLM model object.

    Returns a BaseLlm instance (e.g. Gemini, Claude, LiteLlm).
    Raises ValueError for unsupported providers.

    Google/Vertex models get default retry options (4 attempts, exponential
    backoff) unless explicitly overridden via kwargs. LiteLLM providers do not
    accept google.genai HttpRetryOptions; passing one breaks JSON serialization.

    In Vertex AI mode, both Google and Anthropic models use ADC credentials.
    In API key mode, Google uses GOOGLE_API_KEY and Anthropic uses ANTHROPIC_API_KEY via LiteLlm.
    """
    if provider == "google":
        if "retry_options" not in kwargs:
            kwargs["retry_options"] = _default_retry_options()
        from google.adk.models import Gemini
        return Gemini(model=model_name, **kwargs)
    if provider == "anthropic":
        # Prefer direct Anthropic API (via LiteLlm) when API key is available.
        # Only use Vertex AI Claude class when explicitly in Vertex mode AND no direct key.
        has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        if has_anthropic_key:
            from google.adk.models.lite_llm import LiteLlm
            return LiteLlm(
                model=f"anthropic/{model_name}",
                **_without_litellm_unsupported_kwargs(kwargs),
            )
        elif _is_vertex_mode():
            if "retry_options" not in kwargs:
                kwargs["retry_options"] = _default_retry_options()
            # Vertex AI Model Garden — requires Claude to be enabled in the project
            from google.adk.models.anthropic_llm import Claude
            return Claude(model=model_name, **kwargs)
        else:
            raise ValueError(
                "Anthropic models require either ANTHROPIC_API_KEY or Vertex AI mode "
                "with Claude enabled in the Model Garden."
            )
    if provider == "openrouter":
        has_openrouter_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
        if not has_openrouter_key:
            raise ValueError("OpenRouter models require OPENROUTER_API_KEY.")
        from google.adk.models.lite_llm import LiteLlm
        # LiteLLM routes correctly with "openrouter/" prefix
        return LiteLlm(
            model=f"openrouter/{model_name}",
            **_without_litellm_unsupported_kwargs(kwargs),
        )

    raise ValueError(
        f"Unsupported model provider: '{provider}'. Currently supported: google, anthropic, openrouter"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_model_string(component: str) -> str:
    """Return the effective "provider/model" string for a component.

    Resolution order: os.environ (hot-swap) -> model_config.json -> defaults.
    """
    env_key = f"MODEL_{component.upper()}"
    env_val = os.environ.get(env_key, "")
    if env_val:
        return env_val

    from app.app_utils.model_config import get_assignment
    config_val = get_assignment(component)
    if config_val:
        return config_val

    return MODEL_DEFAULTS.get(component, "")


def get_model_name(component: str) -> str:
    """Return just the model name (no provider prefix) for direct API calls."""
    _, model_name = _parse_model_str(get_model_string(component))
    return model_name


def get_model(component: str, **kwargs):
    """Return a fully constructed model object for the given component.

    Resolves env override -> default, parses provider, builds model.
    Extra kwargs (e.g. retry_options) are forwarded to the provider constructor.

    Robustness: if the resolved model requires a provider API key that's
    missing in the environment, fall back to the component's
    MODEL_DEFAULTS value and log a warning. This keeps the bot bootable
    after a key rotation or vault hiccup — the override silently steps
    aside instead of bricking import-time agent construction.

    If the default ALSO fails to build, the error propagates (no key for
    *any* model means the agent can't run anyway).
    """
    model_str = get_model_string(component)
    if not model_str:
        raise ValueError(f"Unknown model component: '{component}'")
    provider, model_name = _parse_model_str(model_str)
    try:
        return _build_model(provider, model_name, **kwargs)
    except ValueError as e:
        default_str = MODEL_DEFAULTS.get(component, "")
        if default_str and default_str != model_str:
            logger.warning(
                "get_model(%s): override %s unusable (%s) — falling back to default %s",
                component, model_str, e, default_str,
            )
            default_provider, default_name = _parse_model_str(default_str)
            return _build_model(default_provider, default_name, **kwargs)
        raise


def set_model(component: str, model_str: str) -> None:
    """Persist a model assignment to model_config.json (not the vault).

    Always stores the canonical `provider/model` form, even if the caller
    passed a legacy Google-API `models/X` string or a bare name.
    """
    if component not in VALID_COMPONENTS:
        raise ValueError(f"Invalid component: '{component}'. Valid: {sorted(VALID_COMPONENTS)}")

    provider, model_name = _parse_model_str(model_str)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}' in '{model_str}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    normalized = f"{provider}/{model_name}"

    from app.app_utils.model_config import set_assignment
    set_assignment(component, normalized)
    logger.info("Model for %s set to %s", component, normalized)


def reset_model(component: str) -> bool:
    """Clear a single component's override, reverting it to its default.

    Returns True if something was cleared, False if it was already at default.
    """
    if component not in VALID_COMPONENTS:
        raise ValueError(f"Invalid component: '{component}'. Valid: {sorted(VALID_COMPONENTS)}")
    from app.app_utils.model_config import unset_assignment
    cleared = unset_assignment(component)
    if cleared:
        logger.info("Model for %s reset to default (%s)", component, MODEL_DEFAULTS.get(component, ""))
    return cleared


def reset_all_models() -> dict[str, str]:
    """Clear every persisted model override at once. Returns the dict of what was cleared."""
    from app.app_utils.model_config import clear_all_assignments
    return clear_all_assignments()


def hydrate_model_env() -> int:
    """Seed os.environ[MODEL_*] from data/model_config.json before any agent imports.

    `set_assignment` writes both to disk and to os.environ, so within a single
    process the hot-swap is immediate. After a restart, however, os.environ
    starts empty. Components that resolve their model **lazily** through
    `get_model_string` re-read the file on every call and still see the
    override — but components that capture the model at module-import time
    (Agent(model=...) at top level) only ever see whatever os.environ
    contains at the moment the module loaded.

    This function rehydrates os.environ from the persisted assignments
    before any agent module gets imported, so import-time captures match
    the user's last `/models set`.

    Call this once, very early in `run_bot.py`. Returns the count of
    applied overrides (0 means no persisted assignments).
    """
    from app.app_utils.model_config import get_all_assignments as _persisted_assignments
    try:
        assignments = _persisted_assignments()
    except Exception as e:
        logger.warning("hydrate_model_env: read failed (%s) — using defaults", e)
        return 0

    count = 0
    for component, model_str in assignments.items():
        if component not in VALID_COMPONENTS:
            continue
        if not isinstance(model_str, str) or not model_str.strip():
            continue
        os.environ[f"MODEL_{component.upper()}"] = model_str.strip()
        count += 1

    if count:
        logger.info("hydrate_model_env: applied %d persisted model assignments", count)
    return count


def get_all_assignments() -> dict[str, str]:
    """Return {component: "provider/model"} for all components (resolved)."""
    return {c: get_model_string(c) for c in MODEL_DEFAULTS}


def list_models() -> dict:
    """Return a fully-resolved snapshot of every component's current model assignment.

    Deterministic — no LLM involved. Safe to call from slash commands, CLIs, or scripts.

    Returns:
        dict: {
            "auth_mode": <auth info dict from get_auth_mode()>,
            "assignments": {component: "provider/model", ...},
            "defaults":    {component: "provider/model", ...},
            "overrides":   {component: "provider/model", ...},  # only components differing from defaults
        }
    """
    resolved = get_all_assignments()
    overrides = {
        comp: model for comp, model in resolved.items()
        if model and model != MODEL_DEFAULTS.get(comp)
    }
    return {
        "auth_mode": get_auth_mode(),
        "assignments": resolved,
        "defaults": dict(MODEL_DEFAULTS),
        "overrides": overrides,
    }


def format_model_assignments(markdown: bool = False) -> str:
    """Render list_models() as a human-readable string.

    Args:
        markdown: If True, wrap model names in backticks for markdown renderers.
    """
    snapshot = list_models()
    assignments = snapshot["assignments"]
    overrides = snapshot["overrides"]
    auth = snapshot["auth_mode"]

    width = max((len(c) for c in assignments), default=0)
    lines = ["Model assignments:"]
    for comp in sorted(assignments):
        model = assignments[comp] or "(unset)"
        marker = " *" if comp in overrides else "  "
        if markdown:
            lines.append(f"{marker}`{comp.ljust(width)}` → `{model}`")
        else:
            lines.append(f"{marker}{comp.ljust(width)}  →  {model}")

    if overrides:
        lines.append("")
        lines.append("(* = overrides the default)")

    lines.append("")
    lines.append(f"Auth mode: {auth.get('auth_method', 'unknown')}")
    if auth.get("vertex_ai"):
        proj = auth.get("google_cloud_project") or "(no project)"
        loc = auth.get("google_cloud_location") or "(no location)"
        lines.append(f"  GCP project:  {proj}")
        lines.append(f"  GCP location: {loc}")
        if auth.get("service_account"):
            lines.append("  Service account: configured")
    else:
        lines.append(f"  GOOGLE_API_KEY:    {'set' if auth.get('google_api_key') else 'missing'}")
        lines.append(f"  ANTHROPIC_API_KEY: {'set' if auth.get('anthropic_api_key') else 'missing'}")
        lines.append(f"  OPENROUTER_API_KEY: {'set' if auth.get('openrouter_api_key') else 'missing'}")

    return "\n".join(lines)


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

    if provider == "openrouter":
        has_openrouter_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
        if not has_openrouter_key:
            return None
        # Basic verification: key exists, assume model name is valid (too many to list)
        return {"name": model_name, "display_name": model_name, "via": "openrouter"}

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
