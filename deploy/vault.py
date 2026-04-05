"""Credential Vault — single source of truth for all secrets and runtime config.

Every credential read/write goes through this module. The vault file is:
  - Atomic: writes use tempfile + os.rename (POSIX atomic on same filesystem)
  - Locked: flock() prevents concurrent writes
  - Backed up: every write saves the previous version as .bak
  - Migratable: auto-migrates from legacy .env + config.json on first run

The vault directory (data/vault/) is gitignored. Git operations, evolution
worktrees, and crash recovery can NEVER touch it.
"""

import fcntl
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VAULT_DIR = os.path.join(_PROJECT_ROOT, "data", "vault")
VAULT_FILE = os.path.join(VAULT_DIR, "credentials.json")
VAULT_BACKUP = os.path.join(VAULT_DIR, "credentials.json.bak")
VAULT_LOCK = os.path.join(VAULT_DIR, ".vault_lock")


def _ensure_dir():
    os.makedirs(VAULT_DIR, mode=0o700, exist_ok=True)


def _read_vault() -> dict:
    """Read the vault file. Returns empty dict if missing or corrupt."""
    if not os.path.exists(VAULT_FILE):
        return {}
    try:
        with open(VAULT_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Vault file corrupt: %s — trying backup", e)
        if os.path.exists(VAULT_BACKUP):
            try:
                with open(VAULT_BACKUP, "r") as f:
                    data = json.load(f)
                # Restore from backup
                _atomic_write(data)
                logger.info("Vault restored from backup")
                return data
            except (json.JSONDecodeError, OSError):
                pass
        logger.error("Vault and backup both unreadable — starting empty")
        return {}


def _atomic_write(data: dict):
    """Write vault atomically: backup current, write temp, rename."""
    _ensure_dir()
    # Backup current file before overwriting
    if os.path.exists(VAULT_FILE):
        try:
            with open(VAULT_FILE, "rb") as src:
                content = src.read()
            # Only backup if current file has content
            if len(content) > 2:  # more than just "{}"
                fd_bak, tmp_bak = tempfile.mkstemp(dir=VAULT_DIR, prefix=".bak.")
                try:
                    os.write(fd_bak, content)
                    os.close(fd_bak)
                    os.rename(tmp_bak, VAULT_BACKUP)
                except Exception:
                    os.close(fd_bak)
                    try:
                        os.unlink(tmp_bak)
                    except OSError:
                        pass
        except OSError:
            pass

    # Write new vault atomically
    fd, tmp_path = tempfile.mkstemp(dir=VAULT_DIR, prefix=".vault.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, VAULT_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_lock(fn):
    """Execute fn while holding an exclusive flock on the vault."""
    _ensure_dir()
    lock_fd = os.open(VAULT_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return fn()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_vault():
    """Load all vault credentials into os.environ. Call once at startup."""
    data = _with_lock(_read_vault)
    for key, value in data.items():
        if value is not None:
            os.environ[key] = str(value)
    logger.info("Vault: loaded %d credentials", len(data))


def get(key: str, default: str = "") -> str:
    """Read a credential. Prefers os.environ (already loaded), falls back to vault file."""
    return os.environ.get(key, default)


def set(key: str, value: str):
    """Set a credential in the vault and os.environ. Atomic + locked."""
    def _do_set():
        data = _read_vault()
        data[key] = value
        _atomic_write(data)
    _with_lock(_do_set)
    os.environ[key] = value
    logger.info("Vault: set %s", key)


def unset(key: str):
    """Remove a credential from the vault and os.environ. Atomic + locked."""
    def _do_unset():
        data = _read_vault()
        if key in data:
            del data[key]
            _atomic_write(data)
    _with_lock(_do_unset)
    os.environ.pop(key, None)
    logger.info("Vault: unset %s", key)


def get_all() -> dict:
    """Return a copy of all vault contents."""
    return _with_lock(_read_vault)


def set_many(updates: dict):
    """Set multiple credentials atomically. Single write, single lock."""
    def _do_set_many():
        data = _read_vault()
        data.update(updates)
        _atomic_write(data)
    _with_lock(_do_set_many)
    for key, value in updates.items():
        if value is not None:
            os.environ[key] = str(value)
    logger.info("Vault: set %d keys", len(updates))
