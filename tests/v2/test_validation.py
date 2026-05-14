"""Tests for ``app.v2.validation``.

Pins per ``docs/PHASE_2_PLAN.md`` slice 3:

- Hash present + matches compute_hash() for frozen specs.
- execution_plan_hash, when set, matches 64-char lowercase hex.
- Reminder rule: execution_plan_hash is None ⇒ trigger is
  OneOffTrigger.
- When the caller supplies an in-memory plan index, the
  validator refuses a referenced hash that isn't in the index.
- When BOTH plan body and registries are supplied, adapter
  references in the plan resolve against the registries.
- Validator returns ALL collected issues, not just the first.
- Module is pure — no I/O imports, no runtime dispatch surface.
"""

from __future__ import annotations

import inspect

import pytest

from app.v2.descriptors.emit import EmitDescriptor
from app.v2.descriptors.source import SourceDescriptor
from app.v2.descriptors.tool import ToolDescriptor
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    SelectionMethod,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
    ReasoningStep,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.registry import EmitRegistry, SourceRegistry, ToolRegistry
from app.v2.tool_tags import ToolCapabilityTag
from app.v2 import validation as validation_mod
from app.v2.validation import (
    RegistrySnapshot,
    ValidationIssue,
    ValidationResult,
    validate_schedule_spec,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_OWNER = UserRef(
    platform="telegram", user_id="330959414", display_name="Sergey"
)
_DELIVERY = Delivery(
    target_session_id="sl_C0B2LJRS8D8",
    fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
)
_FAILURE = FailurePolicy(on_failure_action=FailureActionType.ALERT_ADMIN)
_AUDIT = AuditPolicy()


def _reminder_spec(**overrides) -> ScheduleSpec:
    """One-off reminder with no ExecutionPlan."""
    base = dict(
        id="ping_sergey",
        owner=_OWNER,
        description="One-off reminder ping",
        trigger=OneOffTrigger(at_iso_datetime="2026-06-01T09:00:00+00:00"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash=None,
    )
    base.update(overrides)
    return ScheduleSpec(**base).with_fresh_hash()


def _cron_spec(**overrides) -> ScheduleSpec:
    """Cron-triggered spec that requires an ExecutionPlan."""
    base = dict(
        id="daily_audit",
        owner=_OWNER,
        description="Daily FBA audit roll-up",
        trigger=CronTrigger(cron="0 18 * * *", timezone="Europe/Kyiv"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash="a" * 64,
    )
    base.update(overrides)
    return ScheduleSpec(**base).with_fresh_hash()


def _plan_with_refs(
    *,
    loader_name: str = "source_drive_file",
    tool_name: str = "slack_post_message",
    adapter_name: str = "slack_post_message",
) -> ExecutionPlan:
    plan = ExecutionPlan(
        id="daily_audit_plan",
        description="walk the audit",
        author="sergey",
        inputs=[InputSpec(id="src", loader=loader_name)],
        reasoning=[
            ReasoningStep(
                id="reason",
                entry_agent="CoordinatorAgent",
                tools=[tool_name],
                user_template="t",
            )
        ],
        emit=[
            EmitStep(id="emit_one", adapter=adapter_name),
        ],
    )
    return plan.with_fresh_hash()


# ===========================================================================
# Happy-path
# ===========================================================================


def test_one_off_reminder_with_no_plan_passes():
    result = validate_schedule_spec(_reminder_spec())
    assert result.ok is True
    assert result.errors() == []


def test_cron_with_valid_plan_hash_passes_without_index():
    """No execution_plans index supplied; only the format +
    reminder-rule + self-hash checks fire."""
    result = validate_schedule_spec(_cron_spec())
    assert result.ok is True


def test_cron_with_known_plan_hash_passes():
    plan = _plan_with_refs()
    spec = _cron_spec(execution_plan_hash=plan.hash)
    result = validate_schedule_spec(spec, execution_plans={plan.hash: plan})
    assert result.ok is True


def test_cron_with_known_plan_and_complete_registries_passes():
    plan = _plan_with_refs()
    spec = _cron_spec(execution_plan_hash=plan.hash)

    tools = ToolRegistry()
    tools.register(
        ToolDescriptor(
            name="slack_post_message",
            description="post to slack",
            tags={
                ToolCapabilityTag.WRITE_EXTERNAL,
                ToolCapabilityTag.SEND_MESSAGE,
            },
            module="app.v2.adapters.slack",
        )
    )
    sources = SourceRegistry()
    sources.register(
        SourceDescriptor(
            id="source_drive_file",
            description="load drive file",
            tags={
                ToolCapabilityTag.READ_EXTERNAL,
                ToolCapabilityTag.USES_OAUTH,
            },
            supports_versioning=True,
            supported_selection_methods=[SelectionMethod.STABLE_ID],
        )
    )
    emits = EmitRegistry()
    emits.register(
        EmitDescriptor(
            id="slack_post_message",
            description="post message to slack",
            tags={
                ToolCapabilityTag.WRITE_EXTERNAL,
                ToolCapabilityTag.SEND_MESSAGE,
            },
            target_kind="slack",
            supports_native_dedup=True,
        )
    )

    snap = RegistrySnapshot(tools=tools, sources=sources, emits=emits)
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=snap
    )
    assert result.ok is True, [i.message for i in result.errors()]


# ===========================================================================
# Hash present + matches
# ===========================================================================


def test_empty_hash_is_error():
    spec = ScheduleSpec(
        id="ping",
        owner=_OWNER,
        description="One-off reminder ping",
        trigger=OneOffTrigger(at_iso_datetime="2026-06-01T09:00:00+00:00"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
    )
    # NOTE: not calling with_fresh_hash() so hash stays ""
    result = validate_schedule_spec(spec)
    codes = [i.code for i in result.errors()]
    assert "hash_required" in codes
    assert result.ok is False


def test_mismatched_hash_is_error():
    spec = _reminder_spec()
    # Tamper: replace hash with a different valid-looking digest.
    tampered = spec.model_copy(update={"hash": "b" * 64})
    result = validate_schedule_spec(tampered)
    codes = [i.code for i in result.errors()]
    assert "hash_mismatch" in codes


def test_hash_validator_emits_at_most_one_hash_issue_when_empty():
    """When the hash is empty, the validator must not also emit a
    spurious mismatch issue — the empty case is one error, not
    two."""
    spec = ScheduleSpec(
        id="ping",
        owner=_OWNER,
        description="One-off reminder ping",
        trigger=OneOffTrigger(at_iso_datetime="2026-06-01T09:00:00+00:00"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
    )
    result = validate_schedule_spec(spec)
    hash_codes = [
        i.code for i in result.errors() if i.path == "hash"
    ]
    assert hash_codes == ["hash_required"]


# ===========================================================================
# execution_plan_hash format
# ===========================================================================


@pytest.mark.parametrize(
    "bad_hash",
    [
        "A" * 64,                  # uppercase
        "a" * 63,                  # too short
        "a" * 65,                  # too long
        "sha256:" + "a" * 64,      # prefixed (snapshot format leak)
        "z" * 64,                  # non-hex chars
        "",                        # empty string treated as bad format
    ],
)
def test_execution_plan_hash_bad_format_is_error(bad_hash):
    """Build the bad-hash spec without going through with_fresh_hash
    so the BAD execution_plan_hash isn't overwritten."""
    spec = _cron_spec(execution_plan_hash=bad_hash)
    result = validate_schedule_spec(spec)
    codes = [i.code for i in result.errors()]
    assert "execution_plan_hash_bad_format" in codes


def test_execution_plan_hash_valid_format_passes_format_check():
    spec = _cron_spec(execution_plan_hash="0" * 32 + "a" * 32)
    result = validate_schedule_spec(spec)
    codes = [i.code for i in result.errors()]
    assert "execution_plan_hash_bad_format" not in codes


# ===========================================================================
# Reminder rule
# ===========================================================================


def test_one_off_with_no_plan_is_allowed():
    result = validate_schedule_spec(_reminder_spec())
    assert "missing_execution_plan_for_complex_trigger" not in [
        i.code for i in result.errors()
    ]


@pytest.mark.parametrize(
    "trigger",
    [
        CronTrigger(cron="0 9 * * *", timezone="UTC"),
        IntervalTrigger(every_seconds=3600),
        EventTrigger(event="some_event"),
        ConditionalTrigger(gate="some_gate", poll_seconds=60),
    ],
)
def test_non_one_off_trigger_requires_execution_plan(trigger):
    spec = ScheduleSpec(
        id="needs_plan",
        owner=_OWNER,
        description="non-one-off trigger without a plan",
        trigger=trigger,
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash=None,
    ).with_fresh_hash()
    result = validate_schedule_spec(spec)
    codes = [i.code for i in result.errors()]
    assert "missing_execution_plan_for_complex_trigger" in codes


# ===========================================================================
# Plan-index existence
# ===========================================================================


def test_missing_plan_hash_in_index_is_error():
    spec = _cron_spec(execution_plan_hash="a" * 64)
    result = validate_schedule_spec(spec, execution_plans={})
    codes = [i.code for i in result.errors()]
    assert "execution_plan_hash_unknown" in codes


def test_known_plan_hash_in_index_passes_existence_check():
    plan = _plan_with_refs()
    spec = _cron_spec(execution_plan_hash=plan.hash)
    result = validate_schedule_spec(spec, execution_plans={plan.hash: plan})
    assert "execution_plan_hash_unknown" not in [
        i.code for i in result.errors()
    ]


def test_no_index_supplied_skips_existence_check():
    """A caller that doesn't pass execution_plans must not
    trigger the existence error — the validator must default to
    not knowing the answer rather than fail closed."""
    spec = _cron_spec(execution_plan_hash="a" * 64)
    result = validate_schedule_spec(spec, execution_plans=None)
    assert "execution_plan_hash_unknown" not in [
        i.code for i in result.errors()
    ]


# ===========================================================================
# Adapter walk
# ===========================================================================


def test_unknown_loader_is_error():
    plan = _plan_with_refs(loader_name="source_ghost")
    spec = _cron_spec(execution_plan_hash=plan.hash)
    sources = SourceRegistry()  # empty
    snap = RegistrySnapshot(sources=sources)
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=snap
    )
    codes = [i.code for i in result.errors()]
    assert "unknown_source_loader" in codes


def test_unknown_tool_is_error():
    plan = _plan_with_refs(tool_name="ghost_tool")
    spec = _cron_spec(execution_plan_hash=plan.hash)
    tools = ToolRegistry()
    snap = RegistrySnapshot(tools=tools)
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=snap
    )
    codes = [i.code for i in result.errors()]
    assert "unknown_tool" in codes


def test_unknown_emit_adapter_is_error():
    plan = _plan_with_refs(adapter_name="ghost_adapter")
    spec = _cron_spec(execution_plan_hash=plan.hash)
    emits = EmitRegistry()
    snap = RegistrySnapshot(emits=emits)
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=snap
    )
    codes = [i.code for i in result.errors()]
    assert "unknown_emit_adapter" in codes


def test_adapter_walk_skipped_when_registries_none():
    plan = _plan_with_refs(adapter_name="ghost_adapter")
    spec = _cron_spec(execution_plan_hash=plan.hash)
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=None
    )
    assert "unknown_emit_adapter" not in [
        i.code for i in result.errors()
    ]


def test_adapter_walk_skipped_when_plan_index_missing_target():
    """When the plan hash is in the spec but the supplied index
    doesn't have the body, adapter walk must NOT raise; the
    missing-hash error already covers the user-visible problem."""
    spec = _cron_spec(execution_plan_hash="a" * 64)
    sources = SourceRegistry()
    snap = RegistrySnapshot(sources=sources)
    result = validate_schedule_spec(
        spec, execution_plans={}, registries=snap
    )
    codes = [i.code for i in result.errors()]
    assert "execution_plan_hash_unknown" in codes
    assert "unknown_source_loader" not in codes  # plan body absent


# ===========================================================================
# Multiple issues collected, not just first
# ===========================================================================


def test_multiple_issues_returned():
    """Bad hash format + missing plan in index + unknown adapter
    walked from the index — three independent error codes
    surface in one call."""
    # Spec: hash mismatch on bad execution_plan_hash format AND
    # a plan body that references an unknown adapter.
    spec = _cron_spec(execution_plan_hash="zzz")  # bad format
    # Build a plan with a different valid hash and register
    # under the hash in the spec to force the existence check to
    # also fire ... actually the spec's execution_plan_hash is
    # "zzz" so the existence check will fail too.
    plan = _plan_with_refs(adapter_name="ghost_adapter")
    emits = EmitRegistry()
    snap = RegistrySnapshot(emits=emits)
    result = validate_schedule_spec(
        spec,
        execution_plans={plan.hash: plan},  # different hash
        registries=snap,
    )
    codes = {i.code for i in result.errors()}
    # Bad format + missing-in-index both fire.
    assert "execution_plan_hash_bad_format" in codes
    assert "execution_plan_hash_unknown" in codes
    # Adapter walk skips because plan body for spec's hash is
    # absent — so no unknown_emit_adapter here (covered by other
    # tests).


def test_validation_result_partitioning():
    r = ValidationResult(
        issues=[
            ValidationIssue(
                code="x", severity="error", path="a", message="m"
            ),
            ValidationIssue(
                code="y",
                severity="warning",
                path="b",
                message="w",
            ),
        ]
    )
    assert r.ok is False
    assert len(r.errors()) == 1
    assert len(r.warnings()) == 1


def test_validation_result_ok_when_only_warnings():
    r = ValidationResult(
        issues=[
            ValidationIssue(
                code="x",
                severity="warning",
                path="a",
                message="m",
            ),
        ]
    )
    assert r.ok is True


# ===========================================================================
# RegistrySnapshot semantics
# ===========================================================================


def test_registry_snapshot_defaults_to_all_none():
    snap = RegistrySnapshot()
    assert snap.tools is None
    assert snap.sources is None
    assert snap.emits is None


def test_registry_snapshot_is_frozen():
    snap = RegistrySnapshot()
    with pytest.raises(Exception):
        snap.tools = ToolRegistry()  # type: ignore[misc]


def test_partial_registries_only_check_supplied_layers():
    """Only sources supplied → no errors for unknown tools/adapters
    in the plan (because those registries are None)."""
    plan = _plan_with_refs(
        loader_name="source_ghost",
        tool_name="ghost_tool",
        adapter_name="ghost_adapter",
    )
    spec = _cron_spec(execution_plan_hash=plan.hash)
    sources = SourceRegistry()
    snap = RegistrySnapshot(sources=sources)  # tools/emits None
    result = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=snap
    )
    codes = {i.code for i in result.errors()}
    assert "unknown_source_loader" in codes
    assert "unknown_tool" not in codes
    assert "unknown_emit_adapter" not in codes


# ===========================================================================
# No-execution smoke test
# ===========================================================================


def test_validation_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "sqlite3",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(validation_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"app.v2.validation imports I/O / storage libs that imply "
        f"runtime: {sorted(leaked)}. Phase 2 is metadata only."
    )


def test_validation_module_has_no_dispatch_callables():
    """Public callables in the validation module are limited to
    the validator entry point plus the model classes. Anything
    named ``call`` / ``invoke`` / ``dispatch`` / ``execute`` /
    ``run`` / ``send`` is a phase-boundary violation."""
    forbidden_names = {"call", "invoke", "dispatch", "execute", "run", "send"}
    for name, member in vars(validation_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden_names:
            pytest.fail(
                f"validation module exposes execution-suggestive "
                f"callable: {name}"
            )
