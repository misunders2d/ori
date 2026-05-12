"""Per-component thinking level — Gemini 3 levels + Anthropic budget map.

Was a global on/off boolean until 2026-05-12. Gemini 3 defaults to
`high` thinking, so the old toggle was effectively a no-op (setting
`thinking_config=None` left the model on its default). The new design
mirrors `MODEL_DEFAULTS` — every component has a default thinking
level matching its tier, and users can override per-component via
`set_thinking_level(component, level)`.

Levels (Gemini 3 vocabulary, mirrored to Anthropic budget map):

| Level     | Gemini 3 semantics                  | Anthropic budget |
|-----------|-------------------------------------|------------------|
| `minimal` | "No thinking" for most queries      | thinking off     |
| `low`     | Minimum latency + cost              | thinking off     |
| `medium`  | Balanced — codegen / SQL synth      | budget 4096      |
| `high`    | Maximum reasoning depth             | budget 8192      |

Wiring:
- `data/thinking_config.json` is the source of truth for per-component
  overrides. Defaults live in `THINKING_DEFAULTS` below.
- `load_level(component)` returns the effective level (override or
  default). Cached with mtime invalidation.
- `save_level(component, level)` persists a single override.
- `apply_to_agent_tree(root_agent)` walks LiteLlm-backed agents
  (Anthropic, OpenRouter etc.) and sets the `thinking` completion
  kwarg from the per-agent level. Idempotent.
- The Gemini side reads the level per turn in `prompt_injection_guardrail`
  and sets `llm_request.config.thinking_config = ThinkingConfig(thinking_level=...)`.

Thoughts are filtered from user-facing output by `agent_executor.py`
(`Part(thought=True)` skipped) regardless of level.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.abspath("./data/thinking_config.json")
VALID_LEVELS = ("minimal", "low", "medium", "high")

# Default thinking level per component. Routing agents → `low`, CRUD
# leaves → `minimal`, code/SQL synth → `medium`. DeveloperAgent on
# Sonnet defaults to `medium` (Anthropic budget 4096) — strong code
# reasoning without paying for `high`-tier 8K thinking budget.
THINKING_DEFAULTS: dict[str, str] = {
    # Coordinator + AmazonHead bumped from low to medium 2026-05-12:
    # production proof that `low` was too shallow for explicit rule
    # following — bot hallucinated a self-reboot refusal despite the
    # REBOOT/RESTART rule in its instruction. Medium fixes that with
    # only a modest thinking-token bump.
    "CoordinatorAgent":         "medium",
    "AmazonHeadAgent":          "medium",
    "KnowledgeAgent":           "low",
    "AmazonAgent":              "minimal",
    "AmazonMemoryAgent":        "minimal",
    "AmazonWorkspaceAgent":     "minimal",
    "ClickUpAgent":             "minimal",
    "google_search":            "minimal",
    "AmazonDataAnalystAgent":   "medium",
    "BigQueryAgent":            "medium",
    "youtube_summarizer":       "minimal",
    "summarizer":               "minimal",
    "session_summarizer":       "minimal",
    "DeveloperAgent":           "medium",
}

# Anthropic uses `thinking={"type":"enabled","budget_tokens":N}`. Map
# Gemini-style levels to Anthropic budgets. minimal/low → thinking off
# (no `thinking` kwarg). medium/high → thinking on with these budgets.
_ANTHROPIC_BUDGET_BY_LEVEL: dict[str, int] = {
    "minimal": 0,
    "low":     0,
    "medium":  4096,
    "high":    8192,
}

_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_mtime: float = 0.0


def _read_disk() -> dict[str, str]:
    """Read persisted overrides. Migrates legacy {enabled, budget_tokens} → empty dict."""
    if not os.path.isfile(_CONFIG_PATH):
        return {}
    try:
        with open(_CONFIG_PATH) as f:
            raw = json.load(f) or {}
    except Exception:
        return {}
    # Legacy schema {enabled, budget_tokens} → ignore (defaults apply).
    if "levels" in raw and isinstance(raw["levels"], dict):
        levels = raw["levels"]
        return {
            str(k): v
            for k, v in levels.items()
            if isinstance(v, str) and v in VALID_LEVELS
        }
    return {}


def _read_disk_cached() -> dict[str, str]:
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


def load_level(component: str) -> str:
    """Return the effective thinking level for a component (override or default)."""
    overrides = _read_disk_cached()
    return overrides.get(component) or THINKING_DEFAULTS.get(component, "low")


def load_all_levels() -> dict[str, str]:
    """Return {component: effective_level} for every known component."""
    overrides = _read_disk_cached()
    return {
        c: overrides.get(c) or THINKING_DEFAULTS.get(c, "low")
        for c in THINKING_DEFAULTS
    }


def save_level(component: str, level: str) -> dict[str, str]:
    """Persist a per-component override. Returns the new full overrides map."""
    if component not in THINKING_DEFAULTS:
        raise ValueError(
            f"Unknown component: '{component}'. Valid: {sorted(THINKING_DEFAULTS)}"
        )
    if level not in VALID_LEVELS:
        raise ValueError(f"Invalid level: '{level}'. Valid: {VALID_LEVELS}")

    global _cache, _cache_mtime
    overrides = _read_disk_cached()
    # If the override matches the default, store it anyway — explicit beats implicit.
    overrides[component] = level
    payload = {"levels": overrides}

    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    tmp = f"{_CONFIG_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, _CONFIG_PATH)
    with _lock:
        _cache = dict(overrides)
        try:
            _cache_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            _cache_mtime = 0.0
    return dict(overrides)


def reset_level(component: str) -> bool:
    """Clear a single component's override, reverting it to its default.

    Returns True if something was cleared, False if no override existed.
    """
    if component not in THINKING_DEFAULTS:
        raise ValueError(
            f"Unknown component: '{component}'. Valid: {sorted(THINKING_DEFAULTS)}"
        )

    global _cache, _cache_mtime
    overrides = _read_disk_cached()
    if component not in overrides:
        return False

    overrides.pop(component, None)
    payload = {"levels": overrides}

    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    tmp = f"{_CONFIG_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, _CONFIG_PATH)
    with _lock:
        _cache = dict(overrides)
        try:
            _cache_mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            _cache_mtime = 0.0
    return True


# ---------------------------------------------------------------------------
# Anthropic / LiteLlm application — long-lived model kwargs
# ---------------------------------------------------------------------------


def _anthropic_thinking_kwargs(level: str) -> dict[str, Any]:
    """Return LiteLLM completion kwargs that enable / disable Anthropic
    extended thinking, derived from the Gemini-style level.
    """
    budget = _ANTHROPIC_BUDGET_BY_LEVEL.get(level, 0)
    if budget <= 0:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": int(budget)}}


def _apply_to_litellm(model, level: str) -> bool:
    """Mutate a LiteLlm-backed model's ``_additional_args`` in place.

    Returns True if anything changed. No-op when ``_additional_args``
    isn't present (e.g. native Gemini backend).
    """
    addl = getattr(model, "_additional_args", None)
    if addl is None:
        return False

    desired = _anthropic_thinking_kwargs(level)

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
    per-component thinking level to each LiteLlm backend it owns.

    Gemini-backed agents are handled per-turn in ``prompt_injection_guardrail``
    (the ``thinking_config`` lives on the per-call ``llm_request``,
    not the long-lived model), so they're counted but not mutated here.

    Idempotent. Returns counts of agents inspected / mutated for logging.
    """
    inspected = 0
    mutated = 0
    visited: set[int] = set()

    def walk(agent):
        nonlocal inspected, mutated
        if agent is None or id(agent) in visited:
            return
        visited.add(id(agent))

        inspected += 1
        name = getattr(agent, "name", "") or ""
        level = load_level(name) if name else "low"

        model = getattr(agent, "canonical_model", None) or getattr(agent, "model", None)
        if _apply_to_litellm(model, level):
            mutated += 1

        for sa in getattr(agent, "sub_agents", []) or []:
            walk(sa)

    walk(root_agent)
    logger.info(
        "thinking.apply_to_agent_tree: inspected=%d mutated=%d (per-agent levels)",
        inspected, mutated,
    )
    return {"inspected": inspected, "mutated": mutated}


# ---------------------------------------------------------------------------
# Backward compat shims — keep the old API alive so callers don't break
# ---------------------------------------------------------------------------


def load() -> dict[str, Any]:
    """Legacy API — preserved for transition.

    Returns an aggregate snapshot derived from per-component levels. The
    `enabled` flag reflects whether ANY component has thinking on (level
    medium or high). `budget_tokens` is the max Anthropic budget across
    enabled components. New code should call `load_level(component)`.
    """
    levels = load_all_levels()
    any_on = any(lvl in ("medium", "high") for lvl in levels.values())
    max_budget = max(
        (_ANTHROPIC_BUDGET_BY_LEVEL.get(lvl, 0) for lvl in levels.values()),
        default=0,
    )
    return {"enabled": any_on, "budget_tokens": max_budget or 4096}


def save(enabled: bool, budget_tokens: int = 4096) -> dict[str, Any]:
    """Legacy API — preserved for transition.

    Applies a blanket policy: enabled=True sets every component to
    `medium` (or `high` if budget_tokens >= 8192); enabled=False sets
    every component to `low`. New code should call
    `save_level(component, level)`.
    """
    blanket = "low"
    if enabled:
        blanket = "high" if budget_tokens >= 8192 else "medium"
    for c in THINKING_DEFAULTS:
        save_level(c, blanket)
    return load()
