"""ModelErrorHandlerPlugin — translates raw model errors into user-visible
LlmResponses instead of crashing the turn or flooding logs with tracebacks."""

from types import SimpleNamespace

import pytest

from app.plugins.model_error_handler import (
    ModelErrorHandlerPlugin,
    _classify,
    _short_error,
)


def test_classify_rate_limit():
    class RateLimitError(Exception):
        pass
    label, hint = _classify(RateLimitError("429 Too Many Requests from openrouter"))
    assert label == "rate limited"
    assert "set_agent_model" in hint or "swap" in hint.lower()


def test_classify_auth_error():
    class AuthenticationError(Exception):
        pass
    label, hint = _classify(AuthenticationError("invalid api key"))
    assert "auth" in label.lower()


def test_classify_not_found():
    class NotFoundError(Exception):
        pass
    label, _ = _classify(NotFoundError("model 'xyz' does not exist"))
    assert "not found" in label.lower()


def test_classify_unknown_falls_back():
    label, hint = _classify(RuntimeError("something weird happened"))
    assert label == "model error"
    assert hint  # has some hint


def test_classify_reads_message_when_class_name_unhelpful():
    """Generic 'Exception' class with rate-limit text in message → still classified."""
    label, _ = _classify(Exception("RateLimitError: 429 hit it"))
    assert label == "rate limited"


def test_short_error_truncates_long_messages():
    big = "x" * 1000
    out = _short_error(RuntimeError(big))
    assert len(out) < 320
    assert out.startswith("RuntimeError:")


def test_short_error_strips_newlines():
    out = _short_error(RuntimeError("line1\nline2\rline3"))
    assert "\n" not in out
    assert "\r" not in out


@pytest.mark.asyncio
async def test_callback_returns_friendly_response():
    """The hook must substitute an LlmResponse — NOT re-raise."""
    class RateLimitError(Exception):
        pass
    plugin = ModelErrorHandlerPlugin()
    cb_ctx = SimpleNamespace(agent_name="CoordinatorAgent")
    llm_req = SimpleNamespace(model="openrouter/deepseek/deepseek-v4-flash")
    err = RateLimitError("429 Too Many Requests")

    result = await plugin.on_model_error_callback(
        callback_context=cb_ctx,
        llm_request=llm_req,
        error=err,
    )

    # Must return an LlmResponse with model-role text.
    assert result is not None
    assert result.content is not None
    assert result.content.role == "model"
    text = result.content.parts[0].text
    assert "rate limited" in text.lower()
    assert "openrouter/deepseek/deepseek-v4-flash" in text
    assert "set_agent_model" in text or "swap" in text.lower()
