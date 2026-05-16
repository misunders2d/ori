"""Phase 12 slice 1 — pure read-only-reasoning enforcement.

Per ``docs/PHASE_12_PLAN.md`` §1 / §3 / §9 (Q1–Q6) + the
claude-reviewer slice-1 hard-checks:

- Outcome shape ``{allowed, blocked_tools, unresolved_tools}``.
- ``tool_mode=write_allowed`` ⇒ ``allowed`` (friction is the
  authoring layer's, Q5) — and ``resolve_tags`` is NOT consulted.
- SECURITY fail-safe: ``resolve_tags`` → ``None`` (unregistered)
  OR an empty set (untagged) ⇒ that tool is treated
  write-capable ⇒ BLOCKED; NEVER silent-allow.
- ``read_only`` blocks EXACTLY the 5-tag set
  (``write_external`` / ``send_message`` / ``filesystem_write``
  / ``db_write`` / ``privileged``); ``filesystem_read`` +
  ``read_external`` are NOT blocking.

Real ``ReasoningStep`` instances are used (not a fake) so the
contract is pinned against the live model.
"""

from __future__ import annotations

import pytest

from app.v2.enums import ToolMode
from app.v2.models.execution_plan import ReasoningStep
from app.v2.reasoning_enforcement import (
    ReasoningEnforcementOutcome,
    ReasoningPlanGuardResult,
    evaluate_reasoning_plan,
    evaluate_reasoning_step,
)
from app.v2.tool_tags import ToolCapabilityTag

T = ToolCapabilityTag

_BLOCKING = [
    T.WRITE_EXTERNAL,
    T.SEND_MESSAGE,
    T.FILESYSTEM_WRITE,
    T.DB_WRITE,
    T.PRIVILEGED,
]
_NON_BLOCKING = [
    T.READ_EXTERNAL,
    T.FILESYSTEM_READ,
    T.COSTLY,
    T.USES_OAUTH,
]


def _step(
    tools: list[str],
    *,
    mode: ToolMode = ToolMode.READ_ONLY,
) -> ReasoningStep:
    return ReasoningStep(
        id="s1",
        entry_agent="CoordinatorAgent",
        tools=tools,
        tool_mode=mode,
        user_template="x",
    )


def _boom(_name: str):  # pragma: no cover - must never be called
    raise AssertionError(
        "resolve_tags must NOT be consulted for a "
        "write_allowed step (Q5 early return)"
    )


# ---------------------------------------------------------------------------
# Outcome shape
# ---------------------------------------------------------------------------


def test_outcome_shape_and_defaults():
    o = ReasoningEnforcementOutcome(allowed=True)
    assert o.allowed is True
    assert o.blocked_tools == ()
    assert o.unresolved_tools == ()
    # frozen dataclass
    with pytest.raises(Exception):
        o.allowed = False  # type: ignore[misc]


def test_read_only_no_tools_is_vacuously_allowed():
    o = evaluate_reasoning_step(
        _step([]), resolve_tags=lambda n: {T.READ_EXTERNAL}
    )
    assert o == ReasoningEnforcementOutcome(allowed=True)


# ---------------------------------------------------------------------------
# write_allowed ⇒ allowed, resolve_tags never consulted (Q5)
# ---------------------------------------------------------------------------


def test_write_allowed_is_allowed_without_consulting_resolver():
    o = evaluate_reasoning_step(
        _step(["sheets_append"], mode=ToolMode.WRITE_ALLOWED),
        resolve_tags=_boom,
    )
    assert o.allowed is True
    assert o.blocked_tools == ()
    assert o.unresolved_tools == ()


# ---------------------------------------------------------------------------
# read_only blocks EXACTLY the 5-tag set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", _BLOCKING)
def test_read_only_blocks_each_of_the_five_tags(tag):
    o = evaluate_reasoning_step(
        _step(["t"]), resolve_tags=lambda n: {tag}
    )
    assert o.allowed is False
    assert o.blocked_tools == (("t", frozenset({tag})),)
    assert o.unresolved_tools == ()


@pytest.mark.parametrize("tag", _NON_BLOCKING)
def test_read_only_does_not_block_read_or_observational_tags(tag):
    o = evaluate_reasoning_step(
        _step(["t"]), resolve_tags=lambda n: {tag}
    )
    assert o == ReasoningEnforcementOutcome(allowed=True)


def test_read_only_mixed_set_blocks_on_one_offending_tag():
    o = evaluate_reasoning_step(
        _step(["t"]),
        resolve_tags=lambda n: {T.READ_EXTERNAL, T.SEND_MESSAGE},
    )
    assert o.allowed is False
    assert o.blocked_tools == (
        ("t", frozenset({T.READ_EXTERNAL, T.SEND_MESSAGE})),
    )


# ---------------------------------------------------------------------------
# SECURITY fail-safe: None OR empty ⇒ unresolved ⇒ BLOCK
# ---------------------------------------------------------------------------


def test_unregistered_tool_none_is_failsafe_blocked():
    o = evaluate_reasoning_step(
        _step(["mystery"]), resolve_tags=lambda n: None
    )
    assert o.allowed is False
    assert o.unresolved_tools == ("mystery",)
    assert o.blocked_tools == ()


@pytest.mark.parametrize("empty", [set(), frozenset()])
def test_untagged_tool_empty_set_is_failsafe_blocked(empty):
    o = evaluate_reasoning_step(
        _step(["bare"]), resolve_tags=lambda n: empty
    )
    assert o.allowed is False
    assert o.unresolved_tools == ("bare",)
    assert o.blocked_tools == ()


def test_failsafe_never_silent_allows_unknown_tool():
    """A read_only step whose ONLY tool is unknown must be
    blocked — the security-critical case (silent-allow would
    defeat §5.4)."""
    o = evaluate_reasoning_step(
        _step(["definitely_not_registered"]),
        resolve_tags=lambda n: None,
    )
    assert o.allowed is False


# ---------------------------------------------------------------------------
# Multiple tools — blocked + unresolved both collected
# ---------------------------------------------------------------------------


def test_multiple_tools_collect_blocked_and_unresolved():
    def resolve(name: str):
        return {
            "writer": {T.WRITE_EXTERNAL},
            "reader": {T.READ_EXTERNAL},
        }.get(name)  # "ghost" → None

    o = evaluate_reasoning_step(
        _step(["writer", "reader", "ghost"]),
        resolve_tags=resolve,
    )
    assert o.allowed is False
    assert o.blocked_tools == (
        ("writer", frozenset({T.WRITE_EXTERNAL})),
    )
    assert o.unresolved_tools == ("ghost",)


def test_all_tools_clean_read_only_is_allowed():
    o = evaluate_reasoning_step(
        _step(["a", "b"]),
        resolve_tags=lambda n: {T.READ_EXTERNAL, T.FILESYSTEM_READ},
    )
    assert o == ReasoningEnforcementOutcome(allowed=True)


# ---------------------------------------------------------------------------
# Plan-level guard (slice 4) — strict delegation to slice-1
# ---------------------------------------------------------------------------


def test_plan_guard_empty_is_all_allowed():
    r = evaluate_reasoning_plan([], resolve_tags=lambda n: None)
    assert r == ReasoningPlanGuardResult()
    assert r.all_allowed is True


def test_plan_guard_delegates_per_step_in_order():
    s1 = _step(["w"])  # read_only + write tool ⇒ blocked
    s2 = ReasoningStep(
        id="s2",
        entry_agent="CoordinatorAgent",
        tools=["r"],
        tool_mode=ToolMode.READ_ONLY,
        user_template="x",
    )
    resolve = lambda n: {  # noqa: E731
        "w": {T.WRITE_EXTERNAL},
        "r": {T.READ_EXTERNAL},
    }.get(n)
    r = evaluate_reasoning_plan([s1, s2], resolve_tags=resolve)
    # Same shape as calling slice-1 per step, in order.
    assert [sid for sid, _o in r.outcomes] == ["s1", "s2"]
    assert r.outcomes[0][1] == evaluate_reasoning_step(
        s1, resolve_tags=resolve
    )
    assert r.outcomes[1][1] == evaluate_reasoning_step(
        s2, resolve_tags=resolve
    )
    assert r.all_allowed is False  # s1 blocked


def test_plan_guard_all_allowed_when_every_step_allowed():
    r = evaluate_reasoning_plan(
        [_step([], mode=ToolMode.WRITE_ALLOWED), _step([])],
        resolve_tags=lambda n: None,
    )
    assert r.all_allowed is True


def test_plan_guard_frozen():
    r = ReasoningPlanGuardResult()
    with pytest.raises(Exception):
        r.outcomes = ()  # type: ignore[misc]
