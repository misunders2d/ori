"""Global thinking on/off switch — provider-agnostic.

One user-controlled flag, persisted to disk, applied to every sub-agent
regardless of backend (Gemini, Anthropic via OpenRouter, LiteLlm-routed
anything-else). Flipping the flag never restarts the bot.

Wiring overview:
  - ``data/thinking_config.json`` is the source of truth.
  - ``load()`` reads it (with a mtime-cached in-memory copy).
  - ``save(enabled, budget_tokens)`` writes it.
  - ``apply_to_agent_tree(root_agent)`` walks every sub-agent and mutates
    each LiteLlm instance's ``_additional_args`` so the next completion
    call uses the new setting. Called at boot and after each toggle.
  - The Gemini side reads ``load()`` from the ``state_setter`` callback
    per turn and sets ``llm_request.config.thinking_config`` accordingly.

Thoughts themselves are filtered out of user-facing output by
``app/core/agent_executor.py`` (``Part(thought=True)`` skipped). That
filter is independent of this flag — even if some provider returns
reasoning content despite the flag being off, the chat surface stays
clean.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.abspath("./data/thinking_config.json")
_DEFAULT_BUDGET_TOKENS = 4096
_DEFAULT_ENABLED = False

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_mtime: float = 0.0


def _read_disk() -> dict[str, Any]:
    if not os.path.isfile(_CONFIG_PATH):
        return {"enabled": _DEFAULT_ENABLED, "budget_tokens": _DEFAULT_BUDGET_TOKENS}
    try:
        with open(_CONFIG_PATH) as f:
            raw = json.load(f) or {}
    except Exception:
        return {"enabled": _DEFAULT_ENABLED, "budget_tokens": _DEFAULT_BUDGET_TOKENS}
    return {
        "enabled": bool(raw.get("enabled", _DEFAULT_ENABLED)),
        "budget_tokens": int(raw.get("budget_tokens", _DEFAULT_BUDGET_TOKENS)),
    }


def load() -> dict[str, Any]:
    """Return ``{"enabled": bool, "budget_tokens": int}``.

    Caches the file contents in memory and reloads only when the on-disk
    mtime advances, so per-turn callbacks don't hit the disk on every
    LLM call.
    """
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(_CONFIG_PATH) if os.path.isfile(_CONFIG_PATH) else 0.0
    except OSError:
        mtime = 0.0
    with _lock:
        if not _cache or mtime != _cache_mtime:
            _cache = _read_disk()
            _cache_mtime = mtime
        return dict(_cache)


def save(enabled: bool, budget_tokens: int = _DEFAULT_BUDGET_TOKENS) -> dict[str, Any]:
    """Write the flag to disk and bust the in-memory cache."""
    global _cache, _cache_mtime
    payload = {
        "enabled": bool(enabled),
        "budget_tokens": int(budget_tokens),
    }
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    tmp = f"{_CONFIG_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, _CONFIG_PATH)
    with _lock:
        _cache = dict(payload)
        try:
            _cache_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            _cache_mtime = 0.0
    return payload


# ---------------------------------------------------------------------------
# Application: walk the agent tree and update each backend
# ---------------------------------------------------------------------------


def _anthropic_thinking_kwargs(enabled: bool, budget_tokens: int) -> dict[str, Any]:
    """Return the LiteLLM completion kwargs that enable / disable Anthropic
    extended thinking. ``thinking`` is the Anthropic-native param;
    LiteLLM passes it through to the provider (and OpenRouter passes it
    through to Anthropic too).
    """
    if enabled:
        return {"thinking": {"type": "enabled", "budget_tokens": int(budget_tokens)}}
    return {}


def _apply_to_litellm(model, enabled: bool, budget_tokens: int) -> bool:
    """Mutate a LiteLlm-backed model's ``_additional_args`` in place.

    Returns True if anything changed. Safe to call on any object — it
    no-ops when ``_additional_args`` isn't present (e.g. native Gemini
    backend).
    """
    addl = getattr(model, "_additional_args", None)
    if addl is None:
        return False

    desired = _anthropic_thinking_kwargs(enabled, budget_tokens)

    changed = False
    if "thinking" in addl and "thinking" not in desired:
        addl.pop("thinking", None)
        changed = True
    elif "thinking" in desired and addl.get("thinking") != desired["thinking"]:
        addl["thinking"] = desired["thinking"]
        changed = True
    return changed


def apply_to_agent_tree(root_agent) -> dict[str, int]:
    """Walk every sub-agent reachable from ``root_agent`` and apply the
    current thinking flag to each LiteLlm backend it owns.

    Returns counts of agents inspected / mutated for logging. Idempotent:
    calling it twice with no flag change does nothing.

    Gemini-backed agents are handled per-turn in ``state_setter`` (the
    ``thinking_config`` lives on the per-call ``llm_request``, not the
    long-lived model), so they're counted but not mutated here.
    """
    cfg = load()
    inspected = 0
    mutated = 0

    visited: set[int] = set()

    def walk(agent):
        nonlocal inspected, mutated
        if agent is None or id(agent) in visited:
            return
        visited.add(id(agent))

        inspected += 1
        model = getattr(agent, "canonical_model", None) or getattr(agent, "model", None)
        if _apply_to_litellm(model, cfg["enabled"], cfg["budget_tokens"]):
            mutated += 1

        for sa in getattr(agent, "sub_agents", []) or []:
            walk(sa)

    walk(root_agent)
    logger.info(
        "thinking.apply_to_agent_tree: enabled=%s budget=%d inspected=%d mutated=%d",
        cfg["enabled"],
        cfg["budget_tokens"],
        inspected,
        mutated,
    )
    return {"inspected": inspected, "mutated": mutated}
