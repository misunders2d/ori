"""Slack transport package.

Re-exports the adapter for direct use; importing this package does NOT
import the poller (it has runtime dependencies on the executor module).
Callers wanting the poller import explicitly:
    from app.transports.slack.poller import is_enabled, start_poller
"""

from app.transports.slack.adapter import (
    SLACK_API_URL,
    SLACK_HEARTBEAT_FILE,
    SlackAdapter,
    _scrub_secrets,
    _update_heartbeat,
)

__all__ = [
    "SLACK_API_URL",
    "SLACK_HEARTBEAT_FILE",
    "SlackAdapter",
    "_scrub_secrets",
    "_update_heartbeat",
]
