"""V2 scheduler — ExecutionPlan (the frozen workflow body).

An ExecutionPlan is the hash-pinned multi-step body that a
ScheduleSpec optionally references for structured work. Owned
by the contract layer, hashed on freeze, stored once + indexed
by hash in the ``execution_plans`` SQLite table — multiple
ScheduleSpecs may reference the same ExecutionPlan body via the
same hash.

Phase 1 ships only the data shapes. Runtime semantics (read-only
reasoning enforcement, source loader registry, emit idempotency)
land at phases 4+ per the design contract §12.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0
- ``docs/PHASE_1_PLAN.md`` §4.5
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.v2.enums import EnforcementMode, FailureActionType, ToolMode
from app.v2.models.source_ref import SourceRefSpec


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Input / output / retry sub-shapes
# ---------------------------------------------------------------------------


class InputSpec(BaseModel):
    """A single deterministic input fetch — runs a registered
    loader before any reasoning starts.

    Phase-2 registry validation will tighten ``loader`` to a known
    name; phase 1 keeps it as a free string.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="snake_case id; the key under which the "
        "loader's output is stored in the fire's state dict.",
    )
    loader: str = Field(
        description="Registered loader name. See "
        "``app.contracts.loaders.LOADERS`` for the v1 set; "
        "v2's source_* loaders live alongside them.",
    )
    args: dict[str, Any] = Field(default_factory=dict)
    cache_for_seconds: int = Field(
        default=0,
        ge=0,
        description="In-wakeup-window memo: if > 0, the "
        "loader's output is reused across fires within the "
        "SAME wakeup tick. Distinct from "
        "``source_ref.cache.cache_ttl_seconds`` (the "
        "cross-fire, snapshot-backed source cache) — see "
        "``docs/PHASE_10_PLAN.md`` §1.4 / Q2.",
    )
    source_ref: Optional[SourceRefSpec] = Field(
        default=None,
        description="Phase-10 (§1.8): per-source LiveSourceRef "
        "policy bundle (cache / live-change / explicit "
        "default). Additive + hash-neutral when None — "
        "stripped from ``canonical_body`` so pre-phase-10 "
        "plan bodies hash unchanged (mirrors the phase-9 "
        "``template.args`` None-strip).",
    )

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                "InputSpec.id must match ^[a-z][a-z0-9_]*$"
            )
        return v


class OutputSpec(BaseModel):
    """How a reasoning step's output is validated post-LLM call.

    ``type='json'``: ADK structured output validated against
    ``schema_`` (JSON Schema). ``type='text'``: free string
    with optional predicate ``constraints``. ``type='none'`` is
    forbidden by the rigor validator (phase 2 wires that check
    in via the typed-tool layer).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["json", "text", "none"] = Field(default="json")
    schema_: Optional[dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="JSON Schema for type=json outputs.",
    )
    constraints: list[str] = Field(default_factory=list)


class Retry(BaseModel):
    """Per-reasoning-step retry policy (different from
    ScheduleSpec-level retry).

    Phase 1 keeps the v1 numbers as defaults; phase 2's rigor
    validator may tighten further.
    """

    model_config = ConfigDict(extra="forbid")

    on_validation_fail: int = Field(default=1, ge=0, le=5)
    on_tool_error: int = Field(default=2, ge=0, le=5)


# ---------------------------------------------------------------------------
# Reasoning + gate + emit
# ---------------------------------------------------------------------------


class ReasoningStep(BaseModel):
    """One LLM call in the reasoning chain.

    Phase-1 highlights:
      - ``tool_mode`` defaults to ``read_only`` (worker hard-blocks
        write-tagged tools when not explicitly opted-in).
      - ``id`` is snake_case so emit idempotency keys can
        reference it stably across retries.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = Field(default="")
    entry_agent: str = Field(
        description="Sub-agent name where this step starts. Must "
        "exist in the live agent tree at fire time; phase-3 "
        "registry validation will tighten this.",
    )
    transfers_allowed: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    tool_mode: ToolMode = ToolMode.READ_ONLY
    model: Optional[str] = None
    user_template: str
    output: OutputSpec = Field(default_factory=OutputSpec)
    retry: Retry = Field(default_factory=Retry)
    max_tool_calls: int = Field(default=20, ge=1, le=200)

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                "ReasoningStep.id must match ^[a-z][a-z0-9_]*$"
            )
        return v


class Gate(BaseModel):
    """Pre-emit check. May skip the emit (default) or abort the
    whole fire (``emit.abort_on_gate_fail = True``).
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        description="Gate kind (e.g. 'sheet_dedup'). Phase-3 "
        "registry validation will tighten to known gates.",
    )
    args: dict[str, Any] = Field(default_factory=dict)


class EmitStep(BaseModel):
    """A side effect applied once all reasoning steps succeed.

    ``id`` is REQUIRED — it's load-bearing for the emit
    idempotency key
    (``schedule_id:root_run_id:emit_id``). v1's optional id
    field is intentionally rejected here per the round-6
    correction documented at ``docs/CONTRACTS_V2_DESIGN.md``
    §6.4.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="REQUIRED stable snake_case identifier. Used "
        "in the emit idempotency key. Renaming an emit's id in a "
        "later revision creates a new logical emit position; "
        "old in-flight retries will not dedup against the new "
        "key.",
    )
    adapter: str = Field(
        description="Registered emit adapter name. Phase-3 "
        "registry validation will tighten this.",
    )
    args: dict[str, Any] = Field(default_factory=dict)
    gate: Optional[Gate] = None
    abort_on_gate_fail: bool = False

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                "EmitStep.id must match ^[a-z][a-z0-9_]*$"
            )
        return v


# ---------------------------------------------------------------------------
# Acceptance + failure
# ---------------------------------------------------------------------------


class Acceptance(BaseModel):
    """Global checks applied after all reasoning finishes, before
    any emit runs. A failure here aborts the fire."""

    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(default_factory=list)


class FailureAction(BaseModel):
    """What happens when a reasoning step exhausts its retries
    OR an acceptance check fails OR a hard-gate emit aborts the
    run. ExecutionPlan-level override of ScheduleSpec.failure.
    """

    model_config = ConfigDict(extra="forbid")

    action: FailureActionType = FailureActionType.ALERT_ADMIN
    notify: list[str] = Field(default_factory=list)
    retry_after_minutes: int = Field(default=60, ge=1)
    abort: bool = True


# ---------------------------------------------------------------------------
# ExecutionPlan (root of the workflow body)
# ---------------------------------------------------------------------------


class ExecutionPlan(BaseModel):
    """The frozen workflow body referenced by a ScheduleSpec.

    Phase-1 invariants:
      - At least one EmitStep required (a plan with no side
        effect has no purpose).
      - Every EmitStep MUST carry a non-empty ``id`` (idempotency
        key foundation).
      - ``enforcement`` defaults to STRICT; PERMISSIVE values
        are accepted by the model but the freeze pathway
        (phase-2 validator) rejects them.

    Hash semantics match ``ScheduleSpec``: SHA-256 over
    canonicalised body excluding ``hash`` and ``authored_at``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="snake_case identifier; same plan body across "
        "different ScheduleSpecs may share an id but each is "
        "hashed independently."
    )
    version: int = Field(default=1, ge=1)
    description: str
    author: str = Field(
        description="Email or platform id of the author. Used "
        "for per-user OAuth resolution at fire time."
    )
    authored_at: str = Field(default_factory=_utc_now_iso)
    parent_hash: Optional[str] = None

    inputs: list[InputSpec] = Field(default_factory=list)
    reasoning: list[ReasoningStep] = Field(default_factory=list)
    emit: list[EmitStep] = Field(
        description="At least one emit required — empty rejected."
    )
    acceptance: Acceptance = Field(default_factory=Acceptance)
    on_failure: FailureAction = Field(default_factory=FailureAction)
    enforcement: EnforcementMode = EnforcementMode.STRICT

    hash: str = Field(default="")

    # ---- validators ----

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                "ExecutionPlan.id must match ^[a-z][a-z0-9_]*$"
            )
        return v

    @field_validator("emit")
    @classmethod
    def _at_least_one_emit(cls, v: list[EmitStep]) -> list[EmitStep]:
        if not v:
            raise ValueError(
                "ExecutionPlan must declare at least one emit — "
                "a plan with no side effect has no purpose."
            )
        return v

    # ---- hashing ----

    def canonical_body(self) -> dict[str, Any]:
        """JSON-serialisable body for hashing. Excludes
        ``hash`` / ``authored_at``.

        Phase-10 amendment (2026-05-16, §1.8): each
        ``InputSpec.source_ref`` is stripped from the
        serialised input dict when ``None`` so pre-phase-10
        plan bodies (which had no ``source_ref`` key on
        disk) hash UNCHANGED. Without this strip,
        ``model_dump`` emits ``"source_ref": null`` on every
        input and shifts the sorted-JSON output. A populated
        ``source_ref`` participates in the hash; a body
        change re-hashes. Mirrors the phase-9
        ``TemplateRef.args`` None-strip (see
        ``docs/PHASE_10_PLAN.md`` §5 / ``ScheduleSpec.
        canonical_body``).
        """
        d = self.model_dump(mode="json", by_alias=True)
        d.pop("hash", None)
        d.pop("authored_at", None)
        for inp in d.get("inputs", []):
            if isinstance(inp, dict) and inp.get("source_ref") is None:
                inp.pop("source_ref", None)
        return d

    def compute_hash(self) -> str:
        body = self.canonical_body()
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def with_fresh_hash(self) -> "ExecutionPlan":
        return self.model_copy(update={"hash": self.compute_hash()})
