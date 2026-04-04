"""Mutable runtime configuration backed by data/config.json.

.env is READ-ONLY at runtime — it's only written by the setup wizard and
launcher.sh (before the container starts). All runtime config changes go here.

On startup, config.json values are merged into os.environ (overriding .env).
This file is safe from os._exit() corruption because writes use atomic
tempfile+rename (POSIX rename is atomic on the same filesystem).
"""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.abspath("./data/config.json")


def _atomic_write(path: str, data: dict):
    """Write JSON atomically: tempfile → rename (POSIX atomic)."""
    dir_path = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_config() -> dict:
    """Load config.json, returning empty dict if missing or corrupt."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("config.json corrupt or unreadable: %s — starting fresh", e)
        return {}


def load_config_into_env():
    """Merge config.json values into os.environ (overrides .env values)."""
    config = load_config()
    for key, value in config.items():
        if value is not None:
            os.environ[key] = str(value)
        elif key in os.environ:
            del os.environ[key]


def set_config(key: str, value: str):
    """Set a single key in config.json and os.environ. Atomic write."""
    config = load_config()
    config[key] = value
    _atomic_write(CONFIG_PATH, config)
    os.environ[key] = value
    logger.info("Config: set %s", key)


def unset_config(key: str):
    """Remove a key from config.json and os.environ. Atomic write."""
    config = load_config()
    if key in config:
        del config[key]
        _atomic_write(CONFIG_PATH, config)
    os.environ.pop(key, None)
    logger.info("Config: unset %s", key)


def get_config(key: str, default: str = "") -> str:
    """Read a key from os.environ (which has config.json merged in)."""
    return os.environ.get(key, default)
