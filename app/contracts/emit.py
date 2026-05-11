"""Emit adapters — deterministic side-effect runners that touch the
outside world (Slack, Telegram, Drive, Sheets, email, memory).

Each adapter takes the rendered ``args`` dict + the full ``state`` dict
and applies one effect. Adapters are async, idempotent where possible,
and never call back into an LLM. They are the ONLY thing in the
contract pipeline that touches the user channel — the worker session's
intermediate text never auto-posts.

Gates: pre-emit checks (``sheet_dedup`` is the canonical example).
A failing gate either skips this emit (default) or aborts the whole
contract (``emit.abort_on_gate_fail`` true).

Registry pattern matches loaders. New adapters register with the
``@register`` decorator; the AUTHOR pipeline validates adapter names
at draft time so unknowns fail loudly during dry-run, not at fire time.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.contracts.templating import render

logger = logging.getLogger(__name__)


# Adapter = (rendered_args, state) -> result dict
Adapter = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]
GateFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[bool]]

EMIT_ADAPTERS: dict[str, Adapter] = {}
GATES: dict[str, GateFn] = {}


def register_adapter(name: str):
    def _wrap(fn: Adapter) -> Adapter:
        if name in EMIT_ADAPTERS:
            raise ValueError(f"emit adapter {name!r} already registered")
        EMIT_ADAPTERS[name] = fn
        return fn

    return _wrap


def register_gate(name: str):
    def _wrap(fn: GateFn) -> GateFn:
        if name in GATES:
            raise ValueError(f"gate {name!r} already registered")
        GATES[name] = fn
        return fn

    return _wrap


# ---------------------------------------------------------------------------
# slack_post — post a single message to a Slack channel
# ---------------------------------------------------------------------------


@register_adapter("slack_post")
async def slack_post(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Post ``args.content`` to ``args.channel`` via the Slack tool path.

    ``args.content`` must already be Slack mrkdwn — the EMIT layer
    doesn't translate. (Author drafts the template that way; the
    ``state_setter`` callback used to translate per-turn, which was
    fragile.) ``args.channel`` is either a channel id (``C012ABCDE``)
    or a name with leading ``#``.
    """
    from app.tools.slack import slack_post_message
    from app.contracts._loader_context import LoaderContext

    if "channel" not in args or "content" not in args:
        raise ValueError("slack_post requires args.channel and args.content")

    return slack_post_message(
        channel=args["channel"],
        text=args["content"],
        thread_ts=args.get("thread_ts"),
        tool_context=LoaderContext(),
    )


# ---------------------------------------------------------------------------
# telegram_dm — direct-message a Telegram user
# ---------------------------------------------------------------------------


@register_adapter("telegram_dm")
async def telegram_dm(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Send ``args.text`` to ``args.user_id`` via Telegram. Used for
    admin alerts (``on_failure`` notifications) and personal reminders.
    """
    from app.tools.telegram import telegram_send_dm
    from app.contracts._loader_context import LoaderContext

    if "user_id" not in args or "text" not in args:
        raise ValueError("telegram_dm requires args.user_id and args.text")

    return await telegram_send_dm(
        user_id=str(args["user_id"]),
        text=args["text"],
        tool_context=LoaderContext(),
    )


# ---------------------------------------------------------------------------
# sheet_append — append a row to a Google Sheet (dedup log, audit log)
# ---------------------------------------------------------------------------


@register_adapter("sheet_append")
async def sheet_append(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Append ``args.row`` (a list of cell values) to
    ``args.spreadsheet_id``. ``args.range`` (e.g. ``"Sheet1"``) targets
    the worksheet — defaults to the first sheet.

    Wired in P7 (the AI Pilot migration is the first real consumer).
    """
    raise NotImplementedError(
        "sheet_append emit adapter is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# drive_doc_fill — write content into a Google Doc template
# ---------------------------------------------------------------------------


@register_adapter("drive_doc_fill")
async def drive_doc_fill(
    args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Replace ``{{field_name}}`` placeholders in a Google Doc template
    with the values in ``args.fields``. Used by the 30-step ASIN audit
    contract to fill out the final report doc.

    Wired in P7 alongside the ASIN audit migration.
    """
    raise NotImplementedError(
        "drive_doc_fill emit adapter is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# email — send an email via the bot's SMTP path
# ---------------------------------------------------------------------------


@register_adapter("email")
async def email(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Send an email. ``args.to``, ``args.subject``, ``args.body``."""
    raise NotImplementedError(
        "email emit adapter is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# memory_update — write a Memory node + relations to the graph
# ---------------------------------------------------------------------------


@register_adapter("memory_update")
async def memory_update(
    args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Persist a Memory node (and optional relations) produced by the
    reasoning chain. Useful for contracts whose primary side effect is
    "remember this audit's findings" rather than post anywhere.
    """
    raise NotImplementedError(
        "memory_update emit adapter is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@register_gate("sheet_dedup")
async def sheet_dedup(args: dict[str, Any], state: dict[str, Any]) -> bool:
    """Return True if the emit is allowed to proceed (i.e. no
    duplicate row exists for the current key).

    ``args.source`` is the spreadsheet id; ``args.key`` is the value to
    look for in the first column. Common pattern: ``key`` is ``{today}``
    so re-fires on the same day are no-ops.

    Wired in P7 alongside ``sheet_read``.
    """
    raise NotImplementedError(
        "sheet_dedup gate is wired in P7. Registry slot reserved."
    )


@register_gate("always_pass")
async def always_pass(args: dict[str, Any], state: dict[str, Any]) -> bool:
    """Trivial gate that always returns True. Useful for testing the
    gate machinery and as a no-op explicit-gate marker (authors can use
    it to indicate "we explicitly chose no gating here", separately from
    "we forgot to set a gate")."""
    return True


@register_gate("always_fail")
async def always_fail(args: dict[str, Any], state: dict[str, Any]) -> bool:
    """Trivial gate that always returns False. Useful for testing the
    abort-on-gate-fail path without setting up real conditions."""
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_emit(
    adapter_name: str, args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Resolve adapter, render args against state, invoke."""
    if adapter_name not in EMIT_ADAPTERS:
        raise KeyError(
            f"unknown emit adapter {adapter_name!r}. "
            f"Known: {sorted(EMIT_ADAPTERS.keys())}"
        )
    rendered = render(args, state)
    return await EMIT_ADAPTERS[adapter_name](rendered, state)


async def run_gate(
    gate_name: str, args: dict[str, Any], state: dict[str, Any]
) -> bool:
    if gate_name not in GATES:
        raise KeyError(
            f"unknown gate {gate_name!r}. Known: {sorted(GATES.keys())}"
        )
    rendered = render(args, state)
    return await GATES[gate_name](rendered, state)


def known_adapters() -> list[str]:
    return sorted(EMIT_ADAPTERS.keys())


def known_gates() -> list[str]:
    return sorted(GATES.keys())
