"""V2 scheduler — source content emit adapter.

Phase 11 slice 5 per ``docs/PHASE_11_PLAN.md`` §3.2 / §4
slice 5 + the claude-reviewer slice-5 hard-checks.

Public surface:

- :class:`SourcePostResult` — typed adapter return shape
  (``skipped_unchanged`` is consumed by the slice-6
  ``skip_unchanged`` path; slice 5 always leaves it
  ``False``).
- :func:`emit_source_to_slack` — async emit body for a
  source-driven ExecutionPlan's ``source_post`` step.

Q3 (reviewer): this is a NEW module — NOT a generalisation
of the phase-9 OneOff emitter. ``app.v2.emit.slack_reminder``
/ ``emit_reminder_to_slack`` are byte-untouched; only the
shared :class:`SlackProtocol` *type* is reused. No vendor
SDK is imported at module load — the Slack surface is
Protocol-typed DI (mirrors the phase-9/10 no-SDK-at-
module-load pin).

Behaviour:

- The Slack channel comes from the ``source_post``
  :class:`EmitStep` ``args`` (template-declared — NOT
  ``spec.delivery``).
- VERBATIM bytes (design §5.3.7): the resolved
  ``content_bytes`` are the canonical utf-8 of the source
  text; they are decoded 1:1 and posted with NO reformat
  beyond the template-declared envelope. Slice 5 ships
  ``progress_strategy="whole"`` only — there is no
  envelope and ``progress_strategy`` is not read here
  (``skip_unchanged`` lands slice 6).
- The adapter NEVER raises — any client failure is wrapped
  to ``ok=False`` so the worker emit branch sees a single
  return shape and routes via
  ``Worker._route_source_failure_policy`` (the slice-2
  shared atomic core).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.2, §5.3.7
- ``docs/PHASE_11_PLAN.md`` §3.2
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from pydantic import BaseModel, ConfigDict

from app.v2.emit.slack_reminder import SlackProtocol
from app.v2.models.execution_plan import ExecutionPlan
from app.v2.models.schedule import ScheduleSpec

if TYPE_CHECKING:  # type-only — no runtime import of the
    # resolver here (keeps this module's load light + the
    # AST hygiene pin tight; the worker already owns the
    # resolver import).
    from app.v2.sources.resolver import ResolveOutcome


#: The registered emit-adapter name a source-driven
#: ExecutionPlan declares for Slack delivery (the template
#: compiles ``EmitStep(adapter=SOURCE_POST_ADAPTER, ...)``).
SOURCE_POST_ADAPTER = "source_post"


class SourcePostResult(BaseModel):
    """Adapter result. The worker emit branch reads this
    and writes the appropriate Run + Event rows.

    ``skipped_unchanged`` — slice-6 ``skip_unchanged``
    no-op-success signal (``True`` ⇒ the source content
    was unchanged so nothing was posted; the run still
    succeeds). Slice 5 always leaves it ``False``.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    channel: str
    ts: Optional[str] = None
    error: Optional[str] = None
    skipped_unchanged: bool = False


async def emit_source_to_slack(
    *,
    spec: ScheduleSpec,
    plan: ExecutionPlan,
    resolved: "dict[str, ResolveOutcome]",
    slack_client: SlackProtocol,
    clock: Callable[[], datetime],
) -> SourcePostResult:
    """Post a source-driven plan's resolved content to its
    ``source_post`` Slack channel, VERBATIM.

    Args:
        spec: Frozen ScheduleSpec for the firing Run
            (reserved for parity with the OneOff emitter;
            channel comes from the EmitStep, not delivery).
        plan: The loaded ExecutionPlan; its ``source_post``
            EmitStep carries the target ``channel`` in
            ``args``.
        resolved: ``{InputSpec.id: ResolveOutcome}`` for the
            RESOLVED / DRIFT(alert) source inputs. Phase-11
            (11A) ships exactly ONE source input.
        slack_client: Protocol-typed Slack adapter (DI).
        clock: Tz-aware UTC clock; reserved for future
            per-call timing (parity with the OneOff
            emitter), not consumed in slice 5.

    Returns:
        :class:`SourcePostResult`. Never raises.
    """
    _unused_clock = clock  # noqa: F841 — reserved (phase parity)
    _unused_spec = spec  # noqa: F841 — reserved (phase parity)

    emit_step = next(
        (e for e in plan.emit if e.adapter == SOURCE_POST_ADAPTER),
        None,
    )
    if emit_step is None:
        return SourcePostResult(
            ok=False,
            channel="",
            error=(
                f"ExecutionPlan {plan.id!r} has no "
                f"{SOURCE_POST_ADAPTER!r} emit step"
            ),
        )

    channel = emit_step.args.get("channel")
    if not isinstance(channel, str) or not channel:
        return SourcePostResult(
            ok=False,
            channel=channel if isinstance(channel, str) else "",
            error=(
                f"{SOURCE_POST_ADAPTER} emit step args missing "
                f"a non-empty 'channel'"
            ),
        )

    # Phase-11 (11A): exactly one resolved source input.
    if len(resolved) != 1:
        return SourcePostResult(
            ok=False,
            channel=channel,
            error=(
                f"phase-11 (11A) expects exactly one resolved "
                f"source input; got {len(resolved)}"
            ),
        )

    outcome = next(iter(resolved.values()))
    content_bytes = outcome.content_bytes
    if content_bytes is None:
        return SourcePostResult(
            ok=False,
            channel=channel,
            error="resolved outcome carried no content_bytes to emit",
        )

    # VERBATIM (§5.3.7): the canonical content_bytes ARE the
    # utf-8 of the source text. Decode 1:1, NO reformat
    # (slice 5 = progress_strategy 'whole'; no envelope).
    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return SourcePostResult(
            ok=False,
            channel=channel,
            error=f"content_bytes is not utf-8 text: {exc}",
        )

    try:
        response = await slack_client.chat_postMessage(
            channel=channel,
            text=text,
        )
    except Exception as exc:  # noqa: BLE001 — wrap any client failure
        return SourcePostResult(
            ok=False,
            channel=channel,
            error=str(exc),
        )

    # Slack API contract: success ONLY when `ok` is the
    # literal True bool (identity check — a truthy non-bool
    # MUST NOT masquerade as ok; mirrors the phase-9
    # slice-4 reviewer fix).
    ok = response.get("ok") is True
    ts = response.get("ts")
    error = response.get("error") if not ok else None
    return SourcePostResult(
        ok=ok,
        channel=channel,
        ts=ts if isinstance(ts, str) else None,
        error=error
        if isinstance(error, str)
        else (
            None
            if ok
            else "slack response carried ok=False with no error string"
        ),
    )


__all__ = [
    "SOURCE_POST_ADAPTER",
    "SourcePostResult",
    "emit_source_to_slack",
]
