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

from app.v2.enums import EnforcementMode
from app.v2.models.execution_plan import ExecutionPlan
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.reasoning_enforcement import evaluate_reasoning_step
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


def _validate_plan_body_integrity(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
) -> list[ValidationIssue]:
    """When the caller supplies a plan body for the spec's
    referenced hash, verify it is internally consistent.

    Three failure modes covered:

    - ``execution_plan_body_hash_missing`` — ``plan.hash`` empty.
      A plan that hasn't been frozen has no business being
      treated as the body of a frozen ScheduleSpec.
    - ``execution_plan_body_hash_mismatch_spec`` — the plan's
      own ``hash`` doesn't equal the spec's
      ``execution_plan_hash``. This catches a wrong mapping key
      (caller bug) or a spec pointing at the wrong revision
      (drift).
    - ``execution_plan_body_hash_mismatch_self`` — the plan's
      ``hash`` doesn't equal ``plan.compute_hash()`` over the
      same body. This catches tampering / stale serialisation
      that would let the registry walk see a body different
      from the one the hash claims to identify.

    Skipped silently when the plan body isn't supplied or the
    spec has no execution_plan_hash at all. Empty plan.hash
    short-circuits the downstream comparisons (they would be
    nonsensical against a missing value).
    """
    if spec.execution_plan_hash is None or execution_plans is None:
        return []
    plan = execution_plans.get(spec.execution_plan_hash)
    if plan is None:
        return []  # existence check has already flagged this

    issues: list[ValidationIssue] = []
    if not plan.hash:
        issues.append(
            ValidationIssue(
                code="execution_plan_body_hash_missing",
                severity="error",
                path="execution_plan.hash",
                message=(
                    f"ExecutionPlan body for spec hash "
                    f"{spec.execution_plan_hash!r} has an empty "
                    "``hash`` — call with_fresh_hash() on the plan "
                    "before freezing the schedule."
                ),
            )
        )
        return issues

    if plan.hash != spec.execution_plan_hash:
        issues.append(
            ValidationIssue(
                code="execution_plan_body_hash_mismatch_spec",
                severity="error",
                path="execution_plan.hash",
                message=(
                    f"ExecutionPlan.hash {plan.hash!r} does not "
                    f"match spec.execution_plan_hash "
                    f"{spec.execution_plan_hash!r}. The mapping "
                    "key and the body's own hash must agree — "
                    "otherwise the spec is pinned to one revision "
                    "while validation walks another."
                ),
            )
        )

    recomputed = plan.compute_hash()
    if plan.hash != recomputed:
        issues.append(
            ValidationIssue(
                code="execution_plan_body_hash_mismatch_self",
                severity="error",
                path="execution_plan.hash",
                message=(
                    f"ExecutionPlan.hash {plan.hash!r} does not "
                    f"match plan.compute_hash() {recomputed!r}. "
                    "The body has been tampered with or serialised "
                    "stale — refuse to trust adapter references "
                    "inside it."
                ),
            )
        )

    return issues


def _validate_plan_enforcement_mode(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
) -> list[ValidationIssue]:
    """ExecutionPlan model accepts PERMISSIVE; the freeze pathway
    rejects it. The chokepoint is THIS validator (per the
    ExecutionPlan docstring).
    """
    if spec.execution_plan_hash is None or execution_plans is None:
        return []
    plan = execution_plans.get(spec.execution_plan_hash)
    if plan is None:
        return []
    if plan.enforcement != EnforcementMode.STRICT:
        return [
            ValidationIssue(
                code="execution_plan_enforcement_not_strict",
                severity="error",
                path="execution_plan.enforcement",
                message=(
                    f"ExecutionPlan.enforcement must be "
                    f"{EnforcementMode.STRICT.value!r} at freeze "
                    f"time; got {plan.enforcement.value!r}."
                ),
            )
        ]
    return []


def _validate_plan_reasoning_output_types(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
) -> list[ValidationIssue]:
    """``OutputSpec.type='none'`` is accepted by the model so
    test fixtures can exercise it; the rigor validator (this
    one) forbids it in frozen plans per the OutputSpec
    docstring.
    """
    if spec.execution_plan_hash is None or execution_plans is None:
        return []
    plan = execution_plans.get(spec.execution_plan_hash)
    if plan is None:
        return []
    issues: list[ValidationIssue] = []
    for ri, step in enumerate(plan.reasoning):
        if step.output.type == "none":
            issues.append(
                ValidationIssue(
                    code="reasoning_output_type_forbidden",
                    severity="error",
                    path=(
                        f"execution_plan.reasoning[{ri}].output.type"
                    ),
                    message=(
                        f"ReasoningStep[{ri}] declares "
                        "output.type='none' — frozen plans require "
                        "'json' or 'text' so post-LLM validation "
                        "has something to check against."
                    ),
                )
            )
    return issues


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


def _validate_reasoning_tool_mode(
    spec: ScheduleSpec,
    execution_plans: Optional[Mapping[str, ExecutionPlan]],
    registries: Optional[RegistrySnapshot],
) -> list[ValidationIssue]:
    """Phase-12 §12 step-12 — read-only-reasoning enforcement at
    the §5.5 chokepoint (Q6: ONE independent additive rule).

    **§11.1 backward-compat.** Iterates ONLY reasoning-bearing
    plans' reasoning steps. A spec with no
    ``execution_plan_hash``, no plan body in the index, or an
    emit-only plan (``reasoning == []``) is an EARLY NO-OP — so
    every shipped phase-9–11 OneOff / ``RecurringSeriesFromSource``
    emit spec is byte/behaviour-unchanged (they carry no
    reasoning).

    **Q2 / §1.1b fail-safe.** Tool→tag resolution rides
    ``RegistrySnapshot.tools`` (the same seam
    :func:`_validate_referenced_adapters` uses). With
    ``registries`` / ``registries.tools`` ``None`` — the state
    of EVERY shipped call site (§1.1a registry-threading map) —
    every referenced tool resolves to ``None`` ⇒ a
    ``read_only`` reasoning-bearing plan is blanket-BLOCKED via
    ``reasoning_tool_unresolved``. This is the INTENDED,
    documented, over-block-safe degeneration (defense-in-depth
    with the phase-11 worker ``_fail_run`` boundary), NEVER a
    silent allow. ``ToolRegistry.lookup`` — NOT
    ``ToolRegistry.tags_for`` — is used so an unknown tool
    resolves to ``None`` (the distinct *unresolved* code), not
    the fail-safe ``{write_external}`` set (which would conflate
    it with the *blocked* code).

    **Non-short-circuiting (Q6).** Returns its OWN issue list;
    iterates EVERY reasoning step and EVERY offending tool, so a
    malformed plan still surfaces ALL issues (the
    all-issues-collected contract). The block decision reuses
    the slice-1 :func:`evaluate_reasoning_step` — the §5.4
    blocking-tag set is NOT re-declared here.
    """
    if spec.execution_plan_hash is None:
        return []
    if execution_plans is None:
        return []
    plan = execution_plans.get(spec.execution_plan_hash)
    if plan is None:
        # Already flagged by _validate_execution_plan_hash_exists.
        return []
    if not plan.reasoning:
        # §11.1: emit-only / no-reasoning plan — early no-op.
        return []

    tools_reg = registries.tools if registries is not None else None

    def _resolve(name: str):
        # None ⇒ unresolved (fail-safe BLOCK). lookup (NOT
        # tags_for) so an unknown tool is None, distinct from a
        # genuinely write-tagged registered tool.
        if tools_reg is None:
            return None
        descriptor = tools_reg.lookup(name)
        if descriptor is None:
            return None
        return frozenset(descriptor.tags)

    issues: list[ValidationIssue] = []
    for ri, step in enumerate(plan.reasoning):
        outcome = evaluate_reasoning_step(
            step, resolve_tags=_resolve
        )
        if outcome.allowed:
            continue
        path = f"execution_plan.reasoning[{ri}].tools"
        for tool_name, tags in outcome.blocked_tools:
            issues.append(
                ValidationIssue(
                    code="reasoning_tool_write_in_read_only",
                    severity="error",
                    path=path,
                    message=(
                        f"reasoning step {step.id!r} is "
                        f"tool_mode=read_only but references "
                        f"{tool_name!r} carrying write-capable "
                        f"tag(s) {sorted(t.value for t in tags)!r}; "
                        f"writes must route through an emit "
                        f"adapter, or the step must opt in with "
                        f"tool_mode=write_allowed (design §5.4 / "
                        f"D6)"
                    ),
                )
            )
        for tool_name in outcome.unresolved_tools:
            issues.append(
                ValidationIssue(
                    code="reasoning_tool_unresolved",
                    severity="error",
                    path=path,
                    message=(
                        f"reasoning step {step.id!r} "
                        f"(tool_mode=read_only) references "
                        f"{tool_name!r} which is unregistered or "
                        f"untagged; §5.4 fail-safe treats it "
                        f"write-capable and BLOCKS it (tool "
                        f"registry "
                        f"{'absent' if tools_reg is None else 'present'})"
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
    issues.extend(_validate_plan_body_integrity(spec, execution_plans))
    issues.extend(_validate_plan_enforcement_mode(spec, execution_plans))
    issues.extend(
        _validate_plan_reasoning_output_types(spec, execution_plans)
    )
    issues.extend(
        _validate_referenced_adapters(spec, execution_plans, registries)
    )
    issues.extend(
        _validate_reasoning_tool_mode(spec, execution_plans, registries)
    )
    return ValidationResult(issues=issues)


__all__ = [
    "Severity",
    "ValidationIssue",
    "ValidationResult",
    "RegistrySnapshot",
    "validate_schedule_spec",
]
