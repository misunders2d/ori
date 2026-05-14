#!/usr/bin/env python3
"""Install version-controlled git hooks (idempotent).

Sets `git config core.hooksPath .githooks` so this clone uses the hooks
checked in under `.githooks/` — currently:

- pre-commit doc-read gate (Phase 8 of the v1 build-out)
- v2 scheduler phase scope guard (invoked from pre-commit;
  inert unless `.v2-current-phase` is present at the repo root)

Safe to call from anywhere — re-running on an already-configured clone
is a no-op. Called from:

- `scripts/install_hooks.py` directly (CLI)
- `run_bot.py` startup (so deployed instances pick it up on first run)
- `deploy/ori-supervisor.py` boot sequence (same reason)
- `interfaces/setup_wizard.py` (so fresh installs get it)
"""

from __future__ import annotations

import pathlib
import subprocess
import sys


def _project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def install_hooks(silent: bool = False) -> bool:
    """Set core.hooksPath if not already pointed at .githooks/.

    Returns True if the path is now `.githooks` (whether we set it or it
    was already there). Returns False on any failure (no git, etc) —
    failures are non-fatal because the bot still works without the hook,
    just without the doc-read enforcement on direct host commits.
    """
    root = _project_root()
    hooks_dir = root / ".githooks"
    if not hooks_dir.is_dir():
        if not silent:
            print(f"ERROR: {hooks_dir} does not exist — nothing to install.", file=sys.stderr)
        return False

    try:
        current = subprocess.run(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=root, capture_output=True, text=True,
        ).stdout.strip()
    except FileNotFoundError:
        if not silent:
            print("git CLI not found; skipping hook install.", file=sys.stderr)
        return False

    if current == ".githooks":
        if not silent:
            print("core.hooksPath already set to .githooks — no-op.")
        return True

    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as e:
        if not silent:
            print(f"git config failed: {e.stderr.strip()}", file=sys.stderr)
        return False

    if not silent:
        print("✓ core.hooksPath set to .githooks (pre-commit doc-read gate active).")
    return True


if __name__ == "__main__":
    ok = install_hooks(silent="--silent" in sys.argv)
    sys.exit(0 if ok else 1)
