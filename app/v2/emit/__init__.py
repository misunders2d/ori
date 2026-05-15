"""V2 scheduler — emit adapters package.

Phase 9 ships the first emit adapter (Slack reminder)
per ``docs/PHASE_9_PLAN.md`` §1.4.

Emit adapters are the thin layer between the worker's
claim → execute pipeline and the actual transport call
(Slack chat.postMessage, Telegram sendMessage, etc.).
Each adapter is protocol-typed DI; no vendor SDK is
imported at module load.

Phase 9 surface:
- :class:`SlackPostResult` — typed adapter result.
- :class:`SlackProtocol` — minimal Slack adapter
  surface (``chat_postMessage`` only).
- :func:`emit_reminder_to_slack` — async emit body for
  the OneOffReminder template.

Future phases add Telegram emit + email emit + emit
retry chains.
"""

from app.v2.emit.slack_reminder import (
    SlackPostResult,
    SlackProtocol,
    emit_reminder_to_slack,
)


__all__ = [
    "SlackPostResult",
    "SlackProtocol",
    "emit_reminder_to_slack",
]
