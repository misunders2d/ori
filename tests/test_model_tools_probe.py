"""Tests for set_agent_model's probe-before-persist safety.

The agent must NOT brick itself by switching to an unreachable model.
The probe constructs the BaseLlm, sends a 1-token "ping", and only
persists state if the call completes.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.tools.model_tools import set_agent_model, verify_model_reachable


class _FakeState(dict):
    def to_dict(self) -> dict:
        return dict(self)


class _FakeToolContext:
    def __init__(self):
        self.state = _FakeState()


@pytest.mark.asyncio
async def test_unknown_component_rejects_without_probe():
    """Validation errors fire before any probe — saves a probe call."""
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model") as probe:
        result = await set_agent_model(
            component="NotARealAgent",
            model="litellm/gemini/gemini-3-flash-preview",
            tool_context=ctx,
        )
    assert result["status"] == "error"
    assert result["error_code"] == "UNKNOWN_COMPONENT"
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_provider_rejects_without_probe():
    """Provider validation also short-circuits before probe."""
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model") as probe:
        result = await set_agent_model(
            component="CoordinatorAgent",
            model="bogus-provider/whatever",
            tool_context=ctx,
        )
    assert result["status"] == "error"
    assert result["error_code"] == "UNKNOWN_PROVIDER"
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_pinned_component_rejects_without_probe():
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model") as probe:
        result = await set_agent_model(
            component="google_search",
            model="litellm/gemini/gemini-3-flash-preview",
            tool_context=ctx,
        )
    assert result["status"] == "error"
    assert result["error_code"] == "COMPONENT_PINNED"
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_failed_probe_does_not_persist():
    """If the probe says unreachable, state stays unchanged. THE WHOLE POINT."""
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model", new=AsyncMock(return_value=(False, "auth failed"))):
        result = await set_agent_model(
            component="CoordinatorAgent",
            model="openrouter/deepseek/deepseek-v999-fake",
            tool_context=ctx,
        )
    assert result["status"] == "error"
    assert result["error_code"] == "MODEL_UNREACHABLE"
    assert "auth failed" in result["probe_error"]
    # State MUST be untouched — agent keeps its working model.
    assert "model" not in ctx.state or ctx.state.get("model") in ({}, None)


@pytest.mark.asyncio
async def test_passing_probe_persists_to_state():
    """Probe success → state.model[component] is set."""
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model", new=AsyncMock(return_value=(True, "ok"))):
        result = await set_agent_model(
            component="CoordinatorAgent",
            model="litellm/openai/gpt-4o-mini",
            tool_context=ctx,
        )
    assert result["status"] == "success"
    assert result["new"] == "litellm/openai/gpt-4o-mini"
    assert result["probe"] == "passed"
    assert ctx.state["model"]["CoordinatorAgent"] == "litellm/openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_skip_probe_persists_without_calling_probe():
    """skip_probe=True bypasses the probe entirely (escape hatch)."""
    ctx = _FakeToolContext()
    with patch("app.tools.model_tools._probe_model") as probe:
        result = await set_agent_model(
            component="CoordinatorAgent",
            model="litellm/openai/gpt-4o-mini",
            skip_probe=True,
            tool_context=ctx,
        )
    assert result["status"] == "success"
    assert result["probe"] == "skipped"
    probe.assert_not_called()
    assert ctx.state["model"]["CoordinatorAgent"] == "litellm/openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_verify_model_reachable_returns_probe_result():
    """The standalone verify tool just exposes _probe_model results."""
    with patch("app.tools.model_tools._probe_model", new=AsyncMock(return_value=(True, "ok"))):
        result = await verify_model_reachable(model="litellm/openai/gpt-4o")
    assert result["status"] == "success"
    assert result["model"] == "litellm/openai/gpt-4o"

    with patch("app.tools.model_tools._probe_model", new=AsyncMock(return_value=(False, "model not found"))):
        result = await verify_model_reachable(model="openrouter/fake/nope-2")
    assert result["status"] == "error"
    assert result["error_code"] == "MODEL_UNREACHABLE"
    assert "model not found" in result["message"]
