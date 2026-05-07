import pytest
from unittest.mock import MagicMock, patch
import os
from google.genai import types
from app.app_utils.models import _build_model

def test_openrouter_build_model_success():
    """Verify OpenRouter model is built using LiteLlm when key is present."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}):
        with patch("google.adk.models.lite_llm.LiteLlm") as MockLiteLlm:
            model = _build_model("openrouter", "deepseek/deepseek-chat")
            
            # Check if LiteLlm was called with correct model string
            MockLiteLlm.assert_called_once()
            args, kwargs = MockLiteLlm.call_args
            assert kwargs["model"] == "openrouter/deepseek/deepseek-chat"
            assert "retry_options" not in kwargs
            assert MockLiteLlm.return_value == model

def test_openrouter_drops_explicit_genai_retry_options():
    """Verify explicit Gemini retry options do not leak into LiteLlm kwargs."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key"}):
        with patch("google.adk.models.lite_llm.LiteLlm") as MockLiteLlm:
            _build_model(
                "openrouter",
                "deepseek/deepseek-chat",
                retry_options=types.HttpRetryOptions(attempts=3),
            )

            args, kwargs = MockLiteLlm.call_args
            assert kwargs["model"] == "openrouter/deepseek/deepseek-chat"
            assert "retry_options" not in kwargs

def test_openrouter_build_model_no_key():
    """Verify ValueError is raised when OpenRouter key is missing."""
    with patch.dict(os.environ, {}, clear=True):
        # Ensure it's not in environ
        if "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]
            
        with pytest.raises(ValueError, match="OpenRouter models require OPENROUTER_API_KEY"):
            _build_model("openrouter", "deepseek/deepseek-chat")

def test_supported_providers_includes_openrouter():
    """Verify openrouter is in SUPPORTED_PROVIDERS."""
    from app.app_utils.models import SUPPORTED_PROVIDERS
    assert "openrouter" in SUPPORTED_PROVIDERS
