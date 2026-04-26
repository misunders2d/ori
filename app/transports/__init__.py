"""Transports REGISTRY — drop-in extensibility for chat channels.

Each transport is a self-contained subpackage with a `chat.py` or
`poller.py` module that exposes:
- `is_enabled() -> bool` — whether to start this transport given env state.
- `start_poller(get_runner_fn, process_init_fn) -> coroutine` — the entry
  point launched by run_bot.py.

The REGISTRY here is a list of transport submodule names, in priority
order (telegram and slack take precedence over cli). run_bot.py iterates
the list, importing each lazily, and starts the first applicable poller(s).

Adding a new chat: drop `app/transports/<name>/{adapter.py, poller.py}`
(or single-file equivalent), add `<name>` to TRANSPORTS, ensure the
package exposes `is_enabled` and `start_poller`. No surgery elsewhere.
"""

from __future__ import annotations

from importlib import import_module

# Order: messenger transports first, CLI fallback last.
TRANSPORTS: list[str] = ["telegram", "slack", "cli"]


def get_poller_module(name: str):
    """Import a transport's poller submodule lazily.

    Telegram lives at `app.transports.telegram.poller`; CLI at
    `app.transports.cli.chat`. We try both naming conventions.
    """
    base = f"app.transports.{name}"
    for sub in (".poller", ".chat"):
        try:
            return import_module(base + sub)
        except ModuleNotFoundError:
            continue
    raise ModuleNotFoundError(f"Transport '{name}' has no poller or chat module.")


def list_transports() -> list[str]:
    """Names registered, in launch-priority order."""
    return list(TRANSPORTS)


def register_transport(name: str, *, position: int | None = None) -> None:
    """Register a new transport. Position controls the launch order."""
    if name in TRANSPORTS:
        return
    if position is None:
        TRANSPORTS.insert(len(TRANSPORTS) - 1, name)  # before CLI
    else:
        TRANSPORTS.insert(position, name)


__all__ = ["TRANSPORTS", "get_poller_module", "list_transports", "register_transport"]
