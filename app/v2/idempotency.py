"""V2 scheduler — emit idempotency key derivation.

The emit idempotency key MUST be stable across the retry chain
so that a retry attempt of the same logical fire short-circuits
on prior ``emit_succeeded`` events instead of double-delivering.

Formula (per ``docs/CONTRACTS_V2_DESIGN.md`` §6.4):

    idempotency_key = f"{schedule_id}:{root_run_id}:{emit_id}"

Three properties:

  * **Schedule scope** — distinct schedules with the same emit_id
    never collide.
  * **Stable across retries** — ``root_run_id`` is constant across
    the retry chain. ``attempt`` is deliberately NOT part of the
    key (its inclusion in v1 was the round-6 regression).
  * **Emit-position stable** — ``emit_id`` is required on every
    EmitStep so reordering the emit list in a revised
    ExecutionPlan doesn't perturb dedup for in-flight retries.

Where the destination supports a native dedup token (Slack
``client_msg_id``), the idempotency key is passed through as
that token too — defense in depth.

Phase 1 ships this helper as schema-adjacent — runtime adapters
call it at phase 4+ when they actually fire emits.
"""

from __future__ import annotations


def compute_idempotency_key(
    *, schedule_id: str, root_run_id: str, emit_id: str
) -> str:
    """Build the canonical emit idempotency key.

    Pure function — no side effects, no I/O. Keep it deterministic
    so two callers (the adapter pre-check + the ledger query)
    arrive at the same string for the same logical emit.

    Args:
        schedule_id: ScheduleSpec.id — owns the namespace.
        root_run_id: Run.root_run_id — stable across the retry
            chain. Use ``Run.root_run_id``, NOT ``Run.id``: for
            retries those differ and the run-id form would let
            the same logical emit fire twice.
        emit_id: EmitStep.id — the position-stable identifier
            on the EmitStep this attempt is firing.

    Returns:
        Colon-joined key suitable for storage in the
        ``emit_succeeded`` event's payload and for the Slack
        ``client_msg_id`` / similar upstream-dedup tokens.
    """
    for name, value in {
        "schedule_id": schedule_id,
        "root_run_id": root_run_id,
        "emit_id": emit_id,
    }.items():
        if not value:
            raise ValueError(
                f"compute_idempotency_key: {name} must be non-empty"
            )
        if ":" in value:
            raise ValueError(
                f"compute_idempotency_key: {name}={value!r} contains "
                "':' which would break the canonical key delimiter."
            )
    return f"{schedule_id}:{root_run_id}:{emit_id}"
