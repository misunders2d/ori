"""Pin the post-2026-05-14 split between ``origin_session_id`` and
``target_session_id`` on the notify dict.

Pre-split bug: ``_stamp_ownership`` did

    target_session = deliver_to or origin_session_id
    stamped["origin_session_id"] = target_session

which CLOBBERED ``origin_session_id`` with the delivery target
whenever ``deliver_to`` was set. ``_deliver_with_fallback`` then
read ``origin_session_id`` for the fallback channel and retried
delivery to the same broken target. Cross-channel scheduled tasks
silently dropped on transport failure.
"""

from __future__ import annotations


def test_stamp_no_deliver_to_keeps_origin():
    """Without ``deliver_to`` the origin IS the target. Both fields
    reflect the creator's session."""
    from app.tools.scheduling import _stamp_ownership

    out = _stamp_ownership({}, "tg_330959414", deliver_to="", origin_session_id="tg_330959414")
    assert out["origin_session_id"] == "tg_330959414"
    assert out["target_session_id"] == "tg_330959414"
    assert out["deliver_to_session"] == "tg_330959414"


def test_stamp_with_deliver_to_splits_origin_and_target():
    """User in ``tg_330959414`` schedules a task to deliver into a
    Slack channel ``sl_C0B2LJRS8D8``. ``origin_session_id`` MUST stay
    on the creator's session; ``target_session_id`` MUST be the
    Slack channel."""
    from app.tools.scheduling import _stamp_ownership

    out = _stamp_ownership(
        {},
        "tg_330959414",
        deliver_to="sl_C0B2LJRS8D8",
        origin_session_id="tg_330959414",
    )
    assert out["origin_session_id"] == "tg_330959414"
    assert out["target_session_id"] == "sl_C0B2LJRS8D8"
    assert out["deliver_to_session"] == "sl_C0B2LJRS8D8"


def test_stamp_empty_origin_only_sets_target():
    """Origin missing (legacy path) — don't fabricate one; just stamp
    the target."""
    from app.tools.scheduling import _stamp_ownership

    out = _stamp_ownership({}, "u", deliver_to="sl_X", origin_session_id="")
    assert "origin_session_id" not in out
    assert out["target_session_id"] == "sl_X"
