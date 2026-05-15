"""Tests for ``app.v2.emit.slack_reminder``.

Phase 9 slice 4 per ``docs/PHASE_9_PLAN.md`` §5.3.

Pins:
- Happy path: stub client receives ONE chat_postMessage
  call with the (channel, text) lifted off the spec;
  result is SlackPostResult(ok=True, channel=..., ts=...).
- Stub raises → SlackPostResult(ok=False, error=...).
- Slack response carries ok=False → adapter surfaces
  the embedded error.
- Slack response missing 'ok' key → ok=False with the
  documented fallback error.
- Missing template → ok=False with the documented
  error.
- Missing args (None / empty dict) → ok=False with
  ``template.args missing 'text'``.
- Args lacking 'text' key → ok=False with the same
  message.
- text not a string → ok=False naming the type
  mismatch.
- AST pin: no ``slack_sdk`` import at module load.
- AST pin: no ``slack_sdk`` substring anywhere in the
  source.
- ``ts`` non-string from Slack → coerced to None.
- Channel always carries the spec's delivery target id
  even when the upstream call short-circuits.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.emit import slack_reminder as slack_mod
from app.v2.emit.slack_reminder import (
    SlackPostResult,
    SlackProtocol,
    emit_reminder_to_slack,
)
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_FIRE_AT = _UTC_NOW + timedelta(hours=1)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _spec(
    *,
    text: str = "ping the channel",
    template: TemplateRef | None = None,
    channel_id: str = "C012ABCDE",
) -> ScheduleSpec:
    if template is None:
        template = TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"text": text},
        )
    spec = ScheduleSpec(
        id="sched_alpha",
        description=f"OneOffReminder: {text}",
        owner=UserRef(
            platform="slack",
            user_id="U_OWNER",
            display_name="Sergey",
        ),
        trigger=OneOffTrigger(
            at_iso_datetime=_FIRE_AT,
            timezone="UTC",
        ),
        delivery=Delivery(
            target_session_id=channel_id,
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
        execution_plan_hash=None,
        template=template,
        authored_at=_UTC_NOW.isoformat(),
    )
    return spec.with_fresh_hash()


class _StubSlackClient:
    """Records every chat_postMessage call + returns a
    canned response."""

    def __init__(
        self,
        *,
        response: dict | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.response = response if response is not None else {
            "ok": True,
            "ts": "1700000000.000100",
            "channel": "C012ABCDE",
        }
        self.raise_exc = raise_exc

    async def chat_postMessage(
        self,
        *,
        channel: str,
        text: str,
    ) -> dict:
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append({"channel": channel, "text": text})
        return self.response


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_calls_chat_post_message_once():
    client = _StubSlackClient()
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is True
    assert result.channel == "C012ABCDE"
    assert result.ts == "1700000000.000100"
    assert result.error is None
    # Exactly one chat_postMessage call with the right
    # arguments.
    assert client.calls == [
        {"channel": "C012ABCDE", "text": "ping the channel"}
    ]


@pytest.mark.asyncio
async def test_happy_path_carries_through_text_and_channel():
    client = _StubSlackClient()
    result = await emit_reminder_to_slack(
        spec=_spec(text="weekly digest", channel_id="C111"),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is True
    assert result.channel == "C111"
    assert client.calls[0]["channel"] == "C111"
    assert client.calls[0]["text"] == "weekly digest"


# ===========================================================================
# Slack API failure modes
# ===========================================================================


@pytest.mark.asyncio
async def test_stub_raises_returns_wrapped_failure():
    client = _StubSlackClient(
        raise_exc=RuntimeError("simulated slack outage")
    )
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert result.channel == "C012ABCDE"
    assert result.ts is None
    assert "simulated slack outage" in result.error


@pytest.mark.asyncio
async def test_response_ok_false_surfaces_embedded_error():
    client = _StubSlackClient(
        response={
            "ok": False,
            "error": "channel_not_found",
        }
    )
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert result.error == "channel_not_found"
    assert result.ts is None


@pytest.mark.asyncio
async def test_response_missing_ok_key_returns_fallback_error():
    client = _StubSlackClient(response={"ts": "..."})  # no ok
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert result.error is not None
    assert "ok=False" in result.error or "no error" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_ok",
    [
        "true",      # truthy str — Slack never sends this
        "True",      # capitalised str
        "false",     # truthy str (non-empty) but semantically false
        "False",     # capitalised str
        "yes",
        "no",
        "0",         # non-empty string
        1,           # truthy int — Slack never sends this
        2,
        -1,
        0,           # falsy but not the True bool
        1.0,         # truthy float
        [],          # falsy non-bool
        ["ok"],      # truthy non-bool
        {},          # falsy non-bool
        {"a": 1},    # truthy non-bool
        None,        # missing
    ],
)
async def test_response_non_bool_ok_treated_as_failure(bad_ok):
    """Round-1 reviewer slice-4 fix: Slack API contract
    treats success ONLY when ``ok`` is the literal ``True``
    bool. Truthy non-bool values (``"true"`` / ``1`` /
    ``"yes"``) MUST NOT map to success. Pin parametrically
    so a future refactor that re-introduces
    ``bool(response.get("ok"))`` semantics surfaces."""
    client = _StubSlackClient(
        response={"ok": bad_ok, "ts": "1700000000.000100"}
    )
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is False, (
        f"non-bool ok={bad_ok!r} of type {type(bad_ok).__name__} "
        "must not map to SlackPostResult.ok=True"
    )


@pytest.mark.asyncio
async def test_response_literal_true_only_is_success():
    """Sanity: the ONLY value of ``ok`` that maps to
    success is the literal ``True`` bool."""
    client = _StubSlackClient(
        response={"ok": True, "ts": "1700000000.000100"}
    )
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_response_ts_non_string_coerced_to_none():
    client = _StubSlackClient(
        response={"ok": True, "ts": 12345}  # int, not str
    )
    result = await emit_reminder_to_slack(
        spec=_spec(),
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is True
    assert result.ts is None


# ===========================================================================
# Template / args failures
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_template_returns_failure():
    # Build a spec with template=None directly (CustomFlow).
    spec_no_template = _spec().model_copy(update={"template": None})

    result = await emit_reminder_to_slack(
        spec=spec_no_template,
        slack_client=_StubSlackClient(),
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert result.channel == "C012ABCDE"  # still carries the target
    assert "spec.template is None" in result.error


@pytest.mark.asyncio
async def test_args_none_returns_failure():
    template_no_args = TemplateRef(
        name="OneOffReminder",
        version="1",
        args=None,
    )
    spec = _spec(template=template_no_args)

    result = await emit_reminder_to_slack(
        spec=spec,
        slack_client=_StubSlackClient(),
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert "template.args missing 'text'" in result.error


@pytest.mark.asyncio
async def test_args_empty_dict_returns_failure():
    template_empty = TemplateRef(
        name="OneOffReminder",
        version="1",
        args={},
    )
    spec = _spec(template=template_empty)

    result = await emit_reminder_to_slack(
        spec=spec,
        slack_client=_StubSlackClient(),
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert "template.args missing 'text'" in result.error


@pytest.mark.asyncio
async def test_args_missing_text_key_returns_failure():
    template_other = TemplateRef(
        name="OneOffReminder",
        version="1",
        args={"snoozable": False},  # no 'text'
    )
    spec = _spec(template=template_other)

    result = await emit_reminder_to_slack(
        spec=spec,
        slack_client=_StubSlackClient(),
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert "template.args missing 'text'" in result.error


@pytest.mark.asyncio
async def test_args_text_non_string_returns_failure():
    template_int = TemplateRef(
        name="OneOffReminder",
        version="1",
        args={"text": 12345},
    )
    spec = _spec(template=template_int)

    result = await emit_reminder_to_slack(
        spec=spec,
        slack_client=_StubSlackClient(),
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert "must be str" in result.error
    assert "int" in result.error


@pytest.mark.asyncio
async def test_short_circuit_does_not_call_slack(monkeypatch):
    """When the template / args check fires, the stub
    client must NOT receive a chat_postMessage call."""
    template_no_args = TemplateRef(
        name="OneOffReminder",
        version="1",
        args=None,
    )
    spec = _spec(template=template_no_args)
    client = _StubSlackClient()

    result = await emit_reminder_to_slack(
        spec=spec,
        slack_client=client,
        clock=_fixed_clock,
    )

    assert result.ok is False
    assert client.calls == []


# ===========================================================================
# Module hygiene
# ===========================================================================


def test_module_does_not_import_slack_sdk():
    src = inspect.getsource(slack_mod)
    assert "slack_sdk" not in src, (
        "slack_sdk substring leaked into module source"
    )


def test_module_does_not_import_googleapiclient():
    src = inspect.getsource(slack_mod)
    assert "googleapiclient" not in src


def test_module_no_module_level_io_imports():
    """AST pin: no module-level import of httpx /
    requests / urllib3 / aiohttp."""
    src = inspect.getsource(slack_mod)
    tree = ast.parse(src)
    forbidden = {
        "httpx",
        "requests",
        "urllib3",
        "aiohttp",
        "smtplib",
        "slack_sdk",
        "googleapiclient",
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    leaked = names & forbidden
    assert not leaked, f"forbidden imports: {leaked!r}"


def test_emit_body_calls_no_datetime_now():
    source = textwrap.dedent(inspect.getsource(emit_reminder_to_slack))
    tree = ast.parse(source)
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                calls.add(".".join(reversed(parts)))

    forbidden = {"datetime.now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, f"forbidden calls: {leaked!r}"
