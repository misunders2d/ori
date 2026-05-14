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
    # Heavy-reasoning / final-response components stay on Flash. These
    # do judgment calls, codegen, SQL synthesis, or research — Lite
    # degrades them noticeably.
    "CoordinatorAgent":         "google/gemini-3-flash-preview",
    "KnowledgeAgent":           "google/gemini-3-flash-preview",
    "AmazonDataAnalystAgent":   "google/gemini-3-flash-preview",
    "BigQueryAgent":            "google/gemini-3-flash-preview",
    # YouTube transcript summarisation is one-shot, bulk input, short
    # output. Lite handles it fine (~3× cheaper than Flash). Quality
    # difference negligible on long transcripts. Hot-swap back if a
    # specific transcript hits a Lite quality wall.
    "youtube_summarizer":       "google/gemini-3.1-flash-lite",

    # Self-evolution = code-modifying-code. Want strong tool-use + code
    # reasoning, but Opus 4.7 burned ~$3 on a single trivial model-name
    # change (2026-05-12 incident: 11+ turns × ~35K input tokens × Opus
    # input rate $15/M, no prompt-caching on the LiteLLM path). Sonnet 4.6
    # is ~5× cheaper input, 5× cheaper output, near-equal tool-calling
    # quality on code tasks. Routed via OpenRouter (single
    # OPENROUTER_API_KEY in vault). Escalate to Opus manually with
    # `/models set DeveloperAgent openrouter/anthropic/claude-opus-4.7`
    # ONLY for hard multi-file refactors where Sonnet struggles. Hot-swap
    # wired (see docs/HOT_SWAP.md); no restart needed.
    "DeveloperAgent":           "openrouter/anthropic/claude-sonnet-4.6",

    # AmazonHeadAgent kept on Flash — it's the routing brain for every
    # Amazon query (decides which Amazon sub-agent handles the request).
    # Lite produced bad routing on 2026-05-11 (mis-routed "plot a
    # chart" to a giphy fetch, plus general latency from retries/
    # ping-pongs). The 50% savings aren't worth the user-visible
    # quality drop on the hottest path.
    "AmazonHeadAgent":          "google/gemini-3-flash-preview",

    # CRUD / tool-execution leaves default to Flash-Lite (~50% the
    # per-token price of Flash). They run AFTER the head has decided
    # who handles the request, so a weaker model only affects the
    # mechanical execution under tight prompts + plan_step_enforcer
    # (which hard-blocks rogue tool calls anyway). Hot-swap any of
    # these back with `/models set <Agent> google/gemini-3-flash-preview`
    # (no restart needed) if quality drops on a specific one.
    "AmazonAgent":              "google/gemini-3.1-flash-lite",
    "AmazonMemoryAgent":        "google/gemini-3.1-flash-lite",
    "AmazonWorkspaceAgent":     "google/gemini-3.1-flash-lite",
    "ClickUpAgent":             "google/gemini-3.1-flash-lite",
    "google_search":            "google/gemini-3.1-flash-lite",

    # Compaction + embedding — already cheap.
    "summarizer":               "google/gemini-3.1-flash-lite",
    "session_summarizer":       "google/gemini-3.1-flash-lite",
    "embedding":                "google/gemini-embedding-001",
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
        has_openrouter_key = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
        # Prompt caching is wired on the OpenRouter path only (top-level
        # `extra_body.cache_control` forwarded by LiteLLM's openrouter
        # handler). LiteLLM's anthropic-direct path needs per-message
        # cache_control injection — not yet wired. To avoid silently
        # losing ~90% of input-cost savings, redirect Claude models
        # through OpenRouter when OPENROUTER_API_KEY is available. Falls
        # back to anthropic-direct if only ANTHROPIC_API_KEY is set.
        is_claude = "claude" in model_name.lower()
        if is_claude and has_openrouter_key:
            logger.info(
                "anthropic/%s routed via OpenRouter for prompt-caching "
                "(billed to OPENROUTER_API_KEY, not ANTHROPIC_API_KEY)",
                model_name,
            )
            return _build_model("openrouter", f"anthropic/{model_name}", **kwargs)
        if has_anthropic_key:
            if is_claude:
                logger.warning(
                    "anthropic/%s using direct API (no OpenRouter key) — "
                    "prompt-caching NOT wired on this path; expect ~5-10× "
                    "higher input cost vs cached. Set OPENROUTER_API_KEY "
                    "or migrate caching to anthropic-direct path.",
                    model_name,
                )
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
        lite_kwargs = _without_litellm_unsupported_kwargs(kwargs)
        # Anthropic auto-prompt-caching via OpenRouter: a top-level
        # `cache_control: {type: ephemeral}` field tells OpenRouter to
        # mark the last cacheable block, which caches the entire prefix
        # (system + tools + history). Anthropic charges 0.1× input on
        # cache reads, 1.25× input on cache writes. Cuts cost on the
        # static schema/system portion (~25-30K tokens for DeveloperAgent)
        # by ~90% on every cache hit (5-min TTL). Min cacheable for
        # Sonnet 4.6 = 2048 tokens, well below our usage.
        # LiteLLM's openrouter handler forwards `extra_body` keys into
        # the request body root; only enabled for Claude (Gemini caches
        # automatically without a marker, others don't support it).
        if "claude" in model_name.lower() or model_name.lower().startswith("anthropic/"):
            extra = lite_kwargs.get("extra_body") or {}
            extra.setdefault("cache_control", {"type": "ephemeral"})
            lite_kwargs["extra_body"] = extra
        # LiteLLM routes correctly with "openrouter/" prefix
        return LiteLlm(
            model=f"openrouter/{model_name}",
            **lite_kwargs,
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

    When falling back, the bad override is ALSO cleared from
    ``model_config.json`` and ``os.environ``. Otherwise ``state_setter``
    would compare the persisted override against the live (default-built)
    model on every turn and emit a permanent "Cross-provider hot-swap
    requested" warning that no restart can reconcile — the override
    keeps re-hydrating from disk, the build keeps failing, and the
    warning keeps firing. Auto-repair eliminates that loop (the user
    sees the fallback warning ONCE in the boot log; subsequent runs
    are clean).

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
                "get_model(%s): override %s unusable (%s) — falling back to default %s "
                "and clearing the bad override (model_config.json + os.environ).",
                component, model_str, e, default_str,
            )
            # Auto-repair: drop the unusable override so state_setter and
            # the next get_model() call both see the same default. Wrapped
            # in best-effort try/except — repair failure must not block
            # boot.
            try:
                from app.app_utils.model_config import unset_assignment

                unset_assignment(component)
            except Exception as repair_err:
                logger.warning(
                    "get_model(%s): auto-repair failed to clear override (%s) — "
                    "the cross-provider warning will likely keep firing until "
                    "the override is removed manually.",
                    component,
                    repair_err,
                )
            os.environ.pop(f"MODEL_{component.upper()}", None)

            default_provider, default_name = _parse_model_str(default_str)
            return _build_model(default_provider, default_name, **kwargs)
        raise


def _provider_key_available(provider: str) -> bool:
    """Return True if the env keys required to build a model on this
    provider are currently present.

    Guards ``set_model`` against persisting an override the runtime
    can't honour. Without this check, a missed key turns into a
    permanent "cross-provider hot-swap" warning loop (the override
    keeps re-hydrating, the build keeps falling back to default,
    state_setter keeps flagging the mismatch).
    """
    if provider == "google":
        # Either direct API key OR Vertex AI ADC mode works.
        return bool(os.environ.get("GOOGLE_API_KEY", "").strip()) or _is_vertex_mode()
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()) or _is_vertex_mode()
    if provider == "openrouter":
        return bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    return False


def set_model(component: str, model_str: str) -> None:
    """Persist a model assignment to model_config.json (not the vault).

    Always stores the canonical `provider/model` form, even if the caller
    passed a legacy Google-API `models/X` string or a bare name.

    Refuses to persist an override whose provider has no usable API key
    in the current environment — silently writing a key-less override
    would feed the auto-repair loop in ``get_model()`` and leave the
    user wondering why nothing changed.
    """
    if component not in VALID_COMPONENTS:
        raise ValueError(f"Invalid component: '{component}'. Valid: {sorted(VALID_COMPONENTS)}")

    provider, model_name = _parse_model_str(model_str)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider '{provider}' in '{model_str}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    if not _provider_key_available(provider):
        raise ValueError(
            f"Cannot set {component!r} to {model_str!r}: provider {provider!r} "
            f"has no usable API key in the environment. Configure the key first "
            f"({provider}=GOOGLE_API_KEY / GOOGLE_GENAI_USE_VERTEXAI / "
            f"ANTHROPIC_API_KEY / OPENROUTER_API_KEY as applicable), then retry."
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


_MODELS_USAGE = (
    "Usage:\n"
    "  `/models` — list all agent model assignments\n"
    "  `/models <Component> <provider/model>` — set one component's model "
    "(e.g. `/models DeveloperAgent openrouter/deepseek/deepseek-v4-flash`)\n"
    "  `/models set <Component> <provider/model>` — explicit alias\n"
    "  `/models default` — reset ALL agents to their default models\n"
    "  `/models default <Component>` — reset one component to its default"
)


def dispatch_models_command(text: str) -> str:
    """Parse a ``/models …`` slash command and return the reply text.

    Pure-data dispatcher: takes the raw user message string, returns
    the response string. Pollers do the auth check + send the reply.
    Identical Slack and Telegram behaviour live here so the two
    surfaces can't drift.

    Grammar:

      * ``/models``                              → list all assignments
      * ``/models default``                      → reset every override
      * ``/models default <Component>``          → reset one
      * ``/models <Component> <provider/model>`` → set one (2026-05-14)
      * ``/models set <Component> <prov/model>`` → explicit alias of the
        set form

    Cross-provider swaps (e.g. anthropic → openrouter) require a bot
    restart because the agent's ``canonical_model`` instance is built
    once at boot. Same-provider swaps (model string within the same
    provider — most ``openrouter/X/Y`` → ``openrouter/A/B`` cases) take
    effect on the next LLM call. The reply text flags the difference
    explicitly so the operator knows whether to restart.
    """
    parts = text.strip().split()

    if len(parts) == 1:  # /models
        return "```\n" + format_model_assignments(markdown=False) + "\n```"

    if len(parts) >= 2 and parts[1].lower() == "default":
        if len(parts) == 2:
            cleared = reset_all_models()
            msg = (
                f"Reset {len(cleared)} model override(s) to defaults."
                if cleared
                else "No overrides to reset — everything is already on defaults."
            )
            return msg + "\n\n```\n" + format_model_assignments(markdown=False) + "\n```"
        component = parts[2]
        if component not in VALID_COMPONENTS:
            return (
                f"Unknown component '{component}'. "
                f"Valid: {', '.join(sorted(VALID_COMPONENTS))}"
            )
        cleared = reset_model(component)
        return (
            f"Reset {component} to default."
            if cleared
            else f"{component} was already on its default — nothing to clear."
        )

    # SET path. Supports both shorthand and explicit forms:
    #   /models <Component> <model_str>
    #   /models set <Component> <model_str>
    if len(parts) == 3 and parts[1] in VALID_COMPONENTS:
        component, new_model = parts[1], parts[2]
    elif len(parts) == 4 and parts[1].lower() == "set":
        component, new_model = parts[2], parts[3]
    else:
        return _MODELS_USAGE

    if component not in VALID_COMPONENTS:
        return (
            f"Unknown component '{component}'. "
            f"Valid: {', '.join(sorted(VALID_COMPONENTS))}"
        )

    # Capture current resolved string so we can flag cross-provider
    # swaps (require restart) vs same-provider swaps (take effect
    # next LLM call).
    try:
        prev = get_model_string(component)
        prev_provider, _ = _parse_model_str(prev) if prev else ("", "")
    except Exception:
        prev_provider = ""

    try:
        set_model(component, new_model)
    except ValueError as e:
        # set_model rejects unknown providers, invalid component
        # names, and missing API keys. Surface verbatim so the
        # operator knows exactly what to fix.
        return f"Failed to set {component}: {e}"

    try:
        new_provider, _ = _parse_model_str(new_model)
    except Exception:
        new_provider = ""

    restart_note = ""
    if prev_provider and new_provider and prev_provider != new_provider:
        restart_note = (
            f"\n\n⚠️ Cross-provider swap ({prev_provider} → {new_provider}). "
            "The agent's model instance is locked at boot — "
            "`update_self` (restart) for this to take effect."
        )
    else:
        restart_note = "\n\nLive — next LLM call to this agent uses the new model."

    return (
        f"`{component}` → `{new_model}`{restart_note}\n\n"
        "```\n" + format_model_assignments(markdown=False) + "\n```"
    )


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
