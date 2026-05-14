"""V2 scheduler — Pydantic model namespace.

One module per top-level concept:

- ``common``        — UserRef, ChannelRef, SheetRef, TemplateRef,
                       Delivery, FailurePolicy, RetryPolicy,
                       AuditPolicy
- ``triggers``      — Trigger discriminator union
- ``schedule``      — ScheduleSpec
- ``execution_plan`` — ExecutionPlan, InputSpec, ReasoningStep,
                        EmitStep, Gate, OutputSpec, Acceptance,
                        FailureAction, Retry
- ``run``           — Run
- ``event``         — Event
- ``state``         — ScheduleState
- ``snapshot``      — SourceSnapshotMetadata

Phase 1 ships ``common`` + ``triggers`` first (this commit). The
rest land in subsequent phase-1 commits per
``docs/PHASE_1_PLAN.md`` §9.
"""
