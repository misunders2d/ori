"""Tests for ``app.v2.runtime._defaults``.

Pins per ``docs/PHASE_5_PLAN.md`` section 3.1 + 5.1.

Behavioural pins:
- ``prod_clock()`` returns a tz-aware datetime in UTC.
- ``prod_clock()`` returns a value close to wall-clock now
  (within a generous tolerance -- this is a smoke check, not
  a timing assertion).
- ``prod_run_id_factory()`` / ``prod_event_id_factory()``
  return 32-char lowercase hex strings (UUID4 hex shape).
- Two consecutive calls return distinct ids; pin the
  cross-call uniqueness behaviour.
- The two factories produce id streams that do not collide
  with each other (verified probabilistically -- 100 ids
  each, no overlap).

Inverse smoke pin:
- The module DOES import ``uuid`` and the module source DOES
  contain ``datetime.now``. This is the INVERSE of the smoke
  checks on every other phase-4/5 runtime module -- those
  modules forbid both. A regression that quietly removes
  the wall-clock / uuid wiring from this module would
  surface here, AND would also flip a phase-4 module's
  smoke check if the wiring moved there.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

from app.v2.runtime import _defaults as defaults_mod
from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)


_UUID4_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _function_body_calls(fn: Callable, dotted_name: str) -> bool:
    """Return True iff the function's BODY (not docstring) calls
    ``dotted_name`` (e.g. ``"datetime.now"`` or ``"uuid.uuid4"``).

    AST walk so the check is robust against docstring substrings.
    ``inspect.getsource(fn)`` returns ``def NAME(...): \n DOCSTRING
    \n BODY``; the AST parse turns the docstring into a string
    Expr at the top of ``func_def.body`` and turns real calls into
    ``ast.Call`` nodes. Walking only ``ast.Call`` nodes ignores
    string expressions entirely.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    # The first statement is the function we just inspected.
    func_def = tree.body[0]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))
    target = dotted_name.split(".")
    for node in ast.walk(func_def):
        if not isinstance(node, ast.Call):
            continue
        chain: list[str] = []
        f = node.func
        # Walk attribute chain: datetime.now -> Attribute(Name('datetime'), 'now')
        while isinstance(f, ast.Attribute):
            chain.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            chain.insert(0, f.id)
        if chain == target:
            return True
    return False


# ===========================================================================
# prod_clock
# ===========================================================================


def test_prod_clock_returns_tz_aware_utc():
    now = prod_clock()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    # ``tzinfo`` may be ``datetime.timezone.utc`` or a
    # tz-aware-equivalent -- pin the UTC offset rather than the
    # tzinfo identity to allow future swaps to e.g. ZoneInfo.
    assert now.utcoffset() == timedelta(0)


def test_prod_clock_is_close_to_wall_clock_now():
    """Sanity: the value the helper returns is within a few
    seconds of an independent ``datetime.now(timezone.utc)``
    call. Generous tolerance -- this is a smoke check, not a
    timing assertion."""
    before = datetime.now(timezone.utc)
    val = prod_clock()
    after = datetime.now(timezone.utc)
    assert before - timedelta(seconds=2) <= val <= after + timedelta(seconds=2)


def test_prod_clock_returns_distinct_instants_in_sequence():
    """Two near-instant calls should still yield monotonic-
    or-equal values. The Python clock has microsecond
    resolution so equality is allowed."""
    a = prod_clock()
    b = prod_clock()
    assert a <= b


# ===========================================================================
# prod_run_id_factory / prod_event_id_factory
# ===========================================================================


def test_prod_run_id_factory_returns_uuid4_hex_shape():
    rid = prod_run_id_factory()
    assert isinstance(rid, str)
    assert _UUID4_HEX_RE.match(rid), (
        f"prod_run_id_factory() must return 32 lowercase hex "
        f"chars (UUID4 hex shape); got {rid!r}"
    )


def test_prod_event_id_factory_returns_uuid4_hex_shape():
    eid = prod_event_id_factory()
    assert isinstance(eid, str)
    assert _UUID4_HEX_RE.match(eid), (
        f"prod_event_id_factory() must return 32 lowercase "
        f"hex chars (UUID4 hex shape); got {eid!r}"
    )


def test_prod_run_id_factory_returns_distinct_ids():
    ids = {prod_run_id_factory() for _ in range(100)}
    assert len(ids) == 100, "100 calls produced a collision"


def test_prod_event_id_factory_returns_distinct_ids():
    ids = {prod_event_id_factory() for _ in range(100)}
    assert len(ids) == 100, "100 calls produced a collision"


def test_run_and_event_factories_do_not_collide():
    """Both factories use UUID4 so cross-stream collisions
    are astronomically unlikely. Pin the property at 100
    samples each."""
    run_ids = {prod_run_id_factory() for _ in range(100)}
    evt_ids = {prod_event_id_factory() for _ in range(100)}
    overlap = run_ids & evt_ids
    assert overlap == set(), (
        f"run id stream collided with event id stream: "
        f"{sorted(overlap)[:5]}"
    )


# ===========================================================================
# Inverse smoke pin -- this module IS the wall-clock / uuid
# wiring; the regression risk is that the wiring quietly
# moves OUT into a runtime module that forbids it.
# ===========================================================================


def test_defaults_module_imports_uuid():
    """Inverse smoke: ``app.v2.runtime._defaults`` MUST
    import ``uuid`` (that's its job). Every OTHER phase-4/5
    runtime module's smoke check forbids ``uuid``; this one
    asserts the import is present so a regression that drops
    it surfaces here, AND any future module that quietly
    starts importing uuid flips its own forbid-uuid check."""
    seen = set()
    for _, member in vars(defaults_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" in seen, (
        "_defaults must import the stdlib ``uuid`` module -- "
        "it is the production wiring for the v2 runtime's "
        "id factories."
    )


def test_prod_clock_body_calls_datetime_now():
    """Inverse smoke: ``prod_clock``'s function BODY (not its
    docstring) contains a call to ``datetime.now``. AST-based
    check so a regression that gutted the body while leaving
    the docstring (which mentions ``datetime.now()`` for
    explanation) would actually flip the assertion.

    A naive substring check (``"datetime.now(" in
    inspect.getsource(prod_clock)``) is vacuously satisfied
    by the docstring text; AST walk over ``ast.Call`` nodes
    skips ``Expr(Constant(str))`` docstring nodes."""
    assert _function_body_calls(prod_clock, "datetime.now"), (
        "prod_clock must call ``datetime.now(...)`` in its body "
        "(verified via AST walk; not satisfied by docstring "
        "mentions). It is the production wiring for the v2 "
        "runtime's clock."
    )


def test_prod_factory_bodies_call_uuid4():
    """Mirror of the prod_clock AST check for the id factories.

    Both factory docstrings mention ``uuid.uuid4().hex`` for
    explanation; an ``inspect.getsource(...) "uuid.uuid4(" in
    source`` check would pass even if the bodies were stubbed
    to return a constant. AST walk pins the actual call."""
    for fn, name in (
        (prod_run_id_factory, "prod_run_id_factory"),
        (prod_event_id_factory, "prod_event_id_factory"),
    ):
        assert _function_body_calls(fn, "uuid.uuid4"), (
            f"{name} must call ``uuid.uuid4(...)`` in its "
            "body (verified via AST walk; not satisfied by "
            "docstring mentions). It is the production wiring "
            "for the v2 runtime's id stream."
        )


def test_function_body_calls_helper_ignores_docstring():
    """Self-test the AST helper: a function whose ONLY mention
    of the target call is in its docstring must return False.
    Pin so a future regression of the helper that falls back
    to substring search would flip here."""
    def docstring_only():
        """Mentions datetime.now( and uuid.uuid4( only in text.

        Body does nothing.
        """
        return None

    assert _function_body_calls(docstring_only, "datetime.now") is False
    assert _function_body_calls(docstring_only, "uuid.uuid4") is False


def test_function_body_calls_helper_finds_real_call():
    """Self-test the AST helper: a function that actually calls
    the target returns True. Belt-and-braces alongside the
    docstring-only false case above."""
    def calls_for_real():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)

    assert _function_body_calls(calls_for_real, "datetime.now") is True
    # Negative: helper does not false-match a sibling attribute.
    assert _function_body_calls(calls_for_real, "datetime.utcnow") is False


def test_defaults_module_exposes_three_callables():
    """The three documented production injectables are the
    public surface. Pin so a regression that quietly drops
    one of them flips here."""
    expected = {"prod_clock", "prod_run_id_factory", "prod_event_id_factory"}
    for name in expected:
        assert hasattr(defaults_mod, name), f"missing public callable {name!r}"
        assert callable(getattr(defaults_mod, name))
    assert set(defaults_mod.__all__) == expected, (
        f"__all__ drifted from {expected}: {defaults_mod.__all__}"
    )
