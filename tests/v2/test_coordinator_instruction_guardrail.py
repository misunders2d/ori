"""Coordinator instruction guardrail — phase 9 slice 8.

Per ``docs/PHASE_9_PLAN.md`` §5.6 + ``docs/CONTRACTS_V2_DESIGN.md``
§11.4 (mandatory guardrail test).

Parses the resolved CoordinatorAgent instruction text and
asserts the bot's authoring instructions stay in sync with
the deployed v2 surface:

- Does NOT contain the deprecated freeform-path signature
  ``contract_freeze(spec:`` / ``contract_freeze(spec ``.
- Does NOT contain ``schedule_recurring_task(`` /
  ``schedule_one_off_task(`` (legacy tools v2 replaces).
- DOES reference the v2 surface tool names:
  ``OneOffReminder``, ``schedule_dry_run``,
  ``schedule_freeze``, ``schedule_draft_commit``,
  ``schedule_create_reminder``.
- DOES contain the scheduling-law clause (pinned fragment).
- The v2 AuthoringToolset is mounted ADDITIVELY alongside
  the v1 ContractToolset (round-2 reviewer Q4 / §11.1
  deprecation timeline).

A failure here is a HARD BLOCK on merging changes that
would leave the bot pointed at the old API while the new
tools are deployed.

NOTE: the CoordinatorAgent module CANNOT be ``importlib.
reload``-ed — ADK sub-agents (DeveloperAgent etc.) are
singletons whose ``parent`` is set on first construction;
re-running ``Agent(...)`` raises a pydantic parent-conflict.
The instruction text is a STATIC string independent of env,
so the guardrail asserts against the already-imported
module. The additive-mount + no-brick behaviour is pinned
against :func:`app.v2.wiring.build_authoring_toolset`
directly (the seam the coordinator's try/except wraps).
"""

from __future__ import annotations

import pytest

from app.sub_agents.coordinator_agent import root_agent


@pytest.fixture(scope="module")
def coordinator_instruction() -> str:
    instr = root_agent.instruction
    assert isinstance(instr, str)
    return instr


# ===========================================================================
# Deprecated-surface absence
# ===========================================================================


def test_no_contract_freeze_spec_signature(coordinator_instruction):
    """The deprecated freeform-path signature must be absent
    (design §11.4 / plan §5.6)."""
    assert "contract_freeze(spec:" not in coordinator_instruction
    assert "contract_freeze(spec " not in coordinator_instruction


def test_no_legacy_schedule_task_tools(coordinator_instruction):
    """Legacy ``schedule_recurring_task`` /
    ``schedule_one_off_task`` invocations must be absent —
    v2 template tools replace them."""
    assert "schedule_recurring_task(" not in coordinator_instruction
    assert "schedule_one_off_task(" not in coordinator_instruction


# ===========================================================================
# v2 surface presence
# ===========================================================================


@pytest.mark.parametrize(
    "tool_name",
    [
        "OneOffReminder",
        "schedule_dry_run",
        "schedule_freeze",
        "schedule_draft_commit",
        "schedule_create_reminder",
    ],
)
def test_v2_tool_referenced(coordinator_instruction, tool_name):
    assert tool_name in coordinator_instruction, (
        f"Coordinator instruction must reference the v2 "
        f"tool {tool_name!r} (plan §5.6)"
    )


def test_scheduling_law_clause_present(coordinator_instruction):
    """The scheduling-law clause must be present. Pin a
    specific fragment so a reword that drops the law's
    intent flips here."""
    assert (
        "SCHEDULING LAW: every scheduled work item is "
        "created via a v2 typed tool"
    ) in coordinator_instruction


# ===========================================================================
# v1 stays mounted (additive cutover)
# ===========================================================================


def test_v1_contract_toolset_still_mounted():
    """The v1 ``ContractToolset`` must STAY mounted on the
    CoordinatorAgent — phase 9 is an ADDITIVE cutover per
    round-2 reviewer Q4 / §11.1 deprecation timeline. It is
    not gated on env, so it's always present regardless of
    whether the v2 wiring succeeded."""
    from app.toolsets.contracts import ContractToolset

    assert any(
        isinstance(t, ContractToolset) for t in root_agent.tools
    ), "v1 ContractToolset must remain mounted (additive cutover)"


# ===========================================================================
# v2 wiring seam — additive mount + no-brick behaviour
# (pinned at the build_authoring_toolset seam since the
#  coordinator module can't be reloaded)
# ===========================================================================


def test_build_authoring_toolset_returns_toolset_when_owner_set():
    """With an explicit owner id, the wiring builds a real
    :class:`AuthoringToolset`. This is what the coordinator
    mounts ADDITIVELY into its tools list."""
    from app.v2.toolsets.authoring import AuthoringToolset
    from app.v2.wiring import build_authoring_toolset

    toolset = build_authoring_toolset(expected_owner_id="T_GUARDRAIL")
    assert isinstance(toolset, AuthoringToolset)
    # The production schedule_create_reminder closure is
    # bound (not the NotImplementedError stub) — its
    # __name__ is the public tool name.
    assert (
        toolset._schedule_create_reminder.__name__
        == "schedule_create_reminder"
    )


def test_build_authoring_toolset_raises_when_owner_unresolvable(
    monkeypatch,
):
    """No explicit owner kwarg AND no env → the slice-6
    constructor gate raises ``RuntimeError``. The
    coordinator's slice-8 mount wraps THIS call in
    try/except so a fresh deploy without
    ``V2_AUTHORING_OWNER_ID`` boots v1-only instead of
    bricking. Pin the raise so the safety-net contract
    holds."""
    monkeypatch.delenv("V2_AUTHORING_OWNER_ID", raising=False)
    # Force the slice-6 module constant to None so the
    # env-fallback also yields None (the constant is
    # captured at import time; monkeypatch the live attr).
    import app.v2.runtime._owner_default as owner_default_mod
    import app.v2.wiring as wiring_mod

    monkeypatch.setattr(
        owner_default_mod, "DEFAULT_AUTHORING_OWNER_ID", None
    )
    monkeypatch.setattr(
        wiring_mod, "DEFAULT_AUTHORING_OWNER_ID", None
    )

    from app.v2.wiring import build_authoring_toolset

    with pytest.raises(RuntimeError):
        build_authoring_toolset()


def test_coordinator_mount_is_resilient_to_wiring_failure():
    """The coordinator module-level mount must NOT propagate
    a wiring exception. Pin the structural contract: the
    module exposes ``_v2_authoring_tools`` as a list (empty
    when wiring failed, one-element when it succeeded) and
    splats it into ``tools`` — never raising at import."""
    import app.sub_agents.coordinator_agent as mod

    assert isinstance(mod._v2_authoring_tools, list)
    # Length is 0 or 1 depending on ambient env; either way
    # the coordinator constructed successfully (import of
    # root_agent at top of this file would have raised
    # otherwise).
    assert len(mod._v2_authoring_tools) in (0, 1)
    assert mod.root_agent.name == "CoordinatorAgent"
