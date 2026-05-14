"""V2 scheduler — Run model.

A Run is one due execution. The unit of work for the worker pool.
Lifecycle states (``pending → claimed → running → succeeded |
failed | cancelled``) are documented at
``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1.

Two key invariants pinned at the model level:

  * **First-attempt self-reference**: when ``attempt == 1``,
    ``root_run_id`` must equal the Run's own ``id`` and
    ``parent_run_id`` must be ``None``. Retry Runs carry the
    original first-attempt's ``id`` in ``root_run_id`` and the
    previous attempt's ``id`` in ``parent_run_id``.

  * **No retry_pending state**: the round-6 design review
    collapsed retries into NEW pending Runs (not a state on the
    old Run). The old Run terminates at ``failed`` and the
    retry chain is queryable via ``root_run_id``. The
    ``RunStatus`` enum does not include ``retry_pending`` at all.

See ``docs/PHASE_1_PLAN.md`` §4.6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.v2.enums import FireReason, RunStatus


class Run(BaseModel):
    """One due execution of a ScheduleSpec. Persisted in the
    ``runs`` SQLite table.

    Construction normally happens via the wakeup function (cron /
    one-off due time) or the failure handler (retry insertion).
    Tests can construct directly; storage layer (phase-3) wires
    up the canonical write path.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="UUID4 string. Worker pool sets this when "
        "inserting a Run row; not derived from any other field "
        "(so retries can have independent ids while sharing "
        "root_run_id).",
    )
    schedule_id: str
    execution_plan_hash: Optional[str] = Field(
        default=None,
        description="Snapshot of ScheduleSpec.execution_plan_hash "
        "at the moment this Run was created. Subsequent revisions "
        "to the ScheduleSpec DO NOT retroactively change this "
        "Run's plan — the body the worker executes is whichever "
        "ExecutionPlan was hash-pinned when the Run was inserted. "
        "None for reminder-shape ScheduleSpecs that have no "
        "execution plan.",
    )
    fire_reason: FireReason
    due_at: datetime = Field(
        description="When the Run is due to fire (UTC). The worker "
        "pool's claim query filters on ``due_at <= now``.",
    )
    status: RunStatus = RunStatus.PENDING
    attempt: int = Field(
        default=1,
        ge=1,
        description="1 for the first attempt; bumped by the "
        "failure handler when inserting a retry Run.",
    )
    root_run_id: str = Field(
        description="Stable id across the retry chain. For "
        "first-attempt Runs this MUST equal ``id`` (self-reference). "
        "For retry Runs this is the id of the original first "
        "attempt — used by the emit idempotency key + retry-chain "
        "queries.",
    )
    parent_run_id: Optional[str] = Field(
        default=None,
        description="Previous attempt's id. None for first "
        "attempts; the failure handler sets this when inserting "
        "a retry.",
    )
    claimed_by: Optional[str] = Field(
        default=None,
        description="Worker id that claimed this Run (diagnostic; "
        "not an enforced authority — SQLite single-writer + the "
        "``WHERE status='pending'`` predicate enforce the "
        "single-claim invariant).",
    )
    claimed_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    @model_validator(mode="after")
    def _retry_chain_invariants(self) -> "Run":
        """Pins two invariants in lockstep:

        1. attempt == 1  ⟺  parent_run_id is None
        2. attempt == 1  ⟹  root_run_id == id (self-reference)

        Violations would let a retry chain get into a state where
        ``root_run_id`` no longer identifies the original attempt,
        breaking idempotency key stability + replay lineage.
        """
        if self.attempt == 1:
            if self.parent_run_id is not None:
                raise ValueError(
                    "Run with attempt=1 must have parent_run_id=None. "
                    "Retry Runs (attempt >= 2) set parent_run_id."
                )
            if self.root_run_id != self.id:
                raise ValueError(
                    "Run with attempt=1 must have root_run_id == id "
                    "(self-reference). Got "
                    f"root_run_id={self.root_run_id!r}, id={self.id!r}."
                )
        else:
            # attempt >= 2: parent_run_id is required.
            if self.parent_run_id is None:
                raise ValueError(
                    f"Run with attempt={self.attempt} must have "
                    "parent_run_id set (chains to the previous attempt)."
                )
            # root_run_id must NOT be self for retries; the original
            # first attempt's id is the stable root.
            if self.root_run_id == self.id:
                raise ValueError(
                    f"Run with attempt={self.attempt} cannot have "
                    "root_run_id == id (that's the first-attempt "
                    "self-reference; retries point at the original)."
                )
        return self
