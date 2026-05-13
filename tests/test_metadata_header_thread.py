"""Tests for the ``thread_id`` field on the metadata header.

Slack threads were invisible to the agent pre-2026-05-13. The
inbound message header only carried timestamp + platform — no
thread identifier — so the LLM had no way to tell which thread it
was reading or replying to. ``_inject_metadata_header`` now accepts
an optional ``thread_id`` arg that surfaces ``Thread: <ts>`` to the
agent.
"""

from __future__ import annotations

from datetime import datetime, timezone


def test_inject_metadata_header_includes_thread_id():
    from app.core.agent_executor import _inject_metadata_header

    header = _inject_metadata_header(
        text="hello",
        timestamp=datetime(2026, 5, 13, 17, 10, tzinfo=timezone.utc),
        platform="slack",
        thread_id="1778690855.536349",
    )
    assert "Thread: 1778690855.536349" in header
    assert "Platform: slack" in header


def test_inject_metadata_header_omits_thread_when_none():
    from app.core.agent_executor import _inject_metadata_header

    header = _inject_metadata_header(
        text="hello",
        timestamp=datetime(2026, 5, 13, 17, 10, tzinfo=timezone.utc),
        platform="slack",
    )
    assert "Thread:" not in header
    assert "Platform: slack" in header


def test_inject_metadata_header_empty_thread_id_omits():
    """Empty-string thread_id is treated as ``None`` — Slack's
    ``event.thread_ts`` is ``""`` for top-level messages, and we
    don't want ``Thread: `` (no value) appearing in the header."""
    from app.core.agent_executor import _inject_metadata_header

    header = _inject_metadata_header(
        text="hello",
        timestamp=datetime(2026, 5, 13, 17, 10, tzinfo=timezone.utc),
        platform="slack",
        thread_id="",
    )
    assert "Thread:" not in header
