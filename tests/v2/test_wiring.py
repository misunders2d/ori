"""Tests for ``app.v2.wiring`` — the CoordinatorAgent mount
seam.

Phase 9 slice 8 per ``docs/PHASE_9_PLAN.md`` §3.8.

Pins:

- ``prod_schedule_id_factory`` (now homed in
  ``app.v2.runtime._defaults`` per slice-8 reviewer 🔴 —
  ``_defaults`` is the SOLE uuid binding site) returns a
  slug matching the ``ScheduleSpec.id`` constraint
  ``^[a-z][a-z0-9_]*$`` (a bare uuid4 hex can start with a
  digit → would fail).
- ``build_authoring_toolset`` with explicit owner →
  AuthoringToolset with the production
  ``schedule_create_reminder`` closure bound (NOT the
  NotImplementedError stub).
- ``build_authoring_toolset`` with no owner + no env → the
  slice-6 constructor gate raises ``RuntimeError``.
- env fallback: owner from
  ``DEFAULT_AUTHORING_OWNER_ID`` when no kwarg.
- The closure's LLM-visible signature is exactly
  ``(at, recipient_channel, text)`` — DI names do not leak
  (round-2 reviewer L500 carry-forward).
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.v2.runtime._defaults import prod_schedule_id_factory
from app.v2.toolsets.authoring import AuthoringToolset
from app.v2.wiring import build_authoring_toolset


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# ===========================================================================
# prod_schedule_id_factory
# ===========================================================================


def test_schedule_id_factory_matches_slug_constraint():
    """100 generated ids must all satisfy the ScheduleSpec.id
    slug regex (leading lowercase letter; lowercase /
    digits / underscores only). A bare uuid4().hex can start
    with 0-9 and would fail — the ``s_`` prefix guards it."""
    for _ in range(100):
        sid = prod_schedule_id_factory()
        assert _SLUG_RE.match(sid), f"bad schedule id: {sid!r}"


def test_schedule_id_factory_unique():
    ids = {prod_schedule_id_factory() for _ in range(100)}
    assert len(ids) == 100


# ===========================================================================
# build_authoring_toolset
# ===========================================================================


def test_explicit_owner_binds_production_closure():
    toolset = build_authoring_toolset(expected_owner_id="T_X")
    assert isinstance(toolset, AuthoringToolset)
    # Production closure bound (its __name__ is the public
    # tool name); the stub would also carry that name, so
    # additionally assert it is NOT the stub object by
    # checking the closure has bound cells (the stub is a
    # plain module-level function).
    bound = toolset._schedule_create_reminder
    assert bound.__name__ == "schedule_create_reminder"
    assert bound.__closure__ is not None, (
        "expected the DI-bound production closure, got the "
        "unbound stub"
    )


def test_no_owner_and_no_env_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("V2_AUTHORING_OWNER_ID", raising=False)
    import app.v2.runtime._owner_default as od
    import app.v2.wiring as wiring_mod

    monkeypatch.setattr(od, "DEFAULT_AUTHORING_OWNER_ID", None)
    monkeypatch.setattr(wiring_mod, "DEFAULT_AUTHORING_OWNER_ID", None)

    with pytest.raises(RuntimeError):
        build_authoring_toolset()


def test_env_fallback_used_when_no_kwarg(monkeypatch):
    import app.v2.wiring as wiring_mod

    monkeypatch.setattr(
        wiring_mod, "DEFAULT_AUTHORING_OWNER_ID", "T_ENV_OWNER"
    )
    toolset = build_authoring_toolset()
    assert isinstance(toolset, AuthoringToolset)
    assert toolset._expected_owner_id == "T_ENV_OWNER"


def test_closure_signature_has_no_di_leak():
    """The LLM-visible signature must be exactly
    ``(at, recipient_channel, text)`` — DI parameter names
    (store / conn_factory / clock / …) must NOT appear
    (round-2 reviewer L500 carry-forward)."""
    toolset = build_authoring_toolset(expected_owner_id="T_X")
    sig = inspect.signature(toolset._schedule_create_reminder)
    assert list(sig.parameters) == ["at", "recipient_channel", "text"]


def test_explicit_owner_kwarg_wins_over_env(monkeypatch):
    import app.v2.wiring as wiring_mod

    monkeypatch.setattr(
        wiring_mod, "DEFAULT_AUTHORING_OWNER_ID", "T_ENV"
    )
    toolset = build_authoring_toolset(expected_owner_id="T_EXPLICIT")
    assert toolset._expected_owner_id == "T_EXPLICIT"
