"""V2 scheduler — authoring tool response model.

Phase 7 slice 1 per ``docs/PHASE_7_PLAN.md`` §3.1 + §5.1.

Every authoring tool returns a :class:`ToolResponse`. The
LLM reads ``status`` first and dispatches on the
per-status payload keys; the tool layer never raises
exceptions back to the agent — failures land as a
``ToolResponse`` whose ``status`` discriminates the cause.

Five statuses:

- ``ok`` — success; ``draft_id`` / ``schedule_id`` / ``spec``
  may carry the documented payload.
- ``validation_failed`` — ``issues`` carries the
  :class:`ValidationIssue` list verbatim.
- ``not_ready`` — the draft is not yet a complete spec;
  ``missing_fields`` lists the unset required fields.
- ``cache_unavailable`` — registry cache absent AND
  refresh failed; ``cache_kind`` + ``network_error`` name
  the cause.
- ``not_found`` — draft id or schedule id does not exist;
  ``message`` carries the operator hint.

References:
- ``docs/PHASE_7_PLAN.md`` §3.1 + §5.1
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.v2.validation import ValidationIssue


ToolResponseStatus = Literal[
    "ok",
    "validation_failed",
    "not_ready",
    "cache_unavailable",
    "not_found",
]


class ToolResponse(BaseModel):
    """Discriminated-union return shape for every authoring
    tool.

    Only the payload fields relevant to the ``status`` are
    populated; others stay ``None``. The model is permissive
    on which fields are set so factories can construct
    responses ergonomically; tests and callers MUST switch on
    ``status`` rather than treating absent fields as a
    boolean.
    """

    model_config = ConfigDict(extra="forbid")

    status: ToolResponseStatus

    # Success payload — relevant to ``status="ok"``.
    draft_id: Optional[str] = None
    schedule_id: Optional[str] = None
    spec: Optional[dict[str, Any]] = None

    # Validation payload — ``status="validation_failed"``.
    issues: Optional[list[ValidationIssue]] = None

    # Not-ready payload — ``status="not_ready"``.
    missing_fields: Optional[list[str]] = None

    # Cache-unavailable payload — ``status="cache_unavailable"``.
    cache_kind: Optional[str] = None
    network_error: Optional[str] = None

    # Operator hint — populated on ``status="not_found"`` and
    # on ``status="ok"`` when the tool wants to surface a
    # "already-applied" or similar no-op message.
    message: Optional[str] = None

    # ---- Factory helpers ----

    @classmethod
    def ok(
        cls,
        *,
        draft_id: Optional[str] = None,
        schedule_id: Optional[str] = None,
        spec: Optional[dict[str, Any]] = None,
        message: Optional[str] = None,
    ) -> "ToolResponse":
        return cls(
            status="ok",
            draft_id=draft_id,
            schedule_id=schedule_id,
            spec=spec,
            message=message,
        )

    @classmethod
    def validation_failed(
        cls,
        *,
        issues: list[ValidationIssue],
    ) -> "ToolResponse":
        return cls(status="validation_failed", issues=issues)

    @classmethod
    def not_ready(
        cls,
        *,
        missing_fields: list[str],
    ) -> "ToolResponse":
        return cls(
            status="not_ready", missing_fields=missing_fields
        )

    @classmethod
    def cache_unavailable(
        cls,
        *,
        kind: str,
        network_error: str,
    ) -> "ToolResponse":
        return cls(
            status="cache_unavailable",
            cache_kind=kind,
            network_error=network_error,
        )

    @classmethod
    def not_found(
        cls,
        *,
        message: str,
    ) -> "ToolResponse":
        return cls(status="not_found", message=message)


__all__ = ["ToolResponse", "ToolResponseStatus"]
