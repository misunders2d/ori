"""Schema-level tests for contract data classes — validation rules,
hash determinism, version chaining.

The on-disk store is exercised separately in
``tests/test_contracts_store.py``.
"""

from __future__ import annotations

import pytest

from app.contracts.schema import (
    Acceptance,
    Contract,
    CronTrigger,
    EmitStep,
    EnforcementMode,
    FailureAction,
    FailureActionType,
    Gate,
    InputSpec,
    OnDemandTrigger,
    OutputSpec,
    ReasoningStep,
    Retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_contract(**overrides) -> Contract:
    """A small valid contract used as the starting point for negative
    tests. Static daily Slack tip: no inputs, no reasoning, one emit.
    """
    base = dict(
        id="daily_tip",
        description="Daily AI tip post.",
        author="sergey",
        trigger=CronTrigger(cron="0 18 * * MON-FRI", timezone="Europe/Kyiv"),
        emit=[
            EmitStep(
                adapter="slack_post",
                args={"channel": "#ai_in_mellanni", "content": "Hello world."},
            )
        ],
    )
    base.update(overrides)
    return Contract(**base)


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


def test_contract_requires_at_least_one_emit():
    """A contract with empty ``emit`` has no side effect and is useless;
    the schema rejects it at construction time."""
    with pytest.raises(ValueError, match="at least one emit"):
        Contract(
            id="ghost",
            description="x",
            author="sergey",
            trigger=OnDemandTrigger(),
            emit=[],
        )


def test_contract_id_pattern_enforced():
    """Contract ids are used as on-disk directory names and APScheduler
    job ids — restrict to snake_case ASCII to avoid path / id surprises.
    """
    with pytest.raises(ValueError):
        _minimal_contract(id="Has Spaces")
    with pytest.raises(ValueError):
        _minimal_contract(id="UPPERCASE")
    with pytest.raises(ValueError):
        _minimal_contract(id="123starts_with_digit")
    # snake_case OK
    _minimal_contract(id="ok_id_42")


def test_input_id_pattern_enforced():
    """Input ids show up as state keys + template placeholders; same
    snake_case rule keeps templates parseable."""
    with pytest.raises(ValueError):
        InputSpec(id="UPPER", loader="static_param")


def test_reasoning_step_id_pattern_enforced():
    with pytest.raises(ValueError):
        ReasoningStep(
            id="UPPER",
            entry_agent="AmazonHeadAgent",
            user_template="...",
        )


# ---------------------------------------------------------------------------
# Trigger discrimination
# ---------------------------------------------------------------------------


def test_each_trigger_type_round_trips():
    """Pydantic union discrimination needs the ``type`` literal on each
    variant — make sure all three deserialise correctly."""
    cron = CronTrigger(cron="0 9 * * *")
    assert cron.type == "cron"

    ondem = OnDemandTrigger()
    assert ondem.type == "on_demand"


# ---------------------------------------------------------------------------
# Hash determinism
# ---------------------------------------------------------------------------


def test_compute_hash_is_deterministic():
    """Same inputs → same hash, regardless of how the dict happened to
    serialise. ``sort_keys=True`` in the canonicalisation should make
    this stable across pydantic versions."""
    c1 = _minimal_contract()
    c2 = _minimal_contract()
    assert c1.compute_hash() == c2.compute_hash()


def test_compute_hash_ignores_authored_at_and_hash_field():
    """authored_at is a wall-clock timestamp that differs by milliseconds
    between two ``_minimal_contract()`` calls. If it counted toward the
    hash, ``with_fresh_hash`` would produce different hashes each call.
    Likewise the hash field itself must not be self-referential."""
    c = _minimal_contract()
    different_ts = c.model_copy(update={"authored_at": "2099-01-01T00:00:00"})
    assert c.compute_hash() == different_ts.compute_hash()

    pre_hash = c.compute_hash()
    populated = c.model_copy(update={"hash": "fakehash"})
    assert populated.compute_hash() == pre_hash


def test_compute_hash_differs_when_meaningful_field_changes():
    """A change to any field that does matter (description, emit args,
    schedule cron, reasoning prompt) must produce a different hash."""
    c1 = _minimal_contract()
    c2 = _minimal_contract(description="A different description")
    assert c1.compute_hash() != c2.compute_hash()

    c3 = _minimal_contract(
        trigger=CronTrigger(cron="30 18 * * MON-FRI", timezone="Europe/Kyiv")
    )
    assert c1.compute_hash() != c3.compute_hash()


def test_with_fresh_hash_populates_hash_field():
    c = _minimal_contract()
    assert c.hash == ""
    frozen = c.with_fresh_hash()
    assert frozen.hash != ""
    assert len(frozen.hash) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Reasoning step rich shape
# ---------------------------------------------------------------------------


def test_reasoning_step_with_full_routing():
    """Smoke test: a representative reasoning step (the kind the bot
    will draft for the FBA news digest) constructs cleanly."""
    step = ReasoningStep(
        id="digest",
        description="Cross-reference fresh news against KB.",
        entry_agent="AmazonHeadAgent",
        transfers_allowed=["AmazonHeadAgent", "AmazonMemoryAgent", "KnowledgeAgent"],
        tools=["memory_search", "web_fetch"],
        model="google/gemini-3-flash-preview",
        user_template="news: {news}\nkb: {kb_snapshot}",
        output=OutputSpec(
            type="json",
            schema={"type": "object", "required": ["top_items"]},
        ),
        retry=Retry(on_validation_fail=1, on_tool_error=2),
        max_tool_calls=15,
    )
    assert step.transfers_allowed[0] == "AmazonHeadAgent"
    assert step.tools == ["memory_search", "web_fetch"]
    assert step.output.type == "json"


def test_emit_step_with_gate():
    """Gates wrap the existing ``sheet_dedup`` pattern (read sheet,
    fail if today's row exists) — make sure the model accepts the
    minimal valid shape."""
    e = EmitStep(
        adapter="slack_post",
        args={"channel": "#x", "content": "..."},
        gate=Gate(type="sheet_dedup", args={"source": "1abc...", "key": "today"}),
        abort_on_gate_fail=True,
    )
    assert e.gate.type == "sheet_dedup"
    assert e.abort_on_gate_fail is True


# ---------------------------------------------------------------------------
# Failure action defaults
# ---------------------------------------------------------------------------


def test_failure_action_defaults_to_alert_admin_with_abort():
    """The safe default is to alert + abort — never half-post a contract
    that ran into trouble."""
    fa = FailureAction()
    assert fa.action == FailureActionType.ALERT_ADMIN
    assert fa.abort is True


# ---------------------------------------------------------------------------
# Extra-fields strictness
# ---------------------------------------------------------------------------


def test_enforcement_defaults_to_strict():
    """The default mode is the safe one: every reasoning step is
    load-bearing once the contract is frozen. Authoring helpers
    explicitly choose ``PERMISSIVE`` only for the experimental author
    loop — the freeze pipeline refuses to persist a permissive
    contract."""
    c = _minimal_contract()
    assert c.enforcement is EnforcementMode.STRICT


def test_enforcement_change_alters_hash():
    """Flipping ``enforcement`` is a meaningful semantic change — it
    must produce a different hash so versions don't collide and
    auditors can see the toggle."""
    c1 = _minimal_contract()
    c2 = _minimal_contract(enforcement=EnforcementMode.PERMISSIVE)
    assert c1.compute_hash() != c2.compute_hash()


def test_unknown_fields_rejected():
    """``extra='forbid'`` on every model prevents typos in authored
    YAML from silently doing the wrong thing — ``transfer_allowed`` (no
    s) would otherwise look fine but actually leave the default empty
    list in place."""
    with pytest.raises(ValueError):
        ReasoningStep(
            id="x",
            entry_agent="A",
            user_template="t",
            transfer_allowed=["A"],  # typo: missing 's'
        )
