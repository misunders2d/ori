"""Tests for app.app_utils.models hot-swap helpers.

Focused on `hydrate_model_env` — the Phase 2 startup rehydration that
seeds os.environ[MODEL_*] from data/model_config.json before any agent
module is imported.
"""

import json
import os

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Run each test against a private model_config.json in a tmp dir.

    Patches the resolved path used by `app.app_utils.model_config` so we
    don't touch the real data/model_config.json, and scrubs any MODEL_*
    env vars from the test environment.
    """
    cfg = tmp_path / "model_config.json"
    monkeypatch.setattr("app.app_utils.model_config._CONFIG_PATH", str(cfg))

    # Drop any stray MODEL_* env vars so we measure exactly what hydrate sets.
    for key in list(os.environ.keys()):
        if key.startswith("MODEL_"):
            monkeypatch.delenv(key, raising=False)

    return cfg


def _write_config(path, assignments: dict):
    path.write_text(json.dumps({"assignments": assignments, "model_cache": {}}))


def test_hydrate_noop_when_no_config(isolated_config, monkeypatch):
    """No config file → no env vars set → returns 0."""
    from app.app_utils.models import hydrate_model_env

    count = hydrate_model_env()

    assert count == 0
    assert not any(k.startswith("MODEL_") for k in os.environ)


def test_hydrate_applies_persisted_assignments(isolated_config):
    """Valid assignments in config → matching os.environ[MODEL_*] entries set."""
    from app.app_utils.models import hydrate_model_env

    _write_config(isolated_config, {
        "CoordinatorAgent": "anthropic/claude-sonnet-4-6",
        "DeveloperAgent":   "openrouter/anthropic/claude-3-haiku",
    })

    count = hydrate_model_env()

    assert count == 2
    assert os.environ.get("MODEL_COORDINATORAGENT") == "anthropic/claude-sonnet-4-6"
    assert os.environ.get("MODEL_DEVELOPERAGENT") == "openrouter/anthropic/claude-3-haiku"


def test_hydrate_skips_unknown_components(isolated_config):
    """Assignments for unknown components are ignored, valid ones still applied."""
    from app.app_utils.models import hydrate_model_env

    _write_config(isolated_config, {
        "CoordinatorAgent": "google/gemini-3-pro-preview",
        "NotAComponent":    "anthropic/claude-sonnet-4-6",
        "":                 "should-be-ignored",
    })

    count = hydrate_model_env()

    assert count == 1
    assert os.environ.get("MODEL_COORDINATORAGENT") == "google/gemini-3-pro-preview"
    assert os.environ.get("MODEL_NOTACOMPONENT") is None


def test_hydrate_skips_empty_values(isolated_config):
    """Empty / whitespace model strings are dropped — they'd resolve to ''."""
    from app.app_utils.models import hydrate_model_env

    _write_config(isolated_config, {
        "CoordinatorAgent": "   ",
        "DeveloperAgent":   "",
        "AmazonAgent":      "anthropic/claude-sonnet-4-6",
    })

    count = hydrate_model_env()

    assert count == 1
    assert os.environ.get("MODEL_AMAZONAGENT") == "anthropic/claude-sonnet-4-6"
    assert "MODEL_COORDINATORAGENT" not in os.environ


def test_hydrate_strips_whitespace(isolated_config):
    """Stored value with stray whitespace is trimmed before being set in env."""
    from app.app_utils.models import hydrate_model_env

    _write_config(isolated_config, {
        "CoordinatorAgent": "  anthropic/claude-sonnet-4-6  ",
    })

    count = hydrate_model_env()

    assert count == 1
    assert os.environ.get("MODEL_COORDINATORAGENT") == "anthropic/claude-sonnet-4-6"


def test_hydrate_handles_corrupt_config(isolated_config, caplog):
    """A junk config file is treated as no overrides — returns 0 without raising."""
    isolated_config.write_text("{ not valid json")

    from app.app_utils.models import hydrate_model_env

    # The underlying _read_config logs a warning and returns the empty default,
    # so hydrate sees no assignments and applies zero.
    count = hydrate_model_env()

    assert count == 0


# ---------------------------------------------------------------------------
# set_model: refuse overrides whose provider key is missing
# ---------------------------------------------------------------------------


def test_set_model_refuses_when_provider_key_missing(isolated_config, monkeypatch):
    """Persisting an override the runtime can't honour would feed the
    auto-repair loop in get_model and leave the user wondering why
    nothing changed. set_model must refuse with a clear error."""
    from app.app_utils.models import set_model

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    with pytest.raises(ValueError, match="no usable API key"):
        set_model("CoordinatorAgent", "openrouter/anthropic/claude-opus-4.7")


def test_set_model_accepts_when_provider_key_present(isolated_config, monkeypatch):
    """With the key in env, the persist call goes through and the
    assignment lands in model_config.json."""
    from app.app_utils.model_config import get_assignment
    from app.app_utils.models import set_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    set_model("CoordinatorAgent", "openrouter/anthropic/claude-opus-4.7")
    assert get_assignment("CoordinatorAgent") == "openrouter/anthropic/claude-opus-4.7"


# ---------------------------------------------------------------------------
# get_model: auto-repair on fallback
# ---------------------------------------------------------------------------


def test_get_model_auto_repairs_unusable_override(isolated_config, monkeypatch, caplog):
    """When the persisted override can't be built (e.g. key missing
    after rotation), get_model falls back to default AND clears the
    bad override. Otherwise state_setter would emit a permanent
    cross-provider warning the user can't reconcile."""
    import logging

    from app.app_utils.model_config import get_assignment
    from app.app_utils.models import get_model

    # Seed a bad override directly via the config layer so we exercise
    # the auto-repair path without tripping set_model's preflight.
    from app.app_utils.model_config import set_assignment

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-temp")
    set_assignment("CoordinatorAgent", "openrouter/anthropic/claude-opus-4.7")
    # Then yank the key so the build fails.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Provide GOOGLE_API_KEY so the default (google/gemini-3-flash-preview)
    # can be built as the fallback.
    monkeypatch.setenv("GOOGLE_API_KEY", "gk-test")

    with caplog.at_level(logging.WARNING):
        model = get_model("CoordinatorAgent")

    # Fallback succeeded — model object exists.
    assert model is not None
    # Override cleared from model_config.json so the next call uses the
    # default cleanly and state_setter has nothing to warn about.
    # (model_config.get_assignment returns "" for missing keys, treat both
    # as "no override".)
    assert not get_assignment("CoordinatorAgent")
    # Env var also cleared so a fresh process doesn't re-hydrate the
    # bad override.
    assert "MODEL_COORDINATORAGENT" not in os.environ
    # Warning surfaced once for the operator.
    assert any(
        "auto-repair" in r.message.lower() or "clearing" in r.message.lower()
        for r in caplog.records
    )
