"""Rigor-validation tests for contract specs.

``validate_step_rigor`` blocks sloppy author-time freezes that would let
the fire-time LLM deviate from declared steps. These tests pin the
behaviour so a future refactor can't quietly relax it.
"""

from __future__ import annotations

import pytest

from app.contracts.schema import (
    Acceptance,
    Contract,
    CronTrigger,
    EmitStep,
    EnforcementMode,
    Gate,
    InputSpec,
    OnDemandTrigger,
    OutputSpec,
    ReasoningStep,
)
from app.contracts.validation import (
    RigorValidationError,
    validate_step_rigor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_input(id_: str = "sales_30d") -> InputSpec:
    return InputSpec(id=id_, loader="bigquery_query", args={"sql": "select 1"})


def _good_json_step(
    id_: str = "analyse_sales",
    user_template: str = "Analyse: {sales_30d}",
) -> ReasoningStep:
    return ReasoningStep(
        id=id_,
        description="Analyse sales data and return structured summary.",
        entry_agent="BigQueryAgent",
        user_template=user_template,
        output=OutputSpec(
            type="json",
            schema={
                "type": "object",
                "properties": {
                    "units_30d": {"type": "integer"},
                    "revenue_30d": {"type": "number"},
                },
                "required": ["units_30d"],
            },
        ),
    )


def _good_emit() -> EmitStep:
    return EmitStep(
        adapter="slack_post",
        args={"channel": "#daily", "content": "Units: {analyse_sales.units_30d}"},
    )


def _minimal_static_contract(**overrides) -> Contract:
    """Contract with no inputs, no reasoning — only emit. The
    daily-tip pattern. Rigor validator should pass it unchanged."""
    base = dict(
        id="daily_tip",
        description="Static daily tip.",
        author="sergey",
        trigger=CronTrigger(cron="0 18 * * MON-FRI", timezone="Europe/Kyiv"),
        emit=[
            EmitStep(
                adapter="slack_post",
                args={"channel": "#tips", "content": "Hi"},
            )
        ],
    )
    base.update(overrides)
    return Contract(**base)


def _full_contract(**overrides) -> Contract:
    """A well-formed multi-step contract used as the baseline for
    negative tests — override one field at a time."""
    base = dict(
        id="daily_audit",
        description="Daily sales audit.",
        author="sergey",
        trigger=CronTrigger(cron="0 13 * * *", timezone="Europe/Kyiv"),
        inputs=[_good_input("sales_30d")],
        reasoning=[_good_json_step()],
        emit=[_good_emit()],
        enforcement=EnforcementMode.STRICT,
    )
    base.update(overrides)
    return Contract(**base)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_static_contract_passes():
    """No reasoning, no inputs — nothing to check. Must not raise."""
    validate_step_rigor(_minimal_static_contract())


def test_full_well_formed_contract_passes():
    validate_step_rigor(_full_contract())


def test_chained_inputs_pass():
    """Inputs may reference each other (loader chaining)."""
    c = _full_contract(
        inputs=[
            _good_input("asins"),
            InputSpec(
                id="keepa_history",
                loader="keepa_get_history",
                args={"asins": "{asins.list}"},
            ),
        ],
        reasoning=[_good_json_step(user_template="Use {asins} and {keepa_history}")],
    )
    validate_step_rigor(c)


def test_text_output_with_min_chars_constraint_passes():
    step = ReasoningStep(
        id="describe",
        description="Write a description.",
        entry_agent="KnowledgeAgent",
        user_template="Describe {sales_30d}",
        output=OutputSpec(type="text", constraints=["min 200 chars"]),
    )
    emit = EmitStep(
        adapter="slack_post",
        args={"channel": "#x", "content": "{describe}"},
    )
    c = _full_contract(reasoning=[step], emit=[emit])
    validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Enforcement mode
# ---------------------------------------------------------------------------


def test_permissive_enforcement_rejected():
    c = _full_contract(enforcement=EnforcementMode.PERMISSIVE)
    with pytest.raises(RigorValidationError, match="permissive"):
        validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Output rigor
# ---------------------------------------------------------------------------


def test_output_none_rejected():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Just look at {sales_30d}",
        output=OutputSpec(type="none"),
    )
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="output.type='none'"):
        validate_step_rigor(c)


def test_json_with_empty_schema_rejected():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Analyse {sales_30d}",
        output=OutputSpec(type="json", schema=None),
    )
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="non-empty schema_"):
        validate_step_rigor(c)


def test_json_with_no_properties_or_items_rejected():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Analyse {sales_30d}",
        output=OutputSpec(type="json", schema={"type": "object"}),
    )
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="properties"):
        validate_step_rigor(c)


def test_json_with_all_optional_properties_rejected():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Analyse {sales_30d}",
        output=OutputSpec(
            type="json",
            schema={
                "type": "object",
                "properties": {"foo": {"type": "string"}},
                # no 'required'
            },
        ),
    )
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="required"):
        validate_step_rigor(c)


def test_json_with_items_passes():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Analyse {sales_30d}",
        output=OutputSpec(
            type="json",
            schema={"type": "array", "items": {"type": "string"}},
        ),
    )
    emit = EmitStep(adapter="slack_post", args={"content": "{x}"})
    c = _full_contract(reasoning=[step], emit=[emit])
    validate_step_rigor(c)


def test_text_without_mechanical_constraint_rejected():
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="KnowledgeAgent",
        user_template="Write about {sales_30d}",
        output=OutputSpec(type="text", constraints=["no markdown"]),
    )
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="mechanical constraint"):
        validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Substep smuggling
# ---------------------------------------------------------------------------


def test_user_template_with_three_numbered_substeps_rejected():
    template = (
        "Do the daily audit.\n"
        "1. Pull sales\n"
        "2. Audit listings\n"
        "3. Recommend actions\n"
    )
    step = _good_json_step(user_template=template)
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="numbered or 'Step N:' substeps"):
        validate_step_rigor(c)


def test_user_template_with_three_keyword_substeps_rejected():
    template = (
        "Daily audit.\n"
        "Step 1: pull sales\n"
        "Step 2: audit listings\n"
        "Step 3: recommend\n"
    )
    step = _good_json_step(user_template=template)
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="numbered or 'Step N:' substeps"):
        validate_step_rigor(c)


def test_user_template_with_two_substeps_passes():
    """Two enumerated items in a description is fine — three is the
    threshold where it crosses into smuggling territory."""
    template = (
        "Analyse sales.\n"
        "1. Compare 7d vs 30d\n"
        "2. Flag SKUs below threshold\n"
    )
    step = _good_json_step(user_template=template)
    c = _full_contract(reasoning=[step])
    validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------


def test_reasoning_template_references_unknown_id_rejected():
    step = _good_json_step(user_template="Compare {sales_30d} and {nonexistent.foo}")
    c = _full_contract(reasoning=[step])
    with pytest.raises(RigorValidationError, match="nonexistent"):
        validate_step_rigor(c)


def test_reasoning_template_references_later_step_id_rejected():
    """A reasoning step may only reference inputs + EARLIER reasoning
    steps. Forward references make no sense and are author error."""
    s1 = _good_json_step(id_="first", user_template="Use {second.foo}")
    s2 = _good_json_step(id_="second", user_template="Use {sales_30d}")
    c = _full_contract(reasoning=[s1, s2])
    with pytest.raises(RigorValidationError, match="second"):
        validate_step_rigor(c)


def test_reasoning_template_references_earlier_step_id_passes():
    s1 = _good_json_step(id_="first", user_template="Use {sales_30d}")
    s2 = _good_json_step(id_="second", user_template="Use {first.units_30d}")
    emit = EmitStep(adapter="slack_post", args={"content": "{second}"})
    c = _full_contract(reasoning=[s1, s2], emit=[emit])
    validate_step_rigor(c)


def test_input_args_reference_unknown_input_rejected():
    bad_input = InputSpec(
        id="enriched",
        loader="bigquery_query",
        args={"sql": "select * from t where x = {nope.value}"},
    )
    c = _full_contract(
        inputs=[_good_input("sales_30d"), bad_input],
        reasoning=[_good_json_step(user_template="Use {sales_30d} and {enriched}")],
    )
    with pytest.raises(RigorValidationError, match="nope"):
        validate_step_rigor(c)


def test_input_args_cannot_reference_reasoning_step():
    """Inputs run before reasoning — they may not reference reasoning
    step ids even though the worker's templating eventually exposes
    them in later phases."""
    bad_input = InputSpec(
        id="enriched",
        loader="bigquery_query",
        args={"sql": "select {analyse_sales.foo}"},
    )
    c = _full_contract(
        inputs=[_good_input("sales_30d"), bad_input],
        reasoning=[_good_json_step()],
    )
    with pytest.raises(RigorValidationError, match="analyse_sales"):
        validate_step_rigor(c)


def test_emit_args_can_reference_reasoning_step():
    emit = EmitStep(
        adapter="slack_post",
        args={"content": "Units: {analyse_sales.units_30d}"},
    )
    c = _full_contract(emit=[emit])
    validate_step_rigor(c)


def test_emit_args_reference_unknown_id_rejected():
    emit = EmitStep(
        adapter="slack_post",
        args={"content": "Units: {missing.thing}"},
    )
    c = _full_contract(emit=[emit])
    with pytest.raises(RigorValidationError, match="missing"):
        validate_step_rigor(c)


def test_gate_args_reference_unknown_id_rejected():
    emit = EmitStep(
        adapter="slack_post",
        args={"content": "Hi"},
        gate=Gate(type="sheet_dedup", args={"key": "{phantom.id}"}),
    )
    c = _full_contract(emit=[emit])
    with pytest.raises(RigorValidationError, match="phantom"):
        validate_step_rigor(c)


def test_acceptance_check_references_unknown_id_rejected():
    c = _full_contract(
        acceptance=Acceptance(checks=["must contain {ghost.value}"])
    )
    with pytest.raises(RigorValidationError, match="ghost"):
        validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_all_errors_reported_in_single_raise():
    """Author gets every issue in one error message, not just the first
    — otherwise the AUTHOR loop fixes one problem per round-trip."""
    step = ReasoningStep(
        id="x",
        description="x",
        entry_agent="BigQueryAgent",
        user_template="Use {unknown_id}",
        output=OutputSpec(type="none"),
    )
    c = _full_contract(
        enforcement=EnforcementMode.PERMISSIVE,
        reasoning=[step],
    )
    with pytest.raises(RigorValidationError) as ei:
        validate_step_rigor(c)
    msg = str(ei.value)
    assert "permissive" in msg
    assert "output.type='none'" in msg
    assert "unknown_id" in msg


# ---------------------------------------------------------------------------
# On-demand trigger does not affect rigor
# ---------------------------------------------------------------------------


def test_on_demand_trigger_passes():
    c = _full_contract(trigger=OnDemandTrigger())
    validate_step_rigor(c)


# ---------------------------------------------------------------------------
# Regression: contract_dry_run must use the freeze() return value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_handles_existing_versions_on_disk(tmp_path, monkeypatch):
    """Repro of the 2026-05-13 ``fba_listing_analysis_b098pc693h``
    incident: ``contract_dry_run`` minted a Contract with version=1,
    hashed it, then called ``store.freeze``. The store, seeing prior
    versions on disk, BUMPED the version (1 → next) and re-hashed
    before persisting — but the dry-run code discarded the return
    and handed the *original* version=1 Contract (with the wrong
    hash) to ``execute_contract``. The worker then asked the store
    for that hash, which wasn't there, and aborted with
    ``no version with hash=...``.

    Fix: ``contract_dry_run`` must adopt the Contract returned by
    ``freeze`` for the execute step.
    """
    from app.contracts.emit import EMIT_ADAPTERS
    from app.contracts.store import ContractStore
    from app.contracts import worker as worker_mod
    from app.tools.contracts import contract_dry_run

    # Per-test store, audit dir, and module-level singletons.
    store = ContractStore(root=str(tmp_path / "contracts"))
    monkeypatch.setattr("app.tools.contracts.contract_store", store)
    monkeypatch.setattr("app.contracts.worker.contract_store", store)
    monkeypatch.setattr(
        "app.contracts.worker._AUDIT_DIR", str(tmp_path / "audit")
    )

    # Capturing emit adapter so dry-run has something to "emit" against.
    captured: list[dict] = []

    async def _cap(args, state):
        captured.append({"args": dict(args), "state_keys": sorted(state.keys())})
        return {"status": "ok", "echo": args}

    prior = EMIT_ADAPTERS.get("record_dryrun")
    EMIT_ADAPTERS["record_dryrun"] = _cap
    try:
        spec = {
            "id": "dry_run_bump_test",
            "description": "Regression: dry-run after multiple freezes.",
            "author": "test",
            "trigger": {"type": "on_demand"},
            "inputs": [
                {
                    "id": "greeting",
                    "loader": "static_param",
                    "args": {"value": "hello"},
                }
            ],
            "emit": [
                {"adapter": "record_dryrun", "args": {"echo": "{greeting}"}}
            ],
        }

        # Seed two prior versions on disk so the next freeze must bump
        # to version >= 3 (and recompute the hash to match the bumped
        # version field).
        from app.contracts.schema import Contract

        v1 = Contract.model_validate(spec).with_fresh_hash()
        store.freeze(v1)

        spec_v2 = dict(spec)
        spec_v2["description"] = "Regression: dry-run after multiple freezes (v2)."
        v2 = Contract.model_validate(spec_v2).with_fresh_hash()
        store.freeze(v2)

        # Now dry-run a THIRD distinct spec. Pre-fix this raised
        # ``no version with hash=...`` because the dry-run code didn't
        # adopt the hash freeze recomputed against the bumped version.
        spec_v3 = dict(spec_v2)
        spec_v3["description"] = "Regression: dry-run after multiple freezes (v3)."

        class _Ctx:
            state = type("S", (), {"to_dict": staticmethod(lambda: {})})()

        out = await contract_dry_run(spec_v3, tool_context=_Ctx())

        assert out.get("__status__") == "ok", f"dry-run returned: {out}"
        # Verify the emit adapter got the rendered arg even on dry-run
        # (dry-run captures rendered args without persisting side
        # effects).
        emit_results = out.get("__emit_results__") or []
        assert any(
            r.get("rendered_args", {}).get("echo") == "hello" for r in emit_results
        ), f"expected rendered echo='hello' in emit_results, got {emit_results!r}"
    finally:
        if prior is None:
            EMIT_ADAPTERS.pop("record_dryrun", None)
        else:
            EMIT_ADAPTERS["record_dryrun"] = prior
