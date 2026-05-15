"""Tests for ``app.v2.runtime.cron_guard``.

Pins per ``docs/PHASE_5_PLAN.md`` §3.2.3 + §5.1.

Behavioural pins for ``reject_numeric_dow``:
- Pure ``*`` DOW → accepted (no raise).
- Named DOW (``MON``, ``FRI``) → accepted.
- Named range (``MON-FRI``) → accepted.
- Named list (``MON,WED,FRI``) → accepted.
- Numeric DOW in any form (range / list / single digit /
  step / named step with digit) → ``ValueError`` with a
  message that mentions the rejected field.
- Non-DOW fields can contain digits without rejection
  (minute / hour / day-of-month / month).
- Malformed cron (not 5 whitespace-separated fields) →
  ``ValueError`` with shape-error message.

Cross-module pin:
- ``app/v2/runtime/wakeup.py`` imports
  ``reject_numeric_dow`` from this module — the phase-5
  extraction did NOT leave a private copy in wakeup.
"""

from __future__ import annotations

import inspect

import pytest

from app.v2.runtime import cron_guard as cron_guard_mod
from app.v2.runtime.cron_guard import reject_numeric_dow


# ===========================================================================
# Accepted DOW forms
# ===========================================================================


@pytest.mark.parametrize(
    "cron",
    [
        "0 18 * * *",
        "0 18 * * MON",
        "0 18 * * FRI",
        "0 18 * * MON-FRI",
        "0 18 * * MON,WED,FRI",
        "0 18 * * SUN-SAT",
    ],
)
def test_named_or_wildcard_dow_accepted(cron):
    """Named days + wildcard pass the guard with no raise."""
    # No assertion needed — the helper returns None on
    # success. The point is that no exception is raised.
    assert reject_numeric_dow(cron) is None


@pytest.mark.parametrize(
    "cron",
    [
        # Numeric digits OUTSIDE the DOW field are fine — only
        # field 5 is restricted.
        "*/5 * * * *",
        "0 18 1-15 * MON-FRI",
        "0 0,12 * * *",
        "30 18 * 1,6 *",
    ],
)
def test_digits_outside_dow_field_accepted(cron):
    """The rule applies to DOW (field 5) only. Pin so a
    future refactor that over-broadens to all fields
    surfaces."""
    assert reject_numeric_dow(cron) is None


# ===========================================================================
# Rejected DOW forms — numeric anywhere in field 5
# ===========================================================================


@pytest.mark.parametrize(
    "cron, rejected_dow",
    [
        ("0 18 * * 1-5", "1-5"),       # numeric range
        ("0 18 * * 0,6", "0,6"),       # numeric list
        ("0 18 * * */2", "*/2"),       # numeric step (any anchor)
        ("0 18 * * MON-FRI/2", "MON-FRI/2"),  # named range + numeric step
        ("0 18 * * 1", "1"),           # single numeric day
        ("0 18 * * 6", "6"),           # another single numeric day
    ],
)
def test_numeric_dow_rejected(cron, rejected_dow):
    with pytest.raises(ValueError, match="numeric day-of-week") as exc_info:
        reject_numeric_dow(cron)
    msg = str(exc_info.value)
    assert rejected_dow in msg, (
        f"error message must name the rejected DOW field "
        f"({rejected_dow!r}); got: {msg!r}"
    )


def test_numeric_dow_message_explains_apscheduler_unix_divergence():
    """The error message must explain WHY (Monday=0 vs
    Sunday=0). Authors who hit this should not have to dig
    through docs to learn the rationale."""
    with pytest.raises(ValueError) as exc_info:
        reject_numeric_dow("0 18 * * 1-5")
    msg = str(exc_info.value)
    assert "Monday=0" in msg
    assert "Sunday=0" in msg


def test_numeric_dow_message_calls_out_step_rejection():
    """Step syntax with named anchor (``MON-FRI/2``) is
    rejected. The message must say so — otherwise authors
    will reach for it as a workaround."""
    with pytest.raises(ValueError) as exc_info:
        reject_numeric_dow("0 18 * * MON-FRI/2")
    msg = str(exc_info.value)
    assert "step syntax" in msg
    # The named example in the message must literally appear
    # so authors can grep / search for it.
    assert "MON-FRI/2" in msg


# ===========================================================================
# Shape errors
# ===========================================================================


@pytest.mark.parametrize(
    "cron, expected_fragment",
    [
        ("", "5"),
        ("0", "5"),
        ("0 18 * *", "5"),  # 4 fields
        ("0 18 * * * *", "5"),  # 6 fields
        ("asdf", "5"),  # 1 field
    ],
)
def test_wrong_field_count_rejected(cron, expected_fragment):
    """Defensive: the phase-1 ``CronTrigger`` model already
    validates 5 fields, but a hand-built trigger could
    bypass Pydantic. The helper rechecks cheaply."""
    with pytest.raises(ValueError, match=expected_fragment):
        reject_numeric_dow(cron)


def test_whitespace_normalisation():
    """``.strip().split()`` collapses leading / trailing /
    multiple internal whitespace — pin so a refactor that
    relies on exact-string parsing breaks here."""
    # Multiple internal spaces, leading + trailing whitespace.
    assert reject_numeric_dow("   0   18 *   *   MON-FRI  ") is None
    # Tabs.
    assert reject_numeric_dow("0\t18\t*\t*\t*") is None


# ===========================================================================
# Cross-module pin — wakeup uses cron_guard, no private copy
# ===========================================================================


def test_wakeup_module_imports_reject_numeric_dow_from_cron_guard():
    """Phase-5 extraction must NOT leave a private copy of
    the guard inside ``wakeup.py``. Test that the wakeup
    module's namespace contains the imported callable from
    cron_guard, and that no ``_reject_numeric_dow`` private
    symbol survives.

    Gotcha: ``app.v2.runtime.__init__`` does
    ``from app.v2.runtime.wakeup import wakeup``, which
    shadows the submodule attribute — ``app.v2.runtime
    .wakeup`` resolves to the function, not the module.
    Reach into ``sys.modules`` to get the actual module.
    """
    import sys

    import app.v2.runtime  # noqa: F401 — triggers package import

    wakeup_mod = sys.modules["app.v2.runtime.wakeup"]

    # The public helper is imported as ``reject_numeric_dow``.
    assert hasattr(wakeup_mod, "reject_numeric_dow"), (
        "wakeup must import reject_numeric_dow from cron_guard"
    )
    assert wakeup_mod.reject_numeric_dow is reject_numeric_dow

    # The old private name must NOT linger as a separate
    # callable. Phase-5 extraction is a rename + relocation,
    # not an alias kept for backward-compat.
    assert not hasattr(wakeup_mod, "_reject_numeric_dow"), (
        "wakeup.py still has a private _reject_numeric_dow — "
        "the extraction left a dead copy. Remove it."
    )


# ===========================================================================
# Hygiene smoke
# ===========================================================================


def test_cron_guard_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
        "sqlite3",  # cron_guard is pure — no DB access
    }
    seen = set()
    for _, member in vars(cron_guard_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"cron_guard module imports unexpected libs: "
        f"{sorted(leaked)}"
    )


def test_cron_guard_module_does_not_import_uuid():
    """Pure cron-string helper has no business with uuid."""
    seen = set()
    for _, member in vars(cron_guard_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_cron_guard_public_surface():
    """Only ``reject_numeric_dow`` is public. Pin so a
    future helper added to this module without an explicit
    review surfaces."""
    assert cron_guard_mod.__all__ == ["reject_numeric_dow"]
