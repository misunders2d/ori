"""Regression tests for the 2026-05-13 silent-fail in slack_post +
defense-in-depth worker-level status check.

Pre-fix sequence (production proof on ``linux_mastery_30_days_v2``):

  1. Contract author put ``args.channel = "sl_C079N5N7H08"`` (an ADK
     session id, not a Slack channel id).
  2. Adapter called ``slack_post_message(channel="sl_C079N5N7H08", ...)``
     WITHOUT awaiting. Returned the un-awaited coroutine.
  3. Worker awaited the outer adapter, got the inner coroutine back,
     never executed it, recorded ``{"phase": "emit", "ok": True}``.
  4. Slack received zero requests. Audit logged success.
  5. User asked "did it post?" — bot said yes. It didn't.

Two fixes pinned here:

  * ``slack_post`` (a) awaits the inner call, (b) raises on
    ``status != "success"``. Same hardening on ``telegram_dm``.
  * Worker rejects ``{"status": "error"|"failed"|"not_found"|"ambiguous"}``
    return dicts even when the adapter didn't raise — catches the
    same silent-fail pattern for any future adapter that forgets to
    raise.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_slack_post_awaits_inner_and_raises_on_slack_error(monkeypatch):
    """slack_post must AWAIT slack_post_message AND raise when the
    underlying tool returns ``status="error"`` (e.g. channel_not_found)."""
    from app.contracts.emit import slack_post

    call_log: list[dict] = []

    async def fake_slack_post_message(channel, text, thread_ts=None, tool_context=None):
        call_log.append({"channel": channel, "text": text})
        return {"status": "error", "message": "channel_not_found"}

    monkeypatch.setattr(
        "app.tools.slack.slack_post_message", fake_slack_post_message
    )

    with pytest.raises(RuntimeError, match="Slack rejected"):
        await slack_post({"channel": "sl_C079N5N7H08", "content": "hi"}, {})

    # The inner call ACTUALLY happened — proves the await is wired.
    assert call_log == [{"channel": "sl_C079N5N7H08", "text": "hi"}]


@pytest.mark.asyncio
async def test_slack_post_returns_dict_on_success(monkeypatch):
    """Happy path: Slack returns status=success → adapter returns the
    dict verbatim, doesn't raise."""
    from app.contracts.emit import slack_post

    async def fake_slack_post_message(channel, text, thread_ts=None, tool_context=None):
        return {"status": "success", "ts": "1700000000.000100", "channel": channel}

    monkeypatch.setattr(
        "app.tools.slack.slack_post_message", fake_slack_post_message
    )

    result = await slack_post(
        {"channel": "C079N5N7H08", "content": "hello"}, {}
    )

    assert result["status"] == "success"
    assert result["ts"] == "1700000000.000100"


@pytest.mark.asyncio
async def test_telegram_dm_raises_on_not_found(monkeypatch):
    """``telegram_send_dm`` returns status=not_found when the roster
    has no match. Adapter must raise so the worker hits its
    emit_failed branch instead of recording silent success."""
    from app.contracts.emit import telegram_dm

    async def fake_telegram_send_dm(user_id, text, tool_context=None):
        return {"status": "not_found", "message": "no telegram user matching ..."}

    monkeypatch.setattr(
        "app.tools.telegram.telegram_send_dm", fake_telegram_send_dm
    )

    with pytest.raises(RuntimeError, match="telegram_dm: send rejected"):
        await telegram_dm({"user_id": "999999", "text": "hi"}, {})


@pytest.mark.asyncio
async def test_telegram_dm_returns_dict_on_success(monkeypatch):
    from app.contracts.emit import telegram_dm

    async def fake_telegram_send_dm(user_id, text, tool_context=None):
        return {"status": "success", "user_id": user_id, "chat_id": 12345}

    monkeypatch.setattr(
        "app.tools.telegram.telegram_send_dm", fake_telegram_send_dm
    )

    result = await telegram_dm({"user_id": "12345", "text": "hi"}, {})

    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Worker-level defense-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_rejects_un_awaited_coroutine_return(
    monkeypatch, tmp_path
):
    """Direct repro of the 2026-05-13 ``linux_mastery_30_days_v2``
    silent no-op. The adapter ``return``s an inner async call WITHOUT
    awaiting it. Worker must detect the coroutine and raise — not
    record fake success."""
    from app.contracts import worker as worker_mod
    from app.contracts.emit import EMIT_ADAPTERS
    from app.contracts.schema import (
        Contract,
        EmitStep,
        EnforcementMode,
        FailureAction,
        FailureActionType,
        InputSpec,
        OnDemandTrigger,
    )
    from app.contracts.store import ContractStore

    store = ContractStore(root=str(tmp_path / "contracts"))
    monkeypatch.setattr("app.contracts.worker.contract_store", store)
    monkeypatch.setattr(
        "app.contracts.worker._AUDIT_DIR", str(tmp_path / "audit")
    )

    async def _inner():
        return {"status": "success"}

    async def adapter_that_forgot_await(args, state):
        # The bug: returns an UN-AWAITED coroutine. The Python runtime
        # would emit a RuntimeWarning at gc, but in production the
        # warning is filtered and the audit silently shows ok=true.
        return _inner()

    EMIT_ADAPTERS["unawaited_test_adapter"] = adapter_that_forgot_await
    try:
        c = Contract(
            id="unawaited_test",
            description="Pin worker coroutine guard.",
            author="t",
            trigger=OnDemandTrigger(),
            inputs=[InputSpec(id="g", loader="static_param", args={"value": "x"})],
            emit=[EmitStep(adapter="unawaited_test_adapter", args={"target": "x"})],
            on_failure=FailureAction(
                action=FailureActionType.ABORT_SILENT, abort=True
            ),
            enforcement=EnforcementMode.STRICT,
        ).with_fresh_hash()
        store.freeze(c)

        out = await worker_mod.execute_contract(c)

        assert out["__status__"] == "error", out
        assert "un-awaited coroutine" in out["__error__"], out
    finally:
        EMIT_ADAPTERS.pop("unawaited_test_adapter", None)


@pytest.mark.asyncio
async def test_worker_audit_logs_result_type_and_repr_on_success(
    monkeypatch, tmp_path
):
    """Audit must capture ``result_type`` + truncated ``result_repr``
    on every emit success. Lets us spot phantom successes (None,
    coroutine, str, ...) post-hoc without touching live state."""
    import json
    import pathlib

    from app.contracts import worker as worker_mod
    from app.contracts.emit import EMIT_ADAPTERS
    from app.contracts.schema import (
        Contract,
        EmitStep,
        EnforcementMode,
        InputSpec,
        OnDemandTrigger,
    )
    from app.contracts.store import ContractStore

    store = ContractStore(root=str(tmp_path / "contracts"))
    monkeypatch.setattr("app.contracts.worker.contract_store", store)
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr("app.contracts.worker._AUDIT_DIR", str(audit_dir))

    async def ok_adapter(args, state):
        return {"status": "success", "ts": "1700000000.0", "channel": "C012"}

    EMIT_ADAPTERS["repr_test_adapter"] = ok_adapter
    try:
        c = Contract(
            id="repr_test",
            description="Pin emit audit enrichment.",
            author="t",
            trigger=OnDemandTrigger(),
            inputs=[InputSpec(id="g", loader="static_param", args={"value": "x"})],
            emit=[EmitStep(adapter="repr_test_adapter", args={"target": "x"})],
            enforcement=EnforcementMode.STRICT,
        ).with_fresh_hash()
        store.freeze(c)

        await worker_mod.execute_contract(c)

        files = list(pathlib.Path(audit_dir / "repr_test").glob("*.jsonl"))
        assert files, "audit dir empty"
        events = [json.loads(line) for line in files[0].read_text().splitlines()]
        emit_event = next(e for e in events if e.get("phase") == "emit")
        assert emit_event["ok"] is True
        assert emit_event["result_type"] == "dict"
        assert "status" in emit_event["result_repr"]
        assert "success" in emit_event["result_repr"]
    finally:
        EMIT_ADAPTERS.pop("repr_test_adapter", None)


@pytest.mark.asyncio
async def test_worker_rejects_emit_with_error_status_even_when_adapter_didnt_raise(
    monkeypatch, tmp_path
):
    """Pre-2026-05-14 the worker recorded ``ok: True`` whenever the
    adapter coroutine returned a value, regardless of the returned
    status. That let any future adapter that silently returns a
    failure dict (instead of raising) record a fake success.

    The fix: worker inspects the returned dict's ``status`` key and
    treats ``error|failed|not_found|ambiguous`` as a raise. This test
    installs a misbehaving adapter that returns an error dict
    WITHOUT raising, and confirms the worker still routes the fire
    through ``on_failure``.
    """
    from app.contracts import worker as worker_mod
    from app.contracts.emit import EMIT_ADAPTERS
    from app.contracts.schema import (
        Contract,
        EmitStep,
        InputSpec,
        OnDemandTrigger,
        EnforcementMode,
        FailureAction,
        FailureActionType,
    )
    from app.contracts.store import ContractStore

    # Install per-test store.
    store = ContractStore(root=str(tmp_path / "contracts"))
    monkeypatch.setattr("app.contracts.worker.contract_store", store)
    monkeypatch.setattr("app.contracts.worker._AUDIT_DIR", str(tmp_path / "audit"))

    # Misbehaving adapter — returns error dict but doesn't raise.
    async def silent_failure_adapter(args, state):
        return {"status": "error", "message": "I forgot to raise"}

    prior = EMIT_ADAPTERS.get("silent_fail_test_adapter")
    EMIT_ADAPTERS["silent_fail_test_adapter"] = silent_failure_adapter
    try:
        c = Contract(
            id="silent_fail_test",
            description="Pin worker's status-check defense.",
            author="t",
            trigger=OnDemandTrigger(),
            inputs=[
                InputSpec(id="g", loader="static_param", args={"value": "x"})
            ],
            emit=[
                EmitStep(
                    adapter="silent_fail_test_adapter",
                    args={"target": "wherever"},
                )
            ],
            on_failure=FailureAction(
                action=FailureActionType.ABORT_SILENT, abort=True
            ),
            enforcement=EnforcementMode.STRICT,
        ).with_fresh_hash()
        store.freeze(c)

        out = await worker_mod.execute_contract(c)

        assert out["__status__"] == "error", out
        assert "non-success status" in out["__error__"], out
    finally:
        if prior is None:
            EMIT_ADAPTERS.pop("silent_fail_test_adapter", None)
        else:
            EMIT_ADAPTERS["silent_fail_test_adapter"] = prior
