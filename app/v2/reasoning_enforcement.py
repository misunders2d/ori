"""Pure read-only-reasoning / emit-only-writes enforcement composition.

Implements ``docs/CONTRACTS_V2_DESIGN.md`` §12 step 12 +
``docs/PHASE_12_PLAN.md`` §1 item 1 / §3 + the claude-reviewer
slice-1 hard-checks.

This is the PURE composition layer (Q1 = (a), build-the-layer):
given a ``ReasoningStep`` (its ``tool_mode`` + the resolved
capability-tag set of each referenced tool) it returns a typed
allow / block outcome. It is built ON
:func:`app.v2.tool_tags.is_blocked_by_read_only_reasoning` — it
does NOT re-declare the blocking-tag set, so the §5.4 source of
truth (``tool_tags._READ_ONLY_BLOCKING_TAGS``) stays single. A
drift-guard in ``tests/v2/test_tool_tags.py`` pins that this
layer's decision never diverges from that helper.

No I/O. No ``datetime.now`` / ``uuid4`` / vendor SDK at module
load. ZERO sources / resolver / cache / worker touch — slice 1
is the pure layer only; it is NOT yet wired into
``validate_schedule_spec`` (slice 2) or the worker seam
(slice 4).

**SECURITY fail-safe (§5.4 / PHASE_12_PLAN §1.1b, Q2 binding
condition).** A referenced tool whose tag set resolves to
``None`` (the tool is unregistered) OR to an empty set (the
tool is registered but carries no tags) is treated
write-capable and BLOCKED under ``tool_mode=read_only`` — it is
NEVER silently allowed. The §5.4 default for an unknown /
untagged tool is ``write_external`` (fail-safe), so the
over-block-safe decision here is the secure one. Such tools are
reported in ``unresolved_tools`` so the validation layer
(slice 2) can emit a distinct issue code.

``tool_mode=write_allowed`` ⇒ ``allowed=True`` here: the
admin-approval friction for a ``write_allowed`` step is the
authoring layer's concern (Q5, slice 3), not this pure block
decision.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4, §12 step 12, D6
- ``docs/PHASE_12_PLAN.md`` §1, §1.1, §3, §9 (Q1–Q6)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Callable,
    Iterable,
    Optional,
)

from app.v2.enums import ToolMode
from app.v2.tool_tags import (
    ToolCapabilityTag,
    is_blocked_by_read_only_reasoning,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.v2.models.execution_plan import ReasoningStep


#: ``resolve_tags(tool_name)`` → the tool's capability-tag set,
#: or ``None`` when the tool is unknown. A ``None`` return OR an
#: empty set is the §5.4 fail-safe case (treated write-capable →
#: BLOCKED under ``read_only``; never silent-allow).
ResolveTags = Callable[
    [str], Optional[AbstractSet[ToolCapabilityTag]]
]


@dataclass(frozen=True)
class ReasoningEnforcementOutcome:
    """Typed result of evaluating one ``ReasoningStep`` against
    the read-only-reasoning policy.

    - ``allowed`` — True iff the step may run as written.
    - ``blocked_tools`` — ``(tool_name, frozenset(tags))`` for
      each referenced tool whose RESOLVED tags overlap the
      §5.4 read-only-blocking set (``write_external`` /
      ``send_message`` / ``filesystem_write`` / ``db_write`` /
      ``privileged``).
    - ``unresolved_tools`` — referenced tool names whose tags
      resolved to ``None`` OR an empty set (unregistered OR
      untagged). Under ``read_only`` these ALSO force
      ``allowed=False`` (§5.4 fail-safe) and are reported
      separately so the slice-2 validation layer can emit a
      distinct issue code.

    For a ``read_only`` step ``allowed`` is False iff
    ``blocked_tools`` OR ``unresolved_tools`` is non-empty. A
    ``write_allowed`` step is always ``allowed`` here (its
    friction is the authoring layer's concern, Q5).
    """

    allowed: bool
    blocked_tools: tuple[
        tuple[str, frozenset[ToolCapabilityTag]], ...
    ] = ()
    unresolved_tools: tuple[str, ...] = ()


def evaluate_reasoning_step(
    step: "ReasoningStep",
    *,
    resolve_tags: ResolveTags,
) -> ReasoningEnforcementOutcome:
    """Pure read-only-reasoning policy decision for one step.

    ``write_allowed`` ⇒ ``allowed`` (friction is the authoring
    layer's concern, Q5). ``read_only`` ⇒ every referenced tool
    is checked:

    - tags resolve to ``None`` OR an empty set ⇒ the tool is
      *unresolved* (§5.4 fail-safe — treated write-capable);
    - otherwise tags overlapping the 5-tag block set (decided
      by :func:`tool_tags.is_blocked_by_read_only_reasoning`,
      NOT a local re-declaration) ⇒ the tool is *blocked*.

    Either non-empty ⇒ ``allowed=False``. The step is never
    mutated; ``resolve_tags`` is the only collaborator and is
    pure by contract.
    """
    if step.tool_mode is ToolMode.WRITE_ALLOWED:
        return ReasoningEnforcementOutcome(allowed=True)

    blocked: list[tuple[str, frozenset[ToolCapabilityTag]]] = []
    unresolved: list[str] = []
    for tool_name in step.tools:
        tags = resolve_tags(tool_name)
        if not tags:
            # ``None`` (unregistered) OR empty set (registered
            # but untagged): §5.4 fail-safe — treat
            # write-capable, BLOCK, NEVER silent-allow.
            unresolved.append(tool_name)
            continue
        if is_blocked_by_read_only_reasoning(tags):
            blocked.append((tool_name, frozenset(tags)))

    allowed = not blocked and not unresolved
    return ReasoningEnforcementOutcome(
        allowed=allowed,
        blocked_tools=tuple(blocked),
        unresolved_tools=tuple(unresolved),
    )


@dataclass(frozen=True)
class ReasoningPlanGuardResult:
    """Plan-level read-only-reasoning guard result (phase-12
    slice-4, build-the-layer).

    ``outcomes`` is ``((step_id, ReasoningEnforcementOutcome),
    …)`` in plan order — one entry per ``ReasoningStep``.
    ``all_allowed`` is True iff EVERY step's outcome is
    allowed. This is the shape a future LLM reasoning-chain
    EXECUTOR would consult at the worker seam BEFORE invoking
    a step's tools. **No executor is shipped** (no §12 step
    owns it — Q1/Q3); this layer is wired at the seam but not
    fired end-to-end.
    """

    outcomes: tuple[
        tuple[str, ReasoningEnforcementOutcome], ...
    ] = ()

    @property
    def all_allowed(self) -> bool:
        return all(o.allowed for _sid, o in self.outcomes)


def evaluate_reasoning_plan(
    steps: "Iterable[ReasoningStep]",
    *,
    resolve_tags: ResolveTags,
) -> ReasoningPlanGuardResult:
    """Pure plan-level guard: map every ``ReasoningStep``
    through :func:`evaluate_reasoning_step` (slice-1).

    Strict delegation — the §5.4 blocking policy is NOT
    re-declared here; this only aggregates per-step outcomes
    keyed by ``step.id`` in order. ``steps`` is never mutated;
    ``resolve_tags`` is the only collaborator (pure by
    contract).
    """
    return ReasoningPlanGuardResult(
        outcomes=tuple(
            (s.id, evaluate_reasoning_step(s, resolve_tags=resolve_tags))
            for s in steps
        )
    )


__all__ = [
    "ResolveTags",
    "ReasoningEnforcementOutcome",
    "ReasoningPlanGuardResult",
    "evaluate_reasoning_step",
    "evaluate_reasoning_plan",
]
