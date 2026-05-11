"""Worker tests — end-to-end execution paths with mocked LLM + adapters.

What's tested
-------------
- The happy path: inputs → reasoning → emit, JSON schema validates,
  state propagates through templates, audit log captures each phase.
- Hash drift between load and execute: aborted with a clear error.
- Reasoning validation failure: one retry, retry-with-feedback prompt,
  then ``on_failure`` if still bad.
- Emit gate behaviour: pass-through, skip-this-emit, abort-the-contract.
- Dry-run: emits are not actually called; rendered args are returned.
- Loader template rendering: prior-step state reachable from later args.
- Enforcement mode: non-STRICT contracts are refused.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest

from app.contracts import worker as worker_mod
from app.contracts.emit import (
    EMIT_ADAPTERS,
    GATES,
    register_adapter,
    register_gate,
)
from app.contracts.schema import (
    Contract,
    CronTrigger,
    EmitStep,
    EnforcementMode,
    FailureAction,
    FailureActionType,
    Gate,
    InputSpec,
    OutputSpec,
    ReasoningStep,
    Retry,
)
from app.contracts.store import ContractStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_store(tmp_path, monkeypatch) -> ContractStore:
    """Per-test contract store + audit dir + module singleton override."""
    store = ContractStore(root=str(tmp_path / "contracts"))
    monkeypatch.setattr("app.contracts.worker.contract_store", store)
    monkeypatch.setattr("app.contracts.worker._AUDIT_DIR", str(tmp_path / "audit"))
    return store


@pytest.fixture
def emit_recorder(monkeypatch):
    """Register a fresh ``record`` emit adapter that appends to a
    test-private list. Each test gets its own list so cross-test
    pollution can't mask missing emits.

    The previous fixture used a registration guard, which meant the
    adapter installed by the FIRST test stayed bound to that test's
    list — later tests appeared to record nothing.
    """
    calls: list[dict[str, Any]] = []

    async def _rec(args, state):
        calls.append({"args": dict(args), "state_keys": sorted(state.keys())})
        return {"status": "ok", "echo": args}

    # Install (overwriting any prior binding) for the duration of the test.
    prior = EMIT_ADAPTERS.get("record")
    EMIT_ADAPTERS["record"] = _rec
    try:
        yield calls
    finally:
        if prior is None:
            EMIT_ADAPTERS.pop("record", None)
        else:
            EMIT_ADAPTERS["record"] = prior


def _make_contract(reasoning=None, emit=None, **overrides) -> Contract:
    base = dict(
        id="t_contract",
        description="Test contract.",
        author="t",
        trigger=CronTrigger(cron="0 0 * * *", timezone="UTC"),
        inputs=[
            InputSpec(id="greeting", loader="static_param", args={"value": "hello"})
        ],
        reasoning=reasoning if reasoning is not None else [],
        emit=emit
        or [
            EmitStep(
                adapter="record",
                args={"echo": "{greeting}"},
            )
        ],
    )
    base.update(overrides)
    return Contract(**base)


# ---------------------------------------------------------------------------
# Happy path: no reasoning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_reasoning_emit_runs_with_rendered_state(
    isolated_store, emit_recorder
):
    """Static contract — input → emit. No LLM. Argument template
    rendered against state. Records the emit call."""
    c = isolated_store.freeze(_make_contract())

    out = await worker_mod.execute_contract(c)

    assert out["__status__"] == "ok"
    assert out["greeting"] == "hello"
    assert emit_recorder == [
        {
            "args": {"echo": "hello"},
            "state_keys": pytest.approx(
                sorted(emit_recorder[0]["state_keys"])
            ),  # arbitrary order
        }
    ]


@pytest.mark.asyncio
async def test_audit_log_written_per_fire(isolated_store, emit_recorder, tmp_path):
    """Every fire writes a JSONL audit trail with one event per phase."""
    c = isolated_store.freeze(_make_contract())
    await worker_mod.execute_contract(c)

    audit_dir = pathlib.Path(tmp_path / "audit" / c.id)
    files = list(audit_dir.glob("*.jsonl"))
    assert len(files) == 1

    lines = [json.loads(l) for l in files[0].read_text().splitlines()]
    phases = [e["phase"] for e in lines]
    assert "fire_start" in phases
    assert "input" in phases
    assert "emit" in phases
    assert "fire_end" in phases


# ---------------------------------------------------------------------------
# Hash drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aborts_when_disk_body_tampered(isolated_store, emit_recorder):
    """If the on-disk body was rewritten after freeze, the worker must
    refuse to fire — the stored hash mismatch is the canary."""
    c = isolated_store.freeze(_make_contract())

    # Rewrite the body to flip the description.
    body_path = pathlib.Path(
        isolated_store._version_path(c.id, c.version, c.hash)
    )
    raw = json.loads(body_path.read_text())
    raw["description"] = "TAMPERED"
    body_path.write_text(json.dumps(raw))

    with pytest.raises(worker_mod.ContractFireError, match="integrity check"):
        await worker_mod.execute_contract(c)


# ---------------------------------------------------------------------------
# Enforcement mode gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_to_fire_permissive_contract(isolated_store, emit_recorder):
    """Production contracts must be STRICT. The author flow refuses to
    freeze a permissive one (P6), but if one slips through, the worker
    also refuses."""
    c = isolated_store.freeze(
        _make_contract(enforcement=EnforcementMode.PERMISSIVE)
    )
    with pytest.raises(worker_mod.ContractFireError, match="not in STRICT"):
        await worker_mod.execute_contract(c)


# ---------------------------------------------------------------------------
# Reasoning with mocked LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_step_with_json_schema_happy_path(
    isolated_store, emit_recorder, monkeypatch
):
    """LLM returns valid JSON matching the schema → state[step.id] is
    the parsed object → emit can template into it."""
    async def fake_llm(step, rendered_user_text, previous_validation_error=None):
        return '{"top_items": [{"title": "T1"}, {"title": "T2"}, {"title": "T3"}]}'

    monkeypatch.setattr(worker_mod, "_invoke_llm_for_step", fake_llm)

    c = isolated_store.freeze(
        _make_contract(
            reasoning=[
                ReasoningStep(
                    id="digest",
                    entry_agent="CoordinatorAgent",
                    user_template="Process: {greeting}",
                    output=OutputSpec(
                        type="json",
                        schema={
                            "type": "object",
                            "required": ["top_items"],
                            "properties": {
                                "top_items": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 3,
                                }
                            },
                        },
                    ),
                )
            ],
            emit=[
                EmitStep(
                    adapter="record",
                    args={"first_title": "{digest.top_items[0].title}"},
                )
            ],
        )
    )

    out = await worker_mod.execute_contract(c)
    assert out["__status__"] == "ok"
    assert out["digest"]["top_items"][0]["title"] == "T1"
    assert emit_recorder[-1]["args"]["first_title"] == "T1"


@pytest.mark.asyncio
async def test_reasoning_retries_once_with_feedback_on_invalid_json(
    isolated_store, emit_recorder, monkeypatch
):
    """First LLM response is broken; second is valid. Worker retries
    exactly once with the prior validation error appended."""
    calls: list[dict] = []

    async def fake_llm(step, rendered_user_text, previous_validation_error=None):
        calls.append({"prev": previous_validation_error})
        # Bad first, good second.
        if len(calls) == 1:
            return "not json at all"
        return '{"x": 1}'

    monkeypatch.setattr(worker_mod, "_invoke_llm_for_step", fake_llm)

    c = isolated_store.freeze(
        _make_contract(
            reasoning=[
                ReasoningStep(
                    id="s1",
                    entry_agent="CoordinatorAgent",
                    user_template="go",
                    output=OutputSpec(type="json", schema={"type": "object"}),
                    retry=Retry(on_validation_fail=1),
                )
            ]
        )
    )

    out = await worker_mod.execute_contract(c)
    assert out["__status__"] == "ok"
    assert len(calls) == 2
    assert calls[0]["prev"] is None
    assert "not valid JSON" in calls[1]["prev"]


@pytest.mark.asyncio
async def test_reasoning_fails_after_max_retries(
    isolated_store, emit_recorder, monkeypatch
):
    """LLM keeps returning garbage. After ``retry.on_validation_fail``
    extra attempts, worker invokes ``on_failure`` and does NOT emit."""
    async def fake_llm(step, rendered_user_text, previous_validation_error=None):
        return "still not json"

    monkeypatch.setattr(worker_mod, "_invoke_llm_for_step", fake_llm)

    c = isolated_store.freeze(
        _make_contract(
            reasoning=[
                ReasoningStep(
                    id="s1",
                    entry_agent="CoordinatorAgent",
                    user_template="go",
                    output=OutputSpec(type="json", schema={"type": "object"}),
                    retry=Retry(on_validation_fail=2),
                )
            ],
            on_failure=FailureAction(
                action=FailureActionType.ABORT_SILENT, abort=True
            ),
        )
    )

    out = await worker_mod.execute_contract(c)
    assert out["__status__"] == "error"
    assert "failed validation" in out["__error__"]

    # Emit must NOT have been called when on_failure aborted.
    assert all(call["args"].get("echo") != "hello" for call in emit_recorder)


# ---------------------------------------------------------------------------
# Gate behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_gate_skips_this_emit_by_default(
    isolated_store, emit_recorder
):
    """A gate that returns False skips just that emit; subsequent emits
    still run."""
    pre_len = len(emit_recorder)
    c = isolated_store.freeze(
        _make_contract(
            emit=[
                EmitStep(
                    adapter="record",
                    args={"label": "should_skip"},
                    gate=Gate(type="always_fail"),
                ),
                EmitStep(adapter="record", args={"label": "should_run"}),
            ]
        )
    )
    await worker_mod.execute_contract(c)

    new_calls = emit_recorder[pre_len:]
    labels = [c["args"]["label"] for c in new_calls]
    assert labels == ["should_run"]


@pytest.mark.asyncio
async def test_failing_gate_with_abort_aborts_the_contract(
    isolated_store, emit_recorder
):
    """If ``abort_on_gate_fail=True``, the fire aborts when the gate
    fails — subsequent emits do NOT run."""
    pre_len = len(emit_recorder)
    c = isolated_store.freeze(
        _make_contract(
            emit=[
                EmitStep(
                    adapter="record",
                    args={"label": "blocked"},
                    gate=Gate(type="always_fail"),
                    abort_on_gate_fail=True,
                ),
                EmitStep(adapter="record", args={"label": "should_not_run"}),
            ]
        )
    )
    out = await worker_mod.execute_contract(c)
    assert out["__status__"] == "error"
    new_calls = emit_recorder[pre_len:]
    assert all(c["args"]["label"] != "should_not_run" for c in new_calls)


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_skips_real_emit_but_renders_args(
    isolated_store, emit_recorder
):
    """Dry-runs are the AUTHOR-time preview path. Adapters are not
    called; the rendered args + would-have-been state come back in
    the result so the user can verify what would post."""
    pre_len = len(emit_recorder)
    c = isolated_store.freeze(_make_contract())
    out = await worker_mod.execute_contract(c, dry_run=True)

    assert out["__status__"] == "ok"
    assert len(emit_recorder) == pre_len, (
        "real adapters must not run during dry_run — got "
        f"{len(emit_recorder) - pre_len} extra calls"
    )
    emit_results = out["__emit_results__"]
    assert emit_results[0]["dry_run"] is True
    assert emit_results[0]["rendered_args"]["echo"] == "hello"


@pytest.mark.asyncio
async def test_dry_run_with_mock_inputs(isolated_store, emit_recorder):
    """Mock inputs let the AUTHOR-time preview short-circuit expensive
    loaders (web search, BigQuery) and still produce a meaningful
    rendered output."""
    c = isolated_store.freeze(
        _make_contract(
            inputs=[
                # Loader that would NotImplementedError if we let it run.
                InputSpec(id="news", loader="bigquery_query", args={"sql": "..."})
            ],
            emit=[EmitStep(adapter="record", args={"summary": "{news.summary}"})],
        )
    )
    out = await worker_mod.execute_contract(
        c, dry_run=True, mock_inputs={"news": {"summary": "mocked"}}
    )

    assert out["__status__"] == "ok"
    assert out["__emit_results__"][0]["rendered_args"]["summary"] == "mocked"
