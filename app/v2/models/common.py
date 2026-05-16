"""V2 scheduler — shared reference + policy models.

Small Pydantic shapes referenced by ScheduleSpec, ExecutionPlan,
and the various trigger / event payloads.

Phase 1 keeps these intentionally light — most fields are simple
typed primitives. Registry-aware validation (Channel / Sheet
allowlist enum lookup) lands at phase 6 (registry cache, see
:mod:`app.v2.registry_cache`) and phase 7 (typed authoring
tools) will tighten the fields to enum-typed values via the
resolver call on save. The §12 renumber on 2026-05-15 shifted
the registry cache to step 6; earlier drafts of this docstring
pointed at an older step number.

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 + ``docs/PHASE_1_PLAN.md``
§4.3 for the design intent.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import JsonValue

from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    OnOversizePolicy,
    PausedPendingPolicy,
    RetryStrategy,
    SourceFallbackPolicy,
)


# ---------------------------------------------------------------------------
# Identity refs — who / where, weakly typed pending registry phase
# ---------------------------------------------------------------------------


class UserRef(BaseModel):
    """A platform-scoped user identity.

    ``platform`` is the bot's adapter name (``slack`` /
    ``telegram`` / ``email``). ``user_id`` is the canonical id
    within that platform (slack ``U…``, telegram numeric, email
    address). ``display_name`` is best-effort for diagnostics.
    """

    model_config = ConfigDict(extra="forbid")

    platform: str
    user_id: str
    display_name: Optional[str] = None


class ChannelRef(BaseModel):
    """A channel-style destination (Slack channel, Telegram
    group, etc.). Phase 1 keeps this as raw strings; the
    phase-6 registry cache
    (:mod:`app.v2.registry_cache.resolver`) is the
    validation seam, wired through the authoring path in
    phase 7.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description="Adapter kind — 'slack', 'telegram', etc. "
        "Treated opaque in phase 1; the phase-6 registry "
        "cache resolver enums this at authoring time.",
    )
    external_id: str = Field(
        description="Platform-native channel id (e.g. 'C012ABCDE' "
        "for Slack; numeric chat_id for Telegram). Validated "
        "against the phase-6 registry cache from phase 7 "
        "onward.",
    )


class SheetRef(BaseModel):
    """A Google Sheets reference. Phase 1 stores ids verbatim;
    the phase-6 registry cache
    (:mod:`app.v2.registry_cache.resolver`) is the
    validation seam, wired through the authoring path in
    phase 7."""

    model_config = ConfigDict(extra="forbid")

    spreadsheet_id: str
    range_: str = Field(
        ...,
        alias="range",
        description="A1 notation (e.g. 'Sheet1!A:C'). The "
        "underscore-suffix is purely so the Python identifier "
        "doesn't shadow the ``range`` builtin.",
    )


class TemplateRef(BaseModel):
    """Identifies the template (and version) that produced a
    ScheduleSpec. ``None`` for CustomFlow specs.

    Phase 1 makes no opinion about how versioning numbers
    advance — phase 8 ships the first template
    (`OneOffReminder`) and pins this contract then.

    Phase 9 (2026-05-15) adds the optional ``args`` field so
    emit-only templates can carry per-instance payload (e.g.
    the ``OneOffReminder`` reminder text) on the spec without
    an ExecutionPlan. The type is
    :class:`pydantic.types.JsonValue` — a recursive JSON-
    primitive union — so non-JSON values (``datetime``,
    ``set``, custom classes, …) raise
    :class:`pydantic.ValidationError` at construction and
    cannot leak into ``ScheduleSpec.compute_hash``.

    Hash drift on pre-amendment specs is avoided by
    :meth:`ScheduleSpec.canonical_body` stripping the
    ``args`` key when ``args is None`` (see the
    ``canonical_body`` body comment). Pre-amendment specs
    on disk (where the field did not exist) re-validate as
    ``args=None`` and hash identically to their original
    form.

    Per-template arg validation runs in the template
    builder (e.g. ``build_one_off_reminder``) BEFORE the
    typed-tool flow; this model treats ``args`` as opaque
    JSON.

    References:
    - ``docs/CONTRACTS_V2_DESIGN.md`` §5.2 (per-template
      payload).
    - ``docs/PHASE_9_PLAN.md`` §0 + §3.1.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    args: Optional[dict[str, JsonValue]] = None


# ---------------------------------------------------------------------------
# Policy objects on ScheduleSpec
# ---------------------------------------------------------------------------


class Delivery(BaseModel):
    """Where a ScheduleSpec delivers its output.

    ``target_session_id`` is the channel / DM session that
    receives the emit's user-visible output. Pre-2026-05-13
    this conflated with the creator's session; the v2 model
    keeps the two split per ``docs/CONTRACTS_V2_DESIGN.md``
    §7.2 (L1/L2/L3 fallback chain).

    ``fallback_policy`` decides what happens when L1 delivery
    fails. Author picks one explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    target_session_id: str
    fallback_policy: DeliveryFallbackPolicy = Field(
        description=(
            "L1 → L2 fallback policy. See §7.2 of the design "
            "contract."
        ),
    )


class RetryPolicy(BaseModel):
    """Backoff + max-attempt policy for ``retry_later`` failure
    handling."""

    model_config = ConfigDict(extra="forbid")

    strategy: RetryStrategy
    base_seconds: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


class FailurePolicy(BaseModel):
    """ScheduleSpec-level failure handling. Per-Run handlers
    (``on_failure`` on the ExecutionPlan) can override.

    Phase-14 (2026-05-16) adds the optional
    ``paused_pending_policy`` field — the run-disposition-on-
    pause policy. It is HOUSED here (NOT as a literal top-level
    ``ScheduleSpec`` field as an early phase-14 plan draft
    wrongly assumed) because ``FailurePolicy`` is persisted via
    the ``failure_json`` column, which already round-trips
    through ``insert_schedule`` / ``_row_to_spec`` with NO DDL
    and NO v002 migration — and ``FailurePolicy`` is the
    defensible semantic home for "what to do with in-flight /
    pending work on a lifecycle state change". The (α) fork
    ruling (``docs/PHASE_14_PLAN.md`` §9.1) records the
    code-verified premise-bust (the ``schedules`` table is
    column-decomposed, not a spec JSON blob) and why this
    locus is correct.

    The phase-9 ``TemplateRef.args`` nested-optional precedent
    is applied verbatim: hash drift on pre-phase-14 specs is
    avoided by :meth:`ScheduleSpec.canonical_body` stripping
    the ``paused_pending_policy`` key from the serialised
    ``failure`` dict when it is unset (None) or the
    ``let_complete`` default (see that method's body comment).
    ``cancel_pending`` participates in the hash — a real
    behaviour change → new version.

    References:
    - ``docs/CONTRACTS_V2_DESIGN.md`` §7 (archive-vs-pause
      asymmetry).
    - ``docs/PHASE_14_PLAN.md`` §9.1 (the (α) fork ruling).
    """

    model_config = ConfigDict(extra="forbid")

    on_failure_action: FailureActionType = FailureActionType.ALERT_ADMIN
    retry_policy: Optional[RetryPolicy] = None
    paused_pending_policy: Optional[PausedPendingPolicy] = Field(
        default=None,
        description=(
            "Run-disposition-on-pause policy. None / "
            "'let_complete' (default) leaves pending Runs "
            "untouched when the schedule is paused (pre-phase-14 "
            "no-op; least-surprise, design §7). 'cancel_pending' "
            "cancels them in the pause transaction via the same "
            "seam as schedule_archive (same run_cancelled event, "
            "same PENDING→CANCELLED transition). Elided from the "
            "canonical hash body when unset/default so "
            "pre-phase-14 specs hash byte-identically "
            "(phase-9 TemplateRef.args precedent)."
        ),
    )


class AuditPolicy(BaseModel):
    """Per-ScheduleSpec audit retention + snapshot caps."""

    model_config = ConfigDict(extra="forbid")

    keep_last_n_snapshots: int = Field(default=30, ge=0)
    dedup_by_content_hash: bool = True
    redact_fields: list[str] = Field(default_factory=list)
    max_snapshot_bytes: int = Field(default=1_000_000, ge=0)
    on_oversize: OnOversizePolicy = OnOversizePolicy.FAIL_AND_ALERT


class LiveSourceCachePolicy(BaseModel):
    """Per-source cache policy attached to a LiveSourceRef.

    Phase 1 ships the policy model; the cache layer itself
    lands at phase 7 (source loaders). Author specifies all
    three knobs explicitly — there is no global default.
    """

    model_config = ConfigDict(extra="forbid")

    cache_ttl_seconds: int = Field(ge=0)
    stale_max_age_seconds: int = Field(ge=0)
    fallback_policy: SourceFallbackPolicy
