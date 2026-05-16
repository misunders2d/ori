"""V2 scheduler — canonical enum values.

All v2 state-machine values, event kinds, and policy modes live
here. Each Python enum mirrors a value set that also appears in
SQLite CHECK constraints (see ``app/v2/ddl/v001_initial.sql`` when
it lands) so the type system and the storage layer agree on the
universe of valid values.

Keep this file pure-stdlib + ``str, Enum`` — it must be import-
cheap and side-effect-free so any other v2 module can pull from
it without dragging in heavy dependencies.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 (canonical EventKinds list)
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2 (SQLite CHECK constraints)
- ``docs/PHASE_1_PLAN.md`` §4.1 (phase-1 enum inventory)
"""

from __future__ import annotations

from enum import Enum


class ScheduleStatus(str, Enum):
    """ScheduleSpec lifecycle status.

    ``active`` — wakeup function creates runs per the trigger.
    ``paused`` — no new runs created; existing pending/running
        runs handled per the ScheduleSpec's paused_pending_policy.
    ``archived`` — terminal; no new runs ever; revival requires
        ``schedule_revive`` admin tool transitioning to ``paused``.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class RunStatus(str, Enum):
    """Lifecycle state for a single Run row.

    The state machine is documented at
    ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1. Terminal states
    (``succeeded``, ``failed``, ``cancelled``) never transition
    further — retries create new Run rows instead of re-statusing
    the old one. There is intentionally NO ``retry_pending``
    state: review round 6 collapsed retries into new pending
    rows with ``root_run_id`` linking back to the original
    attempt.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FireReason(str, Enum):
    """Why a Run row was created.

    ``scheduled``  — created by the wakeup function from the
        contract's trigger (cron / interval / one_off).
    ``manual``     — explicit user-initiated fire via tool call.
    ``retry``      — created by the failure handler in response to
        a prior failed run; ``parent_run_id`` chains the
        attempts and ``root_run_id`` is the original.
    ``replay``     — re-fire of a historical run for debugging /
        replay-comparison.
    ``backfill``   — created for a missed due_at when catching up
        after downtime, bounded by the contract's
        ``backfill_policy``.
    """

    SCHEDULED = "scheduled"
    MANUAL = "manual"
    RETRY = "retry"
    REPLAY = "replay"
    BACKFILL = "backfill"


class EnforcementMode(str, Enum):
    """Per-ExecutionPlan strictness.

    ``strict``: every reasoning step MUST run, MUST produce
        schema-valid output, MUST be captured to the fire's audit
        log. Skipping, mocking, or short-circuiting is a hard
        contract-violation and aborts the fire via ``on_failure``.
    ``permissive``: authoring-experiment only; never reaches
        production. Author tools refuse to freeze a permissive
        ExecutionPlan.
    """

    STRICT = "strict"
    PERMISSIVE = "permissive"


class EventKind(str, Enum):
    """Canonical event-ledger kinds.

    Every state transition in the v2 scheduler writes one of
    these. The full canonical list is documented at
    ``docs/CONTRACTS_V2_DESIGN.md`` §4.0. Adding a new event kind
    requires a docs update in lockstep so the canonical list
    stays single-source-of-truth.
    """

    # Schedule-level
    SCHEDULE_CREATED = "schedule_created"
    SCHEDULE_REVISED = "schedule_revised"
    SCHEDULE_PAUSED = "schedule_paused"
    SCHEDULE_ARCHIVED = "schedule_archived"
    SCHEDULE_RESUMED = "schedule_resumed"
    SCHEDULE_REVIVED = "schedule_revived"

    # Run-level
    RUN_CREATED = "run_created"
    RUN_CLAIMED = "run_claimed"
    RUN_STARTED = "run_started"
    RUN_RECOVERED = "run_recovered"  # boot recovery promoted a stale claimed/running run
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_RETRY_SCHEDULED = "run_retry_scheduled"
    RUN_CANCELLED = "run_cancelled"

    # Source-level
    SOURCE_RESOLVED = "source_resolved"
    SOURCE_DRIFT_DETECTED = "source_drift_detected"
    SOURCE_FAILED = "source_failed"

    # Reasoning-level
    REASONING_STARTED = "reasoning_started"
    REASONING_COMPLETED = "reasoning_completed"
    REASONING_FAILED = "reasoning_failed"

    # Emit-level
    EMIT_STARTED = "emit_started"
    EMIT_SUCCEEDED = "emit_succeeded"
    EMIT_FAILED = "emit_failed"
    EMIT_SKIPPED_IDEMPOTENT = "emit_skipped_idempotent"

    # Delivery / alert
    DELIVERY_FAILED = "delivery_failed"
    ADMIN_ALERT_SENT = "admin_alert_sent"
    ADMIN_ALERT_ACKED = "admin_alert_acked"

    # Auxiliary
    AUDIT_MIRROR_APPENDED = "audit_mirror_appended"
    BOOT_SELF_TEST_PASSED = "boot_self_test_passed"
    BOOT_SELF_TEST_FAILED = "boot_self_test_failed"
    MIGRATION_V1_TO_V2_COMPLETE = "migration_v1_to_v2_complete"


class SourceMode(str, Enum):
    """How a content source is captured in the contract body.

    ``literal``  — bytes frozen into the ExecutionPlan body.
    ``snapshot`` — fetched once at author time; the captured
        content + metadata are frozen into the body.
    ``live``     — only the reference + parser + permissions are
        frozen; content is fetched at fire time. The
        ``LiveSourceRef`` may include an optional ``version``
        field to pin a specific revision (collapsed from the
        earlier separate ``version_pinned`` mode per review
        round 3).
    """

    LITERAL = "literal"
    SNAPSHOT = "snapshot"
    LIVE = "live"


class SelectionMethod(str, Enum):
    """How the fire-time item was selected from a source.

    Recorded in the ``source_resolved`` event payload so the
    audit log explains "why item 5 instead of item 12".

    ``stable_id``    — source exposes a stable id per item
        (Sheets row id, Doc heading anchor, revision id).
    ``content_hash`` — id derived from normalized content hash
        (used for plain text / markdown / Keep notes where the
        source has no native stable id).
    ``row_number``   — positional fallback when neither of the
        above is available; flagged as fragile.
    """

    STABLE_ID = "stable_id"
    CONTENT_HASH = "content_hash"
    ROW_NUMBER = "row_number"


class RecoveryPolicy(str, Enum):
    """How to route stale Run rows found by the boot recovery
    scan (claimed/running rows older than the recovery timeout).

    Renamed from the original ``retry_pending`` term per review
    round 7 — that name conflicted with the now-dead
    ``retry_pending`` RunStatus value.

    ``queue_retry`` — insert a new pending Run (retry chain)
        following the standard failure-handler path.
    ``mark_failed`` — leave the original at status=``failed``;
        no retry queued. Use when the work is non-idempotent
        and a retry would do harm.
    ``clear_claim`` — clear the claim and return the run to
        ``pending`` so another worker can pick it up. Used when
        the worker crash is known to be transient and the
        partial work is safe to redo.
    """

    QUEUE_RETRY = "queue_retry"
    MARK_FAILED = "mark_failed"
    CLEAR_CLAIM = "clear_claim"


class OnOversizePolicy(str, Enum):
    """What happens when a per-fire source snapshot exceeds
    ``max_snapshot_bytes``. See
    ``docs/CONTRACTS_V2_DESIGN.md`` §4.6.4. Author MUST pick
    one explicitly — no silent default.

    ``fail_and_alert``      — fire fails; admin alerted.
    ``store_pointer_only``  — store immutable revision id only
        (requires source to expose one).
    ``redact_and_store``    — apply ``redact_fields`` rules and
        retry within cap.
    ``hash_only_no_replay`` — store only the content hash; fire
        is explicitly marked non-replayable.
    """

    FAIL_AND_ALERT = "fail_and_alert"
    STORE_POINTER_ONLY = "store_pointer_only"
    REDACT_AND_STORE = "redact_and_store"
    HASH_ONLY_NO_REPLAY = "hash_only_no_replay"


class LiveChangePolicy(str, Enum):
    """How to react when a LiveSourceRef's content shape changes
    between fires.

    ``allow`` — edits flow through; no alert (default for daily
        content the user maintains).
    ``alert_on_shape_change`` — log/alert when item_count or
        schema changes; still fire.
    ``require_reapprove_on_shape_change`` — refuse to fire on
        shape change; admin must re-approve before next fire.
    """

    ALLOW = "allow"
    ALERT_ON_SHAPE_CHANGE = "alert_on_shape_change"
    REQUIRE_REAPPROVE_ON_SHAPE_CHANGE = "require_reapprove_on_shape_change"


class SourceFallbackPolicy(str, Enum):
    """What to do when a LiveSourceRef can't be fetched at fire
    time (network failure, source missing, parse error). Auth
    failures bypass this and always route to on_failure.

    ``use_last_good_snapshot`` — fall back to the most recent
        cached snapshot if available; otherwise alert and skip.
    ``alert_and_skip`` — don't fire; alert admin.
    ``alert_and_use_default`` — use the contract's explicitly-
        declared default value. Valid only when a default is
        provided AND shown in dry-run.
    """

    USE_LAST_GOOD_SNAPSHOT = "use_last_good_snapshot"
    ALERT_AND_SKIP = "alert_and_skip"
    ALERT_AND_USE_DEFAULT = "alert_and_use_default"


class FailureActionType(str, Enum):
    """ScheduleSpec.failure.on_failure_action — what to do when
    a Run terminates at ``failed``.

    ``alert_admin``  — write admin_alert_sent event + DM admin.
    ``retry_later``  — insert a new pending Run with backoff.
    ``abort_silent`` — terminal; no alert. Ledger event only.
    ``custom``       — invoke a registered callable (phase 3+).
    """

    ALERT_ADMIN = "alert_admin"
    RETRY_LATER = "retry_later"
    ABORT_SILENT = "abort_silent"
    CUSTOM = "custom"


class DeliveryFallbackPolicy(str, Enum):
    """ScheduleSpec.delivery.fallback_policy values.

    ``session_to_origin`` — on L1 delivery failure, try the
        creator's session (different channel for cross-platform
        schedules). Default for most ScheduleSpecs.
    ``admin_alert_only`` — skip the user-facing L2 fallback;
        only the L3 admin alert fires. Used when L2 retry is
        guaranteed to fail (e.g. the recipient explicitly
        unreachable).

    See ``docs/CONTRACTS_V2_DESIGN.md`` §7.2 (L1/L2/L3 layers).
    """

    SESSION_TO_ORIGIN = "session_to_origin"
    ADMIN_ALERT_ONLY = "admin_alert_only"


class RetryStrategy(str, Enum):
    """RetryPolicy.strategy values.

    ``exponential`` — backoff scales by attempt number
        (typically ``base_seconds * 2 ** (attempt - 1)``).
    ``fixed``       — same ``base_seconds`` between every retry.
    """

    EXPONENTIAL = "exponential"
    FIXED = "fixed"


class ToolMode(str, Enum):
    """ReasoningStep.tool_mode — controls which tools the LLM
    may call inside the reasoning step.

    ``read_only`` (default) — a §12 step-12 read-only-reasoning
        ENFORCEMENT rule (``validate_schedule_spec``) rejects a
        reasoning step that references a tool tagged
        ``write_external``, ``send_message``,
        ``filesystem_write``, ``db_write``, or ``privileged``
        (the §5.4 ``_READ_ONLY_BLOCKING_TAGS`` set — single
        source of truth in ``tool_tags``). All writes go
        through emit adapters. A build-the-layer worker runtime
        guard mirrors this; no reasoning EXECUTOR is shipped
        (no §12 step owns it).
    ``write_allowed`` — opt-in for the rare workflow that needs
        the LLM to call a write tool mid-reasoning. Surfaces a
        §5.9 advisory friction WARNING at validation; NO
        admin-approval gate or executor is shipped (the gate
        is conceptual and DEFERRED).
    """

    READ_ONLY = "read_only"
    WRITE_ALLOWED = "write_allowed"


class PausedPendingPolicy(str, Enum):
    """What happens to a schedule's already-pending Runs when
    the schedule is paused (:func:`schedule_pause`).

    Phase-14 (2026-05-16). Housed on
    :class:`app.v2.models.common.FailurePolicy` (persisted via
    the ``failure_json`` column — it round-trips through
    ``insert_schedule`` / ``_row_to_spec`` with NO DDL and NO
    v002 migration; the phase-9 ``TemplateRef.args``
    nested-optional precedent applied). An early phase-14 plan
    draft wrongly assumed a literal top-level ``ScheduleSpec``
    field — the (α) fork ruling corrected that
    (``docs/PHASE_14_PLAN.md`` §9.1): a new top-level field is
    NOT persisted by the column-decomposed ``schedules`` table
    without DDL, whereas ``FailurePolicy`` already round-trips
    and is the defensible semantic home for
    "what to do with in-flight / pending work on a lifecycle
    state change".

    ``let_complete`` (default) — pending Runs are left
        untouched; they fire normally even though the schedule
        is paused. This is the pre-phase-14 behaviour: pausing
        with no policy set is a no-op for in-flight work
        (design §7 archive-vs-pause asymmetry; least-surprise).
    ``cancel_pending`` — every pending Run is cancelled in the
        same transaction as the pause, exactly as
        :func:`schedule_archive` does (same
        ``update_status_with_event`` cancel-pending seam, same
        ``run_cancelled`` event, same PENDING→CANCELLED
        state-machine transition). Differs from archive ONLY in
        the ``run_cancelled`` payload ``reason``
        (``schedule_paused`` vs ``schedule_archived``) so the
        audit ledger states the truth (§13).

    See ``docs/CONTRACTS_V2_DESIGN.md`` §7.
    """

    LET_COMPLETE = "let_complete"
    CANCEL_PENDING = "cancel_pending"
