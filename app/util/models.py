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
}


# Component name -> default model string (`<provider>/<rest>` form).
# Override per-session via state.model[component]; per-environment via
# MODEL_<COMPONENT> env vars (e.g. MODEL_CoordinatorAgent).
MODEL_DEFAULTS: dict[str, str] = {
    # Hot-swappable agents — LiteLlm routes through litellm
    "CoordinatorAgent": "litellm/gemini/gemini-2.5-flash",
    "DeveloperAgent": "litellm/anthropic/claude-3-5-sonnet-20241022",
    "KnowledgeAgent": "litellm/gemini/gemini-2.5-flash",
    # Service models — keyed by role, not agent name
    "summarizer": "litellm/gemini/gemini-2.5-flash-lite",
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
    model_str = override or os.environ.get(f"MODEL_{component}") or MODEL_DEFAULTS[component]
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
