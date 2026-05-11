"""Global thinking on/off switch — persisted, provider-agnostic.

Covers:
- ``app.app_utils.thinking`` load/save round-trip + mtime cache invalidation
- ``apply_to_agent_tree`` walks the agent tree and mutates LiteLlm-backed
  models' ``_additional_args`` in place (Anthropic ``thinking`` kwarg).
- The Gemini side (per-turn ``thinking_config = None`` from ``state_setter``)
  is exercised in ``test_callback_guardrails.py``; this file focuses on
  the LiteLlm path and the disk-backed config.
"""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect the thinking-config JSON to ``tmp_path`` and bust the
    in-memory cache so each test starts from a known state.
    """
    from app.app_utils import thinking

    target = tmp_path / "thinking_config.json"
    monkeypatch.setattr(thinking, "_CONFIG_PATH", str(target))

    # Reset module-level cache (without it, the previous test's state
    # bleeds in through the in-memory copy).
    monkeypatch.setattr(thinking, "_cache", {})
    monkeypatch.setattr(thinking, "_cache_mtime", 0.0)

    return target


# ---------------------------------------------------------------------------
# load / save
# ---------------------------------------------------------------------------


def test_load_returns_defaults_when_file_absent(isolated_config):
    from app.app_utils.thinking import load

    cfg = load()
    assert cfg == {"enabled": False, "budget_tokens": 4096}


def test_load_returns_defaults_on_corrupt_file(isolated_config):
    from app.app_utils.thinking import load

    isolated_config.write_text("{not json")
    cfg = load()
    assert cfg["enabled"] is False
    assert cfg["budget_tokens"] == 4096


def test_save_then_load_round_trip(isolated_config):
    from app.app_utils.thinking import load, save

    cfg = save(enabled=True, budget_tokens=8192)
    assert cfg == {"enabled": True, "budget_tokens": 8192}

    on_disk = json.loads(isolated_config.read_text())
    assert on_disk == {"enabled": True, "budget_tokens": 8192}

    # Cached load reflects the save without re-reading the file.
    assert load() == {"enabled": True, "budget_tokens": 8192}


def test_cache_invalidates_when_file_changes(isolated_config):
    """Two ``load()`` calls should reflect external changes to the file
    once its mtime advances — otherwise the second toggle wouldn't take
    effect until a process restart.
    """
    from app.app_utils.thinking import load, save

    save(enabled=False, budget_tokens=4096)
    assert load()["enabled"] is False

    # Direct disk write (simulates another process / manual edit).
    isolated_config.write_text(
        json.dumps({"enabled": True, "budget_tokens": 1024})
    )
    # Bump mtime by 1 second so the cache invalidator notices the change.
    new_mtime = os.path.getmtime(isolated_config) + 1
    os.utime(isolated_config, (new_mtime, new_mtime))

    refreshed = load()
    assert refreshed["enabled"] is True
    assert refreshed["budget_tokens"] == 1024


# ---------------------------------------------------------------------------
# apply_to_agent_tree
# ---------------------------------------------------------------------------


class _FakeLiteLlmModel:
    """Minimal stand-in: only has ``_additional_args``, which is what the
    apply function inspects/mutates. Matches the shape of
    ``google.adk.models.lite_llm.LiteLlm``.
    """

    def __init__(self, additional_args=None):
        self._additional_args = dict(additional_args or {})


class _FakeNativeModel:
    """Stand-in for a Gemini / non-LiteLlm backend — has no
    ``_additional_args``, so the apply function must skip it cleanly.
    """


class _FakeAgent:
    def __init__(self, model=None, sub_agents=None):
        self.canonical_model = model
        self.sub_agents = list(sub_agents or [])


def test_apply_enables_anthropic_thinking_on_litellm_only(isolated_config):
    from app.app_utils import thinking
    from app.app_utils.thinking import apply_to_agent_tree

    thinking.save(enabled=True, budget_tokens=2048)

    litellm = _FakeLiteLlmModel()
    native = _FakeNativeModel()
    root = _FakeAgent(
        model=native,
        sub_agents=[_FakeAgent(model=litellm)],
    )

    counts = apply_to_agent_tree(root)

    assert counts["inspected"] == 2
    assert counts["mutated"] == 1
    assert litellm._additional_args == {
        "thinking": {"type": "enabled", "budget_tokens": 2048}
    }


def test_apply_clears_thinking_when_disabled(isolated_config):
    from app.app_utils import thinking
    from app.app_utils.thinking import apply_to_agent_tree

    # Start from enabled state with thinking already on the model.
    thinking.save(enabled=True, budget_tokens=4096)
    litellm = _FakeLiteLlmModel(
        additional_args={"thinking": {"type": "enabled", "budget_tokens": 4096}}
    )
    root = _FakeAgent(model=litellm)
    apply_to_agent_tree(root)
    assert "thinking" in litellm._additional_args

    # Flip the flag off — apply must clear the kwarg.
    thinking.save(enabled=False, budget_tokens=4096)
    apply_to_agent_tree(root)
    assert "thinking" not in litellm._additional_args


def test_apply_is_idempotent(isolated_config):
    """Calling apply twice in a row with no flag change must not report
    spurious mutations — the boot path + per-toggle path can stack and
    we don't want either to claim it changed anything when it didn't.
    """
    from app.app_utils import thinking
    from app.app_utils.thinking import apply_to_agent_tree

    thinking.save(enabled=True, budget_tokens=1024)
    litellm = _FakeLiteLlmModel()
    root = _FakeAgent(model=litellm)

    first = apply_to_agent_tree(root)
    second = apply_to_agent_tree(root)
    assert first["mutated"] == 1
    assert second["mutated"] == 0


def test_apply_tolerates_cyclic_agent_tree(isolated_config):
    """Sub-agent graphs can have shared references (e.g. multiple parents
    pointing to the same memory agent). The walker must not loop forever.
    """
    from app.app_utils import thinking
    from app.app_utils.thinking import apply_to_agent_tree

    thinking.save(enabled=True, budget_tokens=512)
    shared = _FakeAgent(model=_FakeLiteLlmModel())
    a = _FakeAgent(model=_FakeLiteLlmModel(), sub_agents=[shared])
    b = _FakeAgent(model=_FakeLiteLlmModel(), sub_agents=[shared])
    root = _FakeAgent(model=_FakeNativeModel(), sub_agents=[a, b])

    counts = apply_to_agent_tree(root)
    # root + a + b + shared (visited once) = 4 inspected.
    assert counts["inspected"] == 4
