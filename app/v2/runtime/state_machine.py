"""Legal Run-status transitions for the v2 runtime.

Phase 4 slice 1 per ``docs/PHASE_4_PLAN.md`` §3.

Pure data + two predicates — no I/O. The worker, claim helper,
and recovery scan import :func:`assert_legal_transition` and
call it BEFORE issuing any storage update so the data plane
never sees a transition that runtime policy forbids.

The phase-3 storage layer is deliberately policy-free (it
accepts any unpredicated UPDATE on a run row); phase 4 adds
the policy chokepoint here.

Transitions phase-4 code performs (§3.1):

- pending  → claimed   — worker wins single-flight claim
- claimed  → running   — worker has started the body
- claimed  → failed    — recovery: claimed-stale (QUEUE_RETRY
                          two-row remediation; the stale row
                          is the one that flips to failed)
- running  → succeeded — worker body completed
- running  → failed    — worker raised, OR recovery flips
                          running-stale

Transitions deliberately NOT in phase 4 (§3.1.1 — these have
no phase-4 owner and would create re-execution risk if added):

- pending → cancelled  (pause/archive admin path — later phase)
- claimed → pending    (admin CLEAR_CLAIM — later phase)
- running → pending    (retry chain inserts a NEW pending row,
                        not a re-status of the old one)

Terminal states: succeeded, failed, cancelled. Retries land
as NEW pending Run rows with parent_run_id / root_run_id
carried — never by re-statusing a terminal row (round-6
invariant; storage RunStatus CHECK enforces no retry_pending).
"""

from __future__ import annotations

from app.v2.enums import RunStatus


class IllegalTransitionError(ValueError):
    """Raised by :func:`assert_legal_transition` when a caller
    attempts a status change that isn't in
    :data:`LEGAL_TRANSITIONS`.

    Subclass of ``ValueError`` so callers can ``except
    ValueError`` and still catch this; the dedicated class
    keeps error messages explicit about which dimension of
    the contract was violated.
    """


# Exact transition set. Reviewer round-3 approved the phase-4
# 5-entry table; phase-7 slice-5a adds the
# ``(PENDING, CANCELLED)`` entry so the authoring layer's
# archive helper can route every pending Run through the
# chokepoint when a schedule is archived. Any further change
# must be reviewer-approved + documented in the relevant
# plan. Adding an entry here without a call site that
# performs it would be a policy bug.
LEGAL_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {
        (RunStatus.PENDING, RunStatus.CLAIMED),    # worker claim
        (RunStatus.PENDING, RunStatus.CANCELLED),  # phase-7 archive helper
        (RunStatus.CLAIMED, RunStatus.RUNNING),    # worker body start
        (RunStatus.CLAIMED, RunStatus.FAILED),     # recovery claimed-stale
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),  # worker body completed
        (RunStatus.RUNNING, RunStatus.FAILED),     # worker raised / recovery
    }
)


def is_legal_transition(src: RunStatus, dst: RunStatus) -> bool:
    """Return ``True`` iff ``(src, dst)`` is in
    :data:`LEGAL_TRANSITIONS`.

    Predicate-style helper for call sites that want to make a
    decision based on the transition's legality (e.g. skip
    rather than abort). For "abort on illegal" semantics use
    :func:`assert_legal_transition` instead.
    """
    return (src, dst) in LEGAL_TRANSITIONS


def assert_legal_transition(src: RunStatus, dst: RunStatus) -> None:
    """Raise :class:`IllegalTransitionError` when ``(src, dst)``
    is not legal.

    Worker + recovery scan call this as the policy chokepoint
    before issuing any storage update. The phase-3 storage
    layer would otherwise accept the unpredicated UPDATE and
    silently mutate the row into a status the runtime never
    intends to reach.
    """
    if not is_legal_transition(src, dst):
        raise IllegalTransitionError(
            f"illegal run-status transition "
            f"{src.value!r} → {dst.value!r}. "
            "Phase-4 legal set: pending→claimed, "
            "claimed→running, claimed→failed, "
            "running→succeeded, running→failed. "
            "See docs/PHASE_4_PLAN.md §3."
        )


__all__ = [
    "IllegalTransitionError",
    "LEGAL_TRANSITIONS",
    "assert_legal_transition",
    "is_legal_transition",
]
