"""Telegram transport package.

Re-exports the adapter for direct use; importing this package does NOT
trigger the poller's import (poller has runtime dependencies that exist
only after Phase G). Callers wanting the poller import explicitly:
    from app.transports.telegram.poller import poll_telegram, is_enabled
"""

from app.transports.telegram.adapter import (
    TELEGRAM_API,
    TELEGRAM_FILE_API,
    TelegramAdapter,
    _scrub_secrets,
    _update_heartbeat,
)


__all__ = [
    "TelegramAdapter",
    "TELEGRAM_API",
    "TELEGRAM_FILE_API",
    "_scrub_secrets",
    "_update_heartbeat",
]
