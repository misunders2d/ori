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
    req = SimpleNamespace(
        model=model,
        contents=[],
        config=SimpleNamespace(system_instruction=None),
    )

    def append_instructions(instructions):
        text = "\n\n".join(instructions)
        if req.config.system_instruction:
            req.config.system_instruction += "\n\n" + text
        else:
            req.config.system_instruction = text

    req.append_instructions = append_instructions
    return req


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
    assert "TERSE STYLE" in req.config.system_instruction


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


@pytest.mark.asyncio
async def test_litellm_openrouter_override_keeps_provider_prefix():
    LiteLlmModel = type(
        "LiteLlm",
        (),
        {"__module__": "google.adk.models.lite_llm"},
    )
    ctx = _context(
        "AmazonAgent",
        LiteLlmModel(),
    )
    ctx._invocation_context.agent.model.model = (
        "openrouter/deepseek/deepseek-chat"
    )
    ctx.state["model:AmazonAgent"] = "openrouter/deepseek/deepseek-v4-flash"
    req = _request("openrouter/deepseek/deepseek-chat")

    await prompt_injection_guardrail(ctx, req)

    assert req.model == "openrouter/deepseek/deepseek-v4-flash"
