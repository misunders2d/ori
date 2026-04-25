"""app/util/models.py — provider registry + resolve_model precedence."""
import os
from unittest.mock import patch

import pytest

from app.state import OriSessionState
from app.util.models import (
    MODEL_DEFAULTS,
    PINNED_COMPONENTS,
    PROVIDER_REGISTRY,
    get_default_model,
    is_pinned,
    list_components,
    resolve_model,
)


def test_registry_has_litellm_and_natives():
    assert "litellm" in PROVIDER_REGISTRY
    assert "gemini" in PROVIDER_REGISTRY
    assert "anthropic" in PROVIDER_REGISTRY


def test_defaults_cover_all_components():
    expected = {
        "CoordinatorAgent",
        "DeveloperAgent",
        "KnowledgeAgent",
        "google_search",
        "embedding",
        "summarizer",
    }
    assert set(MODEL_DEFAULTS) >= expected


def test_pinned_set_contains_search_and_embedding():
    assert "google_search" in PINNED_COMPONENTS
    assert "embedding" in PINNED_COMPONENTS


def test_resolve_default_returns_litellm_for_coordinator():
    """CoordinatorAgent's default starts with `litellm/` so it routes via LiteLlm."""
    from google.adk.models import LiteLlm
    llm = resolve_model("CoordinatorAgent")
    assert isinstance(llm, LiteLlm)


def test_resolve_default_returns_native_gemini_for_search():
    """google_search is pinned to the native Gemini class."""
    from google.adk.models import Gemini
    llm = resolve_model("google_search")
    assert isinstance(llm, Gemini)


def test_state_override_swaps_provider():
    """A LiteLlm-routed component swaps cleanly between providers via state."""
    from google.adk.models import LiteLlm
    state = OriSessionState(model={"DeveloperAgent": "litellm/openai/gpt-4o"})
    llm = resolve_model("DeveloperAgent", state)
    assert isinstance(llm, LiteLlm)


def test_pinned_component_ignores_state_override():
    """google_search ignores state overrides — it must stay native Gemini."""
    from google.adk.models import Gemini
    state = OriSessionState(model={"google_search": "litellm/openai/gpt-4o"})
    llm = resolve_model("google_search", state)
    assert isinstance(llm, Gemini)


def test_unknown_provider_raises():
    state = OriSessionState(model={"CoordinatorAgent": "bogus/model-xyz"})
    with pytest.raises(ValueError, match="Unknown model provider"):
        resolve_model("CoordinatorAgent", state)


def test_env_override_above_default_below_state():
    """Precedence: state.model > MODEL_<COMPONENT> env > MODEL_DEFAULTS."""
    from google.adk.models import LiteLlm
    with patch.dict(os.environ, {"MODEL_KnowledgeAgent": "litellm/openai/gpt-4o-mini"}):
        # No state override → env wins
        llm = resolve_model("KnowledgeAgent")
        assert isinstance(llm, LiteLlm)
        # State override beats env
        state = OriSessionState(model={"KnowledgeAgent": "anthropic/claude-3-5-sonnet-20241022"})
        from google.adk.models import Claude
        llm2 = resolve_model("KnowledgeAgent", state)
        assert isinstance(llm2, Claude)


def test_helpers():
    assert is_pinned("google_search") is True
    assert is_pinned("CoordinatorAgent") is False
    assert "CoordinatorAgent" in list_components()
    assert get_default_model("CoordinatorAgent") == MODEL_DEFAULTS["CoordinatorAgent"]
