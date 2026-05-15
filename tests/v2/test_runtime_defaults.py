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

import inspect
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.runtime import _defaults as defaults_mod
from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)


_UUID4_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


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


def test_prod_clock_source_calls_datetime_now():
    """Inverse smoke: ``prod_clock``'s function body DOES
    contain ``datetime.now(``. Every other runtime module's
    forbid-side check rejects that substring at the module
    level; this one asserts it lives here at the function
    level so a regression that quietly stops actually
    calling ``datetime.now`` surfaces here.

    Scope the inspection to the function body (not the
    module) -- module-level docstrings contain the literal
    ``datetime.now(`` and would make a module-source check
    vacuous (the docstring would satisfy the assertion even
    if the function were gutted). ``inspect.getsource(
    prod_clock)`` returns only the function definition +
    body, so the check is load-bearing."""
    source = inspect.getsource(prod_clock)
    assert "datetime.now(" in source, (
        "prod_clock must call ``datetime.now(...)`` -- it is "
        "the production wiring for the v2 runtime's clock."
    )


def test_prod_factory_sources_call_uuid4():
    """Mirror of the prod_clock check for the id factories:
    each factory body must actually call ``uuid.uuid4`` so
    the regression-detection is at the function-source level,
    not the module-source level (which contains docstring
    substrings)."""
    for fn, name in (
        (prod_run_id_factory, "prod_run_id_factory"),
        (prod_event_id_factory, "prod_event_id_factory"),
    ):
        source = inspect.getsource(fn)
        assert "uuid.uuid4(" in source, (
            f"{name} must call ``uuid.uuid4(...)`` -- it is "
            "the production wiring for the v2 runtime's id "
            "stream."
        )


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
