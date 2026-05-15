"""V2 scheduler — Slack reminder emit adapter.

Phase 9 slice 4 per ``docs/PHASE_9_PLAN.md`` §1.4 + §3.4.

Public surface:

- :class:`SlackPostResult` — typed adapter return shape.
- :class:`SlackProtocol` — minimal Slack surface
  (``chat_postMessage`` only). Production wires the
  upstream Slack SDK's ``AsyncWebClient.chat_postMessage``
  under this Protocol; tests pass an in-memory stub. No
  vendor SDK is imported at module load.
- :func:`emit_reminder_to_slack` — async emit body for
  the OneOffReminder template.

The adapter does NOT touch the EventLedger. The worker
emit branch (slice 5) reads :class:`SlackPostResult` and
appends ``run_succeeded`` / ``run_failed`` per the
:class:`FailurePolicy`.

Failure handling: any exception raised by the protocol
implementation is caught + wrapped to
``SlackPostResult(ok=False, channel=..., ts=None,
error=str(exc))``. The adapter never raises to its caller
— the worker emit branch can rely on a single return
shape.

The ``clock`` argument is reserved for future use
(per-call timing / future retry decisions). Phase 9 does
not consume it but keeps the parameter so the calling
contract stays stable across phases.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.2 (template-first
  authoring), §7.2 (L1/L2/L3 delivery fallback — phase
  9 implements L1 only).
- ``docs/PHASE_9_PLAN.md`` §1.4 + §3.4 + §5.3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict

from app.v2.models.schedule import ScheduleSpec


# ---------------------------------------------------------------------------
# Protocol — no vendor SDK at module load
# ---------------------------------------------------------------------------


class SlackProtocol(Protocol):
    """Minimal Slack adapter surface this adapter depends
    on. Production wires the upstream Slack SDK's
    ``AsyncWebClient.chat_postMessage`` under this
    Protocol; tests pass an in-memory stub.
    """

    def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
    ) -> Awaitable[Mapping[str, Any]]:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class SlackPostResult(BaseModel):
    """Adapter result. The worker emit branch reads this
    and writes the appropriate Run + Event rows.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    channel: str
    ts: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Emit body
# ---------------------------------------------------------------------------


async def emit_reminder_to_slack(
    *,
    spec: ScheduleSpec,
    slack_client: SlackProtocol,
    clock: Callable[[], datetime],
) -> SlackPostResult:
    """Send a OneOffReminder spec's text payload to its
    Slack delivery target.

    Behaviour:

    - Reads ``spec.delivery.target_session_id`` as the
      Slack channel id.
    - Reads ``spec.template.args["text"]`` as the
      message body. Missing template / missing args /
      missing ``text`` key → ``SlackPostResult(ok=False,
      channel=..., error="template.args missing
      'text'")``. The adapter does NOT raise — the worker
      branch sees a uniform return shape.
    - Calls
      ``slack_client.chat_postMessage(channel=..., text=...)``.
    - Slack response is treated as ok=True iff the
      returned mapping carries an ``"ok": True``
      (canonical Slack response shape). Any other shape
      / missing key surfaces as ok=False with a
      descriptive ``error`` string.
    - Any exception raised by the protocol implementation
      is caught + wrapped to ``ok=False, error=str(exc)``.

    Args:
        spec: Frozen ScheduleSpec for the firing Run.
        slack_client: Protocol-typed Slack adapter.
        clock: Tz-aware UTC clock. Phase 9 does not use
            this; reserved for future per-call timing /
            retry decisions so the calling contract is
            stable across phases.

    Returns:
        :class:`SlackPostResult`.
    """
    # ``clock`` reserved for future use; unused in phase 9.
    _unused_clock = clock  # noqa: F841

    channel = spec.delivery.target_session_id

    template = spec.template
    if template is None:
        return SlackPostResult(
            ok=False,
            channel=channel,
            ts=None,
            error="spec.template is None; OneOffReminder emit requires a template",
        )
    args = template.args
    if not args or "text" not in args:
        return SlackPostResult(
            ok=False,
            channel=channel,
            ts=None,
            error="template.args missing 'text'",
        )
    text = args["text"]
    if not isinstance(text, str):
        return SlackPostResult(
            ok=False,
            channel=channel,
            ts=None,
            error=(
                f"template.args['text'] must be str; got "
                f"{type(text).__name__}"
            ),
        )

    try:
        response = await slack_client.chat_postMessage(
            channel=channel,
            text=text,
        )
    except Exception as exc:  # noqa: BLE001 — wrap any client failure
        return SlackPostResult(
            ok=False,
            channel=channel,
            ts=None,
            error=str(exc),
        )

    # Slack API contract: success ONLY when `ok` is the
    # literal True bool. Truthy non-bool values ("true"
    # string, 1 int, "yes", etc.) MUST NOT map to success
    # — Slack never sends those, and accepting them would
    # let a malformed / spoofed response masquerade as ok.
    # Use identity check (`is True`) so the type narrows
    # to bool, not the looser truthy semantics of
    # ``bool(...)`` (round-1 reviewer slice-4 bug fix).
    ok = response.get("ok") is True
    ts = response.get("ts")
    error = response.get("error") if not ok else None
    return SlackPostResult(
        ok=ok,
        channel=channel,
        ts=ts if isinstance(ts, str) else None,
        error=error if isinstance(error, str) else (
            None if ok else "slack response carried ok=False with no error string"
        ),
    )


__all__ = [
    "SlackPostResult",
    "SlackProtocol",
    "emit_reminder_to_slack",
]
