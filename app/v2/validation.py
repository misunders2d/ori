"""ScheduleSpec validation entry point — pure, no I/O.

Implements the validator chokepoint described in
``docs/CONTRACTS_V2_DESIGN.md`` §5.5. Every authoring path
(typed ADK tools, template factories, source importers, freeze,
boot self-test) funnels through this function. Phase 2 slice 3
ships the chokepoint plus the rules that depend only on shape
+ in-memory metadata. Later phases add validators that need
runtime data (live source reachability, OAuth status, etc.) by
composing additional rules on top.

Pure module:

- No SQLite, no production DB.
- No HTTP, no Slack/Drive/Telegram clients.
- No registry-singleton mutation. The caller passes an explicit
  :class:`RegistrySnapshot` if registry-aware checks are wanted.
- No worker / wakeup / ADK wiring.

The validator collects ALL issues, not just the first — callers
get the full picture in one pass. ``ValidationResult.ok`` is a
shorthand for "no error-severity issues"; warnings do not
disqualify a spec.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5
- ``docs/PHASE_2_PLAN.md`` slice 3
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.v2.models.execution_plan import ExecutionPlan
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.registry import EmitRegistry, SourceRegistry, ToolRegistry


Severity = Literal["error", "warning"]


# SHA-256 hex (lower case). Matches the storage shape used in
# ``execution_plans.hash`` (no ``sha256:`` prefix — that prefix
# lives on source-snapshot content hashes, not plan hashes).
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Issue / result models
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """A single problem found by the validator.

    ``code`` is the stable identifier authoring tools should
    react to (e.g. show a specific hint, suppress one validator
    in dev mode). ``path`` is a JSONPath-style reference into
    the spec so an editor UI can highlight the failing field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    severity: Severity
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ValidationResult(BaseModel):
    """Aggregate result of :func:`validate_schedule_spec`.

    ``issues`` is the full list in collection order. Helpers
    ``errors`` / ``warnings`` partition by severity.
    """

    model_config = ConfigDict(extra="forbid")

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no error-severity issues were collected."""
        return all(i.severity != "error" for i in self.issues)

    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


@dataclass(frozen=True)
class RegistrySnapshot:
    """Optional read-only handles to the three registries.

    Each field may be ``None`` independently — when ``None``,
    the corresponding lookups are skipped. The validator never
    mutates anything passed here.
    """

    tools: Optional[ToolRegistry] = None
    sources: Optional[SourceRegistry] = None
    emits: Optional[EmitRegistry] = None


# ---------------------------------------------------------------------------
# Per-rule validators. Pure functions returning 0+ issues each.
# Kept as module-level functions (not methods) so they're trivial
# to unit-test and reorder.
# ---------------------------------------------------------------------------


def _validate_hash_present_and_matches(spec: ScheduleSpec) -> list[ValidationIssue]:
    """ScheduleSpec.hash must be non-empty AND equal compute_hash().

    Empty hash is the canonical "draft" sentinel; freezing the
    spec involves calling ``with_fresh_hash()``. The validator
    is meant to be called against frozen specs, so an empty
    hash is an error.
    """
    if not spec.hash:
        return [
            ValidationIssue(
                code="hash_required",
                severity="error",
                path="hash",
                message=(
                    "ScheduleSpec.hash must be set before validation "
                    "(call with_fresh_hash() at freeze time)."
                ),
            )
        ]
    expected = spec.compute_hash()
    if spec.hash != expected:
        return [
            ValidationIssue(
                code="hash_mismatch",
                severity="error",
                path="hash",
                message=(
                    f"ScheduleSpec.hash does not match canonical body "
                    f"(expected {expected!r}, got {spec.hash!r})."
                ),
            )
        ]
    return []


def _validate_execution_plan_hash_format(
    spec: ScheduleSpec,
) -> list[ValidationIssue]:
    """execution_plan_hash, when set, MUST be 64 lowercase hex.

    Phase-1 model accepts any string — this validator tightens
    to the SHA-256 shape so authoring tools fail loud when a
    hand-rolled or partial hash gets pasted in.
    """
    if spec.execution_plan_hash is None:
        return []
    if not _SHA256_HEX.fullmatch(spec.execution_plan_hash):
        return [
            ValidationIssue(
                code="execution_plan_hash_bad_format",
                severity="error",
                path="execution_plan_hash",
                message=(
                    f"execution_plan_hash must be 64 lowercase "
                    f"hex chars (SHA-256); got "
                    f"{spec.execution_plan_hash!r}."
                ),
            )
        ]
    return []


def _validate_reminder_rule(spec: ScheduleSpec) -> list[ValidationIssue]:
    """Reminder-style specs (no ExecutionPlan) are restricted to
    ``OneOffTrigger`` per design decision D5.

    Recurring shapes (cron / interval / event / conditional) need
    a workflow body — an unconditioned cron tick with no
    structured work is almost certainly a mistake.
    """
    if spec.execution_plan_hash is not None:
        return []
    if isinstance(spec.trigger, OneOffTrigger):
        return []
    return [
        ValidationIssue(
            code="missing_execution_plan_for_complex_trigger",
            severity="error",
            path="execution_plan_hash",
            message=(
                f"trigger.type={spec.trigger.type!r} requires an "
                "execution_plan_hash; only OneOffTrigger reminders "
                "may omit it (design D5)."
            ),
        )
    ]


def _validate_execution_plan_hash_exists(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
) -> list[ValidationIssue]:
    """When the caller supplies an in-memory plan index, refuse
    a hash that isn't in the index.

    No DB lookup — the caller is responsible for assembling the
    index from whatever source they own (test fixtures, the
    storage layer, a frozen registration script). When
    ``execution_plans is None`` the validator skips this check
    entirely.
    """
    if spec.execution_plan_hash is None:
        return []
    if execution_plans is None:
        return []
    if spec.execution_plan_hash not in execution_plans:
        return [
            ValidationIssue(
                code="execution_plan_hash_unknown",
                severity="error",
                path="execution_plan_hash",
                message=(
                    f"execution_plan_hash {spec.execution_plan_hash!r} "
                    f"is not present in the supplied plan index "
                    f"({len(execution_plans)} known)."
                ),
            )
        ]
    return []


def _validate_referenced_adapters(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
    registries: Optional[RegistrySnapshot],
) -> list[ValidationIssue]:
    """When BOTH plan body and registries are supplied, walk the
    plan and verify every loader / tool / emit adapter reference
    resolves to a registered descriptor.

    Skipped silently when either side is missing — the validator
    is not the right place to demand that the caller wire up the
    optional plumbing.
    """
    if spec.execution_plan_hash is None:
        return []
    if execution_plans is None or registries is None:
        return []
    plan = execution_plans.get(spec.execution_plan_hash)
    if plan is None:
        # already flagged by _validate_execution_plan_hash_exists
        return []

    issues: list[ValidationIssue] = []

    if registries.sources is not None:
        for i, ip in enumerate(plan.inputs):
            if ip.loader not in registries.sources:
                issues.append(
                    ValidationIssue(
                        code="unknown_source_loader",
                        severity="error",
                        path=f"execution_plan.inputs[{i}].loader",
                        message=(
                            f"loader {ip.loader!r} is not registered "
                            "with the supplied SourceRegistry."
                        ),
                    )
                )

    if registries.tools is not None:
        for ri, rs in enumerate(plan.reasoning):
            for ti, tool_name in enumerate(rs.tools):
                if tool_name not in registries.tools:
                    issues.append(
                        ValidationIssue(
                            code="unknown_tool",
                            severity="error",
                            path=(
                                f"execution_plan.reasoning[{ri}]."
                                f"tools[{ti}]"
                            ),
                            message=(
                                f"tool {tool_name!r} is not "
                                "registered with the supplied "
                                "ToolRegistry."
                            ),
                        )
                    )

    if registries.emits is not None:
        for ei, es in enumerate(plan.emit):
            if es.adapter not in registries.emits:
                issues.append(
                    ValidationIssue(
                        code="unknown_emit_adapter",
                        severity="error",
                        path=f"execution_plan.emit[{ei}].adapter",
                        message=(
                            f"adapter {es.adapter!r} is not "
                            "registered with the supplied "
                            "EmitRegistry."
                        ),
                    )
                )

    return issues


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def validate_schedule_spec(
    spec: ScheduleSpec,
    *,
    execution_plans: Optional[Mapping[str, ExecutionPlan]] = None,
    registries: Optional[RegistrySnapshot] = None,
) -> ValidationResult:
    """Validate ``spec`` against phase-2 schedule rules.

    All rules run independently; the returned
    :class:`ValidationResult` carries every collected issue so
    the caller sees the full picture rather than fixing one
    error at a time.

    Args:
        spec: A frozen ``ScheduleSpec`` (``hash`` populated).
        execution_plans: Optional mapping ``hash → ExecutionPlan``.
            When supplied, the validator (a) refuses a referenced
            plan hash that is not in the mapping and (b) — if
            ``registries`` is also supplied — walks the plan body
            to verify each loader / tool / emit adapter reference
            is registered. The mapping is read-only; never
            mutated.
        registries: Optional :class:`RegistrySnapshot` carrying
            handles to the tool / source / emit registries.
            Adapter-existence checks run only when both this and
            ``execution_plans`` are supplied.

    Returns:
        ``ValidationResult`` whose ``issues`` list contains every
        collected ``ValidationIssue``. ``ok`` is True iff there
        are no error-severity entries.
    """
    issues: list[ValidationIssue] = []
    issues.extend(_validate_hash_present_and_matches(spec))
    issues.extend(_validate_execution_plan_hash_format(spec))
    issues.extend(_validate_reminder_rule(spec))
    issues.extend(_validate_execution_plan_hash_exists(spec, execution_plans))
    issues.extend(
        _validate_referenced_adapters(spec, execution_plans, registries)
    )
    return ValidationResult(issues=issues)


__all__ = [
    "Severity",
    "ValidationIssue",
    "ValidationResult",
    "RegistrySnapshot",
    "validate_schedule_spec",
]
