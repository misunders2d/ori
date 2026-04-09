"""Model configuration — separate from the credential vault.

Stores model assignments and cached provider model lists in data/model_config.json.
This is NOT credentials — it's runtime configuration that the agent can safely modify.

Format:
{
  "assignments": {"CoordinatorAgent": "google/gemini-3-flash-preview", ...},
  "model_cache": {
    "google": {"models": [...], "updated": "2026-04-09T12:00:00"},
    "anthropic": {"models": [...], "updated": "2026-04-09T12:00:00"}
  }
}
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.abspath("./data/model_config.json")

# Cache validity: 24 hours
_CACHE_TTL_SECONDS = 86400

# Hardcoded fallback patterns for when cache is empty and API is unreachable
_KNOWN_GOOGLE_PATTERNS = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-embedding-001",
]

_KNOWN_ANTHROPIC_PATTERNS = [
    "claude-sonnet-4-20250514",
    "claude-haiku-4-20250414",
    "claude-opus-4-20250515",
    "claude-3-5-sonnet-v2@20241022",
    "claude-3-5-haiku@20241022",
]


def _read_config() -> dict:
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Model config corrupt: %s — starting fresh", e)
    return {"assignments": {}, "model_cache": {}}


def _write_config(data: dict):
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Model assignments
# ---------------------------------------------------------------------------

def get_assignment(component: str) -> str:
    """Get the model string for a component from config. Returns '' if not set."""
    config = _read_config()
    return config.get("assignments", {}).get(component, "")


def set_assignment(component: str, model_str: str):
    """Persist a model assignment to config and os.environ."""
    config = _read_config()
    if "assignments" not in config:
        config["assignments"] = {}
    config["assignments"][component] = model_str
    _write_config(config)

    # Also set in os.environ for immediate use
    env_key = f"MODEL_{component.upper()}"
    os.environ[env_key] = model_str
    logger.info("Model config: %s = %s", component, model_str)


def get_all_assignments() -> dict:
    """Return all persisted model assignments."""
    config = _read_config()
    return config.get("assignments", {})


def migrate_from_vault():
    """One-time migration: move MODEL_* keys from vault to model_config.json.

    Called once at startup. After migration, removes the keys from the vault.
    """
    try:
        from deploy.vault import get_all as vault_get_all, unset as vault_unset
    except ImportError:
        return

    vault_data = vault_get_all()
    migrated = {}

    for key, value in vault_data.items():
        if key.startswith("MODEL_") and value:
            # Convert MODEL_COORDINATORAGENT -> CoordinatorAgent
            from app.app_utils.models import VALID_COMPONENTS
            for comp in VALID_COMPONENTS:
                if key == f"MODEL_{comp.upper()}":
                    migrated[comp] = value
                    break

    if not migrated:
        return

    config = _read_config()
    if "assignments" not in config:
        config["assignments"] = {}

    for comp, model_str in migrated.items():
        # Only migrate if not already in the new config
        if comp not in config["assignments"]:
            config["assignments"][comp] = model_str

    _write_config(config)

    # Remove from vault
    for key in vault_data:
        if key.startswith("MODEL_"):
            vault_unset(key)

    logger.info("Migrated %d model assignments from vault to model_config.json", len(migrated))


# ---------------------------------------------------------------------------
# Model list cache
# ---------------------------------------------------------------------------

def get_cached_models(provider: str) -> list[str] | None:
    """Get cached model names for a provider. Returns None if cache is stale or missing."""
    config = _read_config()
    cache = config.get("model_cache", {}).get(provider)
    if not cache:
        return None

    updated = cache.get("updated", "")
    if updated:
        try:
            cache_time = datetime.fromisoformat(updated)
            age = (datetime.now(timezone.utc) - cache_time).total_seconds()
            if age > _CACHE_TTL_SECONDS:
                return None  # stale
        except (ValueError, TypeError):
            return None

    return cache.get("models", [])


def update_model_cache(provider: str, model_names: list[str]):
    """Update the cached model list for a provider."""
    config = _read_config()
    if "model_cache" not in config:
        config["model_cache"] = {}
    config["model_cache"][provider] = {
        "models": sorted(set(model_names)),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    _write_config(config)
    logger.info("Model cache updated for %s: %d models", provider, len(model_names))


def get_known_models(provider: str) -> list[str]:
    """Get model names for validation: cached > hardcoded fallback."""
    cached = get_cached_models(provider)
    if cached:
        return cached
    if provider == "google":
        return list(_KNOWN_GOOGLE_PATTERNS)
    if provider == "anthropic":
        return list(_KNOWN_ANTHROPIC_PATTERNS)
    return []
