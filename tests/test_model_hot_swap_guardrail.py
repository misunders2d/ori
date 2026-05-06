from types import SimpleNamespace

import pytest

from app.callbacks.guardrails import prompt_injection_guardrail


class _State(dict):
    def to_dict(self):
        return dict(self)


def _context(agent_name: str, initialized_model):
    agent = SimpleNamespace(name=agent_name, model=initialized_model)
    invocation_context = SimpleNamespace(agent=agent)
    return SimpleNamespace(
        agent_name=agent_name,
        state=_State(),
        _invocation_context=invocation_context,
    )


def _request(model: str):
    return SimpleNamespace(model=model, contents=[], config=None)


@pytest.mark.asyncio
async def test_cross_provider_override_does_not_rewrite_gemini_request():
    ctx = _context(
        "AmazonAgent",
        SimpleNamespace(model="gemini-3-flash-preview"),
    )
    ctx.state["model:AmazonAgent"] = "openrouter/deepseek/deepseek-v4-flash"
    req = _request("gemini-3-flash-preview")

    await prompt_injection_guardrail(ctx, req)

    assert req.model == "gemini-3-flash-preview"


@pytest.mark.asyncio
async def test_same_provider_override_rewrites_request_model():
    ctx = _context(
        "AmazonAgent",
        SimpleNamespace(model="gemini-3-flash-preview"),
    )
    ctx.state["model:AmazonAgent"] = "google/gemini-3.1-flash-lite-preview"
    req = _request("gemini-3-flash-preview")

    await prompt_injection_guardrail(ctx, req)

    assert req.model == "gemini-3.1-flash-lite-preview"
