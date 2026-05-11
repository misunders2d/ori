"""Contract schema — the pydantic models every scheduled-task contract
conforms to.

The shape is intentionally generic. Wildly different tasks (a static
daily Slack tip, a 30-step ASIN audit, a weekly news digest) all fit
the same envelope: GATHER → REASON → MATERIALIZE → VALIDATE → EMIT.
Per-task variability lives inside the slot values, not the structure.

Authoring intelligence (the bot, in conversation with the user) drafts
the slot values for each new task. Once approved, the contract is
hashed + frozen — execution is then mechanical and drift-proof.

Hash semantics: ``Contract.hash`` is computed over the canonicalised
JSON of every field EXCEPT ``hash``, ``authored_at``, and the live
``status`` tracker. Two contracts with identical authoring content
produce identical hashes; any byte changes (a prompt tweak, a tool
whitelist edit, a schema field rename) produce a new hash and so a new
version.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Triggers — when does the contract fire?
# ---------------------------------------------------------------------------


class CronTrigger(BaseModel):
    """Recurring schedule. ``cron`` is a standard 5-field cron expression
    interpreted in ``timezone``. APScheduler is the runtime."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cron"] = "cron"
    cron: str = Field(
        ...,
        description="Standard 5-field cron expression — e.g. '0 18 * * MON' "
        "for 18:00 every Monday in ``timezone``.",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA timezone name — e.g. 'Europe/Kyiv', 'America/New_York'.",
    )


class OnDemandTrigger(BaseModel):
    """Fired explicitly by a tool call or admin action. No cron."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["on_demand"] = "on_demand"


class EventTrigger(BaseModel):
    """Fired by an internal event hook (e.g. ``new_asin_added``,
    ``buybox_lost``). The event-bus side of this is part of P5 wiring;
    the contract just declares what it listens for."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    event: str = Field(..., description="Event name the executor subscribes to.")


Trigger = Union[CronTrigger, OnDemandTrigger, EventTrigger]


# ---------------------------------------------------------------------------
# Inputs — deterministic, NO LLM
# ---------------------------------------------------------------------------


class InputSpec(BaseModel):
    """A single deterministic input fetch.

    ``loader`` names a registered loader (``bigquery_query``,
    ``web_search``, ``static_param``, ...). ``args`` are passed to the
    loader; templated strings ``{...}`` can reference earlier inputs by
    id, so loaders can chain (e.g. fetch ASINs from BigQuery, then pass
    those ASINs to Keepa).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Lowercase snake_case identifier — used as the key "
        "under which this loader's output is stored.",
    )
    loader: str = Field(
        ...,
        description="Registered loader name. See app.contracts.loaders.LOADERS.",
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to the loader. String values may contain "
        "``{input_id.field}`` templates to reference earlier inputs.",
    )
    cache_for_seconds: int = Field(
        default=0,
        ge=0,
        description="If > 0, cache this loader's output for the given "
        "number of seconds so repeated fires within the window skip the "
        "fetch. Useful for expensive web searches.",
    )


# ---------------------------------------------------------------------------
# Reasoning — LLM calls with sub-agent routing
# ---------------------------------------------------------------------------


class OutputSpec(BaseModel):
    """How the LLM step's output is validated.

    The output ``type`` decides whether we ask the model for free text
    (constrained by predicates) or structured JSON (constrained by a
    schema). Both reject the fire when validation fails; the executor
    retries once with feedback before invoking ``on_failure``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["json", "text", "none"] = Field(
        default="json",
        description="``json`` = ADK structured output, ``text`` = free "
        "string with optional predicate constraints, ``none`` = no LLM "
        "call (step records inputs as-is — rare).",
    )
    schema_: Optional[dict[str, Any]] = Field(
        default=None,
        alias="schema",
        description="JSON Schema for ``type=json``. Validated post-call.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="For ``type=text``: human-readable predicates the "
        "executor evaluates after the call (e.g. 'min 200 chars', "
        "'contains \"FBA\"', 'no markdown #'). Some are mechanical "
        "(length); others are advisory and logged but not enforced.",
    )


class Retry(BaseModel):
    """How many times to retry a reasoning step before invoking
    ``on_failure``. Each retry includes the previous validation error
    as feedback to the model.
    """

    model_config = ConfigDict(extra="forbid")

    on_validation_fail: int = Field(default=1, ge=0, le=5)
    on_tool_error: int = Field(default=2, ge=0, le=5)


class ReasoningStep(BaseModel):
    """One LLM call in the reasoning chain.

    A step names the ``entry_agent`` whose tools cover the work, the
    set of agents it may transfer to, and the tool whitelist that
    ``plan_step_enforcer`` hard-blocks against. Output shape is locked
    by ``output``. Per-step state is stored under ``id`` and is
    available to later steps via ``user_template`` interpolation.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(
        default="",
        description="One-line human summary. Used by the dry-run UI.",
    )
    entry_agent: str = Field(
        ...,
        description="Sub-agent name where this step starts. The agent "
        "must exist in the live agent tree. Examples: "
        "'AmazonHeadAgent', 'BigQueryAgent', 'AmazonMemoryAgent'.",
    )
    transfers_allowed: list[str] = Field(
        default_factory=list,
        description="Sub-agents this step may transfer to. Includes "
        "the entry_agent. Empty list = no transfers allowed.",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Hard-enforced tool whitelist for this step. "
        "``plan_step_enforcer`` rejects any tool call outside the list. "
        "Sub-agent native tools still require entry here.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Hot-swap model key (e.g. 'gemini-3-flash-preview', "
        "'openrouter/anthropic/claude-opus-4.7'). Defaults to the "
        "entry_agent's configured model.",
    )
    user_template: str = Field(
        ...,
        description="Template for the user-role message sent to the LLM. "
        "Placeholders ``{input_id.field}`` and ``{step_id.field}`` are "
        "rendered from prior loader/step outputs at fire time.",
    )
    output: OutputSpec = Field(default_factory=OutputSpec)
    retry: Retry = Field(default_factory=Retry)
    max_tool_calls: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Hard cap on tool invocations within this step. "
        "Prevents runaway loops if a sub-agent gets confused.",
    )


# ---------------------------------------------------------------------------
# Emit — deterministic side effects
# ---------------------------------------------------------------------------


class Gate(BaseModel):
    """A pre-emit check that may abort the emit step.

    Today's only gate type is ``sheet_dedup`` — read a tracking sheet,
    fail the emit if a row already exists for the current date/key. New
    gate types are added in ``app.contracts.emit.GATES``.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., description="Gate kind (e.g. 'sheet_dedup').")
    args: dict[str, Any] = Field(default_factory=dict)


class EmitStep(BaseModel):
    """A side effect to apply once all reasoning steps succeed.

    ``adapter`` names a registered emit adapter (``slack_post``,
    ``drive_doc_fill``, ``sheet_append``, ``telegram_dm``, ``email``,
    ``memory_update``). ``args`` are template-rendered against the
    full state dict (inputs + reasoning outputs).

    Multiple emits run in declared order. Each can carry a ``gate``
    that, if it fails, skips just that emit (the rest still run unless
    ``abort_on_gate_fail`` is true).
    """

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = Field(
        default=None,
        description="Optional human label for this emit; useful in the "
        "audit log.",
    )
    adapter: str = Field(..., description="Registered emit adapter name.")
    args: dict[str, Any] = Field(default_factory=dict)
    gate: Optional[Gate] = Field(
        default=None,
        description="Optional pre-emit gate. If the gate fails, this "
        "emit is skipped (or the whole contract aborts — see flag below).",
    )
    abort_on_gate_fail: bool = Field(
        default=False,
        description="If true, a failed gate aborts the entire contract "
        "fire (subsequent emits don't run, on_failure is invoked).",
    )


# ---------------------------------------------------------------------------
# Acceptance + failure handling
# ---------------------------------------------------------------------------


class Acceptance(BaseModel):
    """Global checks applied to the entire fire after all reasoning is
    done, before any emit runs. A failure here aborts the contract.
    """

    model_config = ConfigDict(extra="forbid")

    checks: list[str] = Field(
        default_factory=list,
        description="Human-readable predicates. Mechanical ones the "
        "executor evaluates (template completeness, length bounds); "
        "advisory ones are logged. See app.contracts.worker for the "
        "supported list.",
    )


class FailureActionType(str, Enum):
    ALERT_ADMIN = "alert_admin"
    ABORT_SILENT = "abort_silent"
    RETRY_LATER = "retry_later"


class EnforcementMode(str, Enum):
    """How rigid the executor is about following the contract verbatim.

    ``strict`` (the only mode you should ever ship): every reasoning
    step MUST run, MUST honour its tool whitelist, MUST produce output
    that validates against its schema or constraints, MUST be stored in
    the fire's audit log. Skipping, mocking, summarising-and-skipping,
    or short-circuiting on the LLM's own initiative is a hard
    contract-violation and aborts the fire via ``on_failure``.

    ``permissive`` exists for one-off authoring experiments only — it
    relaxes whitelist enforcement to warn-and-continue. Never use it
    for a frozen production contract; the AUTHOR tools refuse to freeze
    a contract whose mode is ``permissive``.
    """

    STRICT = "strict"
    PERMISSIVE = "permissive"


class FailureAction(BaseModel):
    """What happens when a reasoning step exhausts its retries OR an
    acceptance check fails OR a hard-gate emit aborts the run."""

    model_config = ConfigDict(extra="forbid")

    action: FailureActionType = FailureActionType.ALERT_ADMIN
    notify: list[str] = Field(
        default_factory=list,
        description="User IDs to DM when ``action=alert_admin``.",
    )
    retry_after_minutes: int = Field(
        default=60,
        ge=1,
        description="Used only when ``action=retry_later``.",
    )
    abort: bool = Field(
        default=True,
        description="If true, the fire is aborted and no partial emit "
        "runs. Almost always what you want.",
    )


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class Contract(BaseModel):
    """The frozen authoring artefact. Identifies a scheduled or
    on-demand task in full: what to fetch, what to reason about, what
    to emit, how to validate, and how to fail.

    Authored by the bot in conversation with the user, dry-run for
    review, then frozen + hashed. Edits create new versions; the
    previous version is preserved in the audit chain.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Globally unique, snake_case. Used for storage path "
        "+ scheduler job id.",
    )
    version: int = Field(default=1, ge=1)
    hash: str = Field(
        default="",
        description="SHA-256 of the canonicalised contract body. "
        "Computed at freeze time. Empty on draft.",
    )
    description: str = Field(
        ...,
        description="Human-readable summary of what this task does.",
    )
    author: str = Field(..., description="User id who authored / approved.")
    authored_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    parent_hash: Optional[str] = Field(
        default=None,
        description="Hash of the previous version this was revised from. "
        "Empty for the initial draft.",
    )

    trigger: Trigger
    inputs: list[InputSpec] = Field(default_factory=list)
    reasoning: list[ReasoningStep] = Field(default_factory=list)
    emit: list[EmitStep]
    acceptance: Acceptance = Field(default_factory=Acceptance)
    on_failure: FailureAction = Field(default_factory=FailureAction)
    enforcement: EnforcementMode = Field(
        default=EnforcementMode.STRICT,
        description="Always ``strict`` for production. The executor "
        "treats each reasoning step as load-bearing: it must run in "
        "order, may only call tools in its whitelist, may only transfer "
        "to allowed sub-agents, must produce schema-valid output, and "
        "its output is captured to the fire's audit log. Skipping, "
        "mocking, or discarding a step aborts the fire.",
    )

    @field_validator("emit")
    @classmethod
    def _at_least_one_emit(cls, v: list[EmitStep]) -> list[EmitStep]:
        if not v:
            raise ValueError(
                "Contract must define at least one emit step — a contract "
                "with no side effect has no purpose."
            )
        return v

    # ----- hashing -----

    def canonical_body(self) -> dict[str, Any]:
        """Return the JSON-serialisable dict used for hash computation.

        Excludes ``hash``, ``authored_at``, and any other fields whose
        value should not influence equality. Two contracts that differ
        only in those fields share the same hash.
        """
        d = self.model_dump(mode="json", by_alias=True)
        d.pop("hash", None)
        d.pop("authored_at", None)
        return d

    def compute_hash(self) -> str:
        body = self.canonical_body()
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def with_fresh_hash(self) -> "Contract":
        """Return a copy of self with the hash field populated. Used at
        freeze time. The model itself is immutable from the executor's
        perspective once hashed."""
        h = self.compute_hash()
        return self.model_copy(update={"hash": h})


class ContractVersion(BaseModel):
    """Pointer record kept in the version index. Maps contract id →
    list of (version, hash) pairs so the executor can resolve a hash
    back to the file it lives in.
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    hash: str
    authored_at: str
    author: str
    parent_hash: Optional[str] = None
