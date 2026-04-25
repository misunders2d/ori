"""LLM model resolution + provider registry.

Provides:
- `PROVIDER_REGISTRY`: maps a provider prefix to a factory `(rest_of_str, opts) -> BaseLlm`.
  Adding a new provider is one entry here. LiteLlm covers most providers
  (gemini, anthropic, openai, mistral, cohere, groq, ollama, deepseek, …)
  via a single class — model strings like `litellm/<provider>/<model>`.
  Native classes (Gemini, Claude) are included for the cases where ADK's
  native integration matters — currently only google_search-binding agents.
- `MODEL_DEFAULTS`: per-component default model strings.
- `resolve_model(component, state, **opts) -> BaseLlm`: factory entry point.

Hot-swap: `state.model[component]` overrides the default. Same-provider
swap = same factory called with a different model string. Cross-provider
swap with a LiteLlm-routed component = also just a model string change
(LiteLlm's class is the same; the litellm library routes internally).
A pinned component (e.g. google_search bound to native Gemini) ignores
state overrides — that's enforced in `ModelConfigPlugin`, not here.

Adding a new native provider: drop a factory entry. Most additions are a
single line — `litellm` already routes everywhere via the litellm library.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Callable

from google.adk.models import BaseLlm, Claude, Gemini, LiteLlm

if TYPE_CHECKING:
    from app.state import OriSessionState


# Each factory takes (remainder, opts) and returns a BaseLlm instance.
# `remainder` is the model string with the provider prefix stripped — so
# "litellm/gemini/gemini-2.5-flash".partition("/") yields
# ("litellm", "/", "gemini/gemini-2.5-flash"); the factory gets
# "gemini/gemini-2.5-flash" and passes it to LiteLlm directly.
PROVIDER_REGISTRY: dict[str, Callable[[str, dict[str, Any]], BaseLlm]] = {
    "litellm": lambda remainder, opts: LiteLlm(model=remainder, **opts),
    "gemini": lambda remainder, opts: Gemini(model_name=remainder, **opts),
    "anthropic": lambda remainder, opts: Claude(model_name=remainder, **opts),
    # OpenRouter routes through LiteLlm; the prefix must be preserved so
    # litellm picks the OpenRouter endpoint. Requires OPENROUTER_API_KEY.
    "openrouter": lambda remainder, opts: LiteLlm(
        model=f"openrouter/{remainder}", **opts
    ),
}


# Component name -> default model string (`<provider>/<rest>` form).
# Override per-session via state.model[component]; per-environment via
# MODEL_<COMPONENT> env vars (e.g. MODEL_CoordinatorAgent).
MODEL_DEFAULTS: dict[str, str] = {
    # Hot-swappable agents — LiteLlm routes through litellm.
    # Defaults are the LITE variant for cost/latency. Users hot-swap to
    # heavier models (gemini-3.1-pro-preview, claude-sonnet-4-6, etc.) per
    # task with `set_agent_model("<Component>", "litellm/<provider>/<model>")`.
    "CoordinatorAgent": "litellm/gemini/gemini-3.1-flash-lite-preview",
    "DeveloperAgent": "litellm/gemini/gemini-3.1-flash-lite-preview",
    "KnowledgeAgent": "litellm/gemini/gemini-3.1-flash-lite-preview",
    # Service models — keyed by role, not agent name
    "summarizer": "litellm/gemini/gemini-3.1-flash-lite-preview",
    # Pinned components — must use a native class (NOT hot-swappable)
    "google_search": "gemini/gemini-2.5-flash",
    "embedding": "gemini/gemini-embedding-001",
}


# Components that are pinned to a specific native provider and ignore
# `state.model` overrides. Documented here as the source of truth so plugins
# and tools can validate against this set.
PINNED_COMPONENTS: frozenset[str] = frozenset({"google_search", "embedding"})


def resolve_model(
    component: str,
    state: "OriSessionState | None" = None,
    **opts: Any,
) -> BaseLlm:
    """Resolve a BaseLlm instance for `component`, honoring runtime overrides.

    Precedence: state.model[component] > MODEL_<COMPONENT> env > MODEL_DEFAULTS.

    Pinned components ignore state overrides (the ModelConfigPlugin enforces
    this on the per-LLM-call hot-swap path; here we trust the caller, but a
    pinned component's default is a native model that CANNOT be replaced
    with a LiteLlm string at construction time without breaking the
    component's contract — e.g. native google-search grounding).
    """
    override = state.model.get(component) if state else None
    if component in PINNED_COMPONENTS:
        # Pinned: ignore state override entirely. Fall back to env then default.
        override = None
    model_str = (
        override or os.environ.get(f"MODEL_{component}") or MODEL_DEFAULTS[component]
    )
    provider, _, remainder = model_str.partition("/")
    factory = PROVIDER_REGISTRY.get(provider)
    if factory is None:
        raise ValueError(
            f"Unknown model provider {provider!r} for component {component!r}. "
            f"Known providers: {sorted(PROVIDER_REGISTRY)}. "
            f"To add a provider, register a factory in app/util/models.py:PROVIDER_REGISTRY."
        )
    return factory(remainder, opts)


def list_components() -> list[str]:
    """All known component names (used by `list_available_models` tool)."""
    return sorted(MODEL_DEFAULTS)


def get_default_model(component: str) -> str:
    """Default model string for a component (no state, no env)."""
    return MODEL_DEFAULTS[component]


def is_pinned(component: str) -> bool:
    """Whether the component ignores hot-swap overrides."""
    return component in PINNED_COMPONENTS


# ---------------------------------------------------------------------------
# Helpers used by tools and the /models command (legacy-shaped API for
# convenience; built on top of the registry above).
# ---------------------------------------------------------------------------

# Set of valid component names — alias used by the /models command.
VALID_COMPONENTS: frozenset[str] = frozenset(MODEL_DEFAULTS)


def get_model(component: str, **opts: Any) -> BaseLlm:
    """Construction-time helper used in agent definitions. State-free —
    runtime hot-swap is handled by ModelConfigPlugin reading state.model[].
    """
    return resolve_model(component, state=None, **opts)


def get_model_string(component: str) -> str:
    """The effective model string for `component` (env override or default)."""
    return os.environ.get(f"MODEL_{component}") or MODEL_DEFAULTS[component]


def get_model_name(component: str) -> str:
    """Just the model name (everything after the provider prefix).

    Used by callers that need the bare provider model id, e.g. the embedding
    client that takes `gemini-embedding-001` without the `gemini/` prefix.
    """
    full = get_model_string(component)
    _, _, remainder = full.partition("/")
    # If it's a litellm-routed string ("litellm/gemini/gemini-2.5-flash"),
    # strip the inner provider too so we get just the model name.
    if "/" in remainder:
        _, _, remainder = remainder.partition("/")
    return remainder


def _parse_model_str(model_str: str) -> tuple[str, str]:
    """(provider, model_name) split. Returns ("", "") on empty input."""
    if not model_str:
        return ("", "")
    provider, _, model = model_str.partition("/")
    return (provider, model)


def reset_all_models() -> list[str]:
    """Clear all `MODEL_<component>` env overrides; return list of cleared keys."""
    cleared: list[str] = []
    for component in MODEL_DEFAULTS:
        key = f"MODEL_{component}"
        if key in os.environ:
            os.environ.pop(key)
            cleared.append(component)
    return cleared


def reset_model(component: str) -> bool:
    """Clear one component's env override. Returns True if a value was cleared."""
    key = f"MODEL_{component}"
    if key in os.environ:
        os.environ.pop(key)
        return True
    return False


def format_model_assignments(
    markdown: bool = True,
    state_overrides: dict[str, str] | None = None,
) -> str:
    """Plain-text/Markdown table of effective model per component.

    Used by the /models command in the Telegram poller and CLI chat.
    Precedence: state_overrides > MODEL_<COMPONENT> env > MODEL_DEFAULTS.
    Pass `state_overrides` to reflect runtime hot-swaps from set_agent_model;
    omit it for the env-only view (e.g. when no session context is available).
    """
    state_overrides = state_overrides or {}
    lines: list[str] = []
    header = "Component             Default                                              Effective                                            Source"
    lines.append(header)
    lines.append("-" * len(header))
    for component in sorted(MODEL_DEFAULTS):
        default = MODEL_DEFAULTS[component]
        state_override = state_overrides.get(component)
        env_key = f"MODEL_{component}"
        env_override = os.environ.get(env_key)
        effective = state_override or env_override or default
        if state_override:
            source = "state"
        elif env_override:
            source = "env"
        else:
            source = "default"
        if is_pinned(component):
            source += " (pinned)"
        lines.append(f"{component:<22}{default:<54}{effective:<54}{source}")
    return "\n".join(lines)


def normalize_assignments() -> None:
    """Clean up legacy/quoted MODEL_<component> env values.

    Some shells store quoted values; strip them so resolve_model() sees the
    raw string. Idempotent.
    """
    for component in MODEL_DEFAULTS:
        key = f"MODEL_{component}"
        val = os.environ.get(key)
        if val and (val.startswith('"') or val.startswith("'")):
            os.environ[key] = val.strip("\"'")
