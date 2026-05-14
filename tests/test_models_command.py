"""Tests for the ``/models`` slash command dispatcher.

Pre-2026-05-14 the slash command supported only LIST and RESET
paths. Setting a model required a conversational message to the LLM
("yo, change the developer agent to ...") which replayed the
entire session history (including stale multimodal parts) and
sometimes burned thousands of tokens AND broke when the target
model couldn't accept the inline data. The SET path now lives in
``dispatch_models_command`` and is callable from both Slack and
Telegram pollers without ever invoking the agent.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def isolated_model_config(tmp_path, monkeypatch):
    """Redirect ``model_config.json`` to a per-test file and pre-set
    a usable OpenRouter / Anthropic API key so ``set_model``'s
    key-availability check passes."""
    cfg = tmp_path / "model_config.json"
    monkeypatch.setattr(
        "app.app_utils.model_config._CONFIG_PATH", str(cfg)
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")
    return cfg


def test_models_list(isolated_model_config):
    from app.app_utils.models import dispatch_models_command

    reply = dispatch_models_command("/models")
    assert "Model assignments" in reply
    assert "CoordinatorAgent" in reply


def test_models_default_resets_all(isolated_model_config):
    from app.app_utils.models import dispatch_models_command, set_model

    set_model("DeveloperAgent", "openrouter/anthropic/claude-sonnet-4.6")
    reply = dispatch_models_command("/models default")
    assert "Reset" in reply
    assert "override" in reply.lower()


def test_models_default_one_component(isolated_model_config):
    from app.app_utils.models import dispatch_models_command, set_model

    set_model("DeveloperAgent", "openrouter/anthropic/claude-sonnet-4.6")
    reply = dispatch_models_command("/models default DeveloperAgent")
    assert "Reset DeveloperAgent" in reply


def test_models_default_unknown_component(isolated_model_config):
    from app.app_utils.models import dispatch_models_command

    reply = dispatch_models_command("/models default GhostAgent")
    assert "Unknown component" in reply
    assert "GhostAgent" in reply


def test_models_set_shorthand(isolated_model_config):
    """``/models <Component> <model>`` is the shorthand form the user
    expected to work. Direct repro of the 2026-05-14 gap."""
    from app.app_utils.models import dispatch_models_command, get_model_string

    reply = dispatch_models_command(
        "/models DeveloperAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "DeveloperAgent" in reply
    assert "openrouter/deepseek/deepseek-v4-flash" in reply
    assert (
        get_model_string("DeveloperAgent")
        == "openrouter/deepseek/deepseek-v4-flash"
    )


def test_models_set_explicit_form(isolated_model_config):
    """``/models set <Component> <model>`` is the explicit alias."""
    from app.app_utils.models import dispatch_models_command, get_model_string

    reply = dispatch_models_command(
        "/models set DeveloperAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "DeveloperAgent" in reply
    assert "openrouter/deepseek/deepseek-v4-flash" in reply
    assert (
        get_model_string("DeveloperAgent")
        == "openrouter/deepseek/deepseek-v4-flash"
    )


def test_models_set_unknown_component(isolated_model_config):
    from app.app_utils.models import dispatch_models_command

    # Three-part with unknown component → falls through to usage.
    reply = dispatch_models_command(
        "/models GhostAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "Usage" in reply


def test_models_set_unsupported_provider(isolated_model_config):
    """``set_model`` rejects providers it doesn't support — dispatcher
    surfaces the ValueError verbatim instead of falling through to
    usage."""
    from app.app_utils.models import dispatch_models_command

    reply = dispatch_models_command(
        "/models DeveloperAgent fakecorp/imaginary-model"
    )
    assert "Failed to set DeveloperAgent" in reply
    assert "fakecorp" in reply.lower() or "provider" in reply.lower()


def test_models_set_missing_api_key(isolated_model_config, monkeypatch):
    """When the chosen provider has no API key in env, ``set_model``
    refuses and the dispatcher surfaces the failure verbatim."""
    from app.app_utils.models import dispatch_models_command

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    reply = dispatch_models_command(
        "/models DeveloperAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "Failed to set DeveloperAgent" in reply
    assert "API key" in reply or "key" in reply.lower()


def test_models_set_same_provider_says_live(isolated_model_config):
    """Same-provider swap (openrouter → openrouter) takes effect on
    the next LLM call. Reply should say so."""
    from app.app_utils.models import dispatch_models_command, set_model

    set_model("DeveloperAgent", "openrouter/anthropic/claude-sonnet-4.6")
    reply = dispatch_models_command(
        "/models DeveloperAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "Live" in reply or "live" in reply
    assert "Cross-provider" not in reply


def test_models_set_cross_provider_says_restart(isolated_model_config):
    """Cross-provider swap requires restart because the agent's
    canonical_model is locked at boot."""
    from app.app_utils.models import dispatch_models_command, set_model

    # Seed with anthropic provider.
    set_model("DeveloperAgent", "anthropic/claude-sonnet-4.6")
    reply = dispatch_models_command(
        "/models DeveloperAgent openrouter/deepseek/deepseek-v4-flash"
    )
    assert "Cross-provider" in reply or "restart" in reply.lower()


def test_models_garbage_input_shows_usage(isolated_model_config):
    from app.app_utils.models import dispatch_models_command

    reply = dispatch_models_command("/models foobar baz qux quux extra")
    assert "Usage" in reply
