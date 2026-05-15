"""Production Slack transport client for the v2 scheduler.

Phase 9 slice 7 (round-2 reviewer follow-up): the slice-5
worker emit branch dispatches through
:class:`app.v2.emit.slack_reminder.SlackProtocol`, but the
production boot path was constructing every ``Worker`` with
``slack_client=None`` -- which fell back to the phase-4
empty-body path and silently marked reminders ``succeeded``
WITHOUT ever calling ``chat_postMessage``. This module
closes that gap.

:class:`LazySlackTransportClient` conforms to
:class:`~app.v2.emit.slack_reminder.SlackProtocol` and
posts via Slack's HTTP API at emit time. Boot wires a
single instance into every Worker; the actual API call
happens only when the worker picks up a OneOffReminder
Run, by which point the env-loaded
``SLACK_BOT_TOKEN`` is in place.

Behaviour:

- Reads ``SLACK_BOT_TOKEN`` from the environment AT EMIT
  TIME (not at instance construction) so the test
  harness can monkeypatch the env between boot and emit.
- Posts to ``https://slack.com/api/chat.postMessage``
  with the standard bot-token Bearer header.
- Reuses a single :class:`httpx.AsyncClient` (built
  lazily on the first call). The worker emit branch
  treats the adapter's return value as opaque -- any
  exception is caught + wrapped to
  :class:`SlackPostResult` at the adapter layer.
- ``SLACK_BOT_TOKEN`` absent → returns
  ``{"ok": False, "error": "slack_bot_token_unset"}``.
  The worker emit branch then writes the documented
  ``emit_failed`` + ``run_failed`` events per the
  :class:`FailurePolicy` of the firing schedule.
- :meth:`close` is exposed so ``run_bot.py`` can release
  the underlying httpx connection pool during shutdown.

This module is deliberately OUTSIDE :mod:`app.v2.emit` so
the existing import-hygiene pin on ``slack_reminder.py``
(no vendor SDK / no httpx at module load) stays tight.
``httpx`` is allowed here -- this IS the vendor wiring.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

import httpx


_logger = logging.getLogger(__name__)


_SLACK_CHAT_POSTMESSAGE_URL = "https://slack.com/api/chat.postMessage"


class LazySlackTransportClient:
    """SlackProtocol-conforming client backed by httpx.

    Built once at boot; reads ``SLACK_BOT_TOKEN`` from env
    on every call so a hot-reloaded token surfaces without
    re-booting the runtime.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds
            )
        return self._client

    async def chat_postMessage(
        self, *, channel: str, text: str
    ) -> Mapping[str, Any]:
        """POST chat.postMessage; return the parsed Slack
        response. Errors surface as
        ``{"ok": False, "error": ...}`` -- never raise."""
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        if not token:
            return {
                "ok": False,
                "error": "slack_bot_token_unset",
            }
        client = self._get_client()
        try:
            resp = await client.post(
                _SLACK_CHAT_POSTMESSAGE_URL,
                json={"channel": channel, "text": text},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        except Exception as exc:
            _logger.exception(
                "Slack chat_postMessage transport error"
            )
            return {
                "ok": False,
                "error": f"transport_error: {exc}",
            }
        try:
            return resp.json()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"slack_response_not_json: {exc}",
            }

    async def close(self) -> None:
        """Release the underlying httpx connection pool.
        Safe to call against a client that never made a
        request (no pool was created)."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                _logger.exception(
                    "LazySlackTransportClient.close: aclose "
                    "failed (continuing)"
                )
            finally:
                self._client = None


__all__ = ["LazySlackTransportClient"]
