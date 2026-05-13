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

# Per-adapter / per-gate argument contracts. Populated by the
# ``register_adapter`` / ``register_gate`` decorators when they're
# given schema kwargs. Consumed by
# ``app.contracts.validation.validate_adapter_arg_shapes`` at author
# time. Required-arg check accepts either the canonical key or any of
# its aliases; optional keys are allowed but never required; unknown
# keys are rejected so typos like ``text`` vs ``content`` (2026-05-13
# AI Pilot fire) get caught at freeze, not in production at fire time.
EMIT_ADAPTER_SCHEMAS: dict[str, dict[str, Any]] = {}
GATE_ARG_SCHEMAS: dict[str, dict[str, Any]] = {}


def register_adapter(
    name: str,
    *,
    required: list[str] | None = None,
    optional: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
):
    """Register a contract emit adapter.

    Schema kwargs (all optional):
      - ``required``: arg keys that MUST appear in ``emit.args`` (or one
        of their aliases). Author-time validator rejects freeze
        otherwise.
      - ``optional``: arg keys that MAY appear. Listed so the validator
        can reject *unknown* keys (typos) without false positives.
      - ``aliases``: ``{canonical_key: [alias_a, alias_b]}``. An alias
        satisfies the required-key check AND counts as a known key.

    Adapters without any of these kwargs declare no contract — the
    validator skips them. New adapters should always declare.
    """

    def _wrap(fn: Adapter) -> Adapter:
        if name in EMIT_ADAPTERS:
            raise ValueError(f"emit adapter {name!r} already registered")
        EMIT_ADAPTERS[name] = fn
        if required or optional or aliases:
            EMIT_ADAPTER_SCHEMAS[name] = {
                "required": list(required or []),
                "optional": list(optional or []),
                "aliases": {k: list(v) for k, v in (aliases or {}).items()},
            }
        return fn

    return _wrap


def register_gate(
    name: str,
    *,
    required: list[str] | None = None,
    optional: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
):
    """Register a contract pre-emit gate. Schema kwargs match
    ``register_adapter``."""

    def _wrap(fn: GateFn) -> GateFn:
        if name in GATES:
            raise ValueError(f"gate {name!r} already registered")
        GATES[name] = fn
        if required or optional or aliases:
            GATE_ARG_SCHEMAS[name] = {
                "required": list(required or []),
                "optional": list(optional or []),
                "aliases": {k: list(v) for k, v in (aliases or {}).items()},
            }
        return fn

    return _wrap


# ---------------------------------------------------------------------------
# slack_post — post a single message to a Slack channel
# ---------------------------------------------------------------------------


@register_adapter(
    "slack_post",
    required=["channel", "content"],
    optional=["thread_ts"],
)
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

    # ``slack_post_message`` is async. The pre-2026-05-14 adapter did
    # ``return slack_post_message(...)`` WITHOUT awaiting — so the
    # worker awaited the outer adapter coroutine, got an un-awaited
    # inner coroutine back as ``result``, and recorded
    # ``{"phase": "emit", "ok": True}`` while the HTTP request to Slack
    # NEVER FIRED. Every slack_post emit since the adapter was written
    # was a silent no-op. Sergey caught it on 2026-05-13 when
    # ``linux_mastery_30_days_v2`` "succeeded" with emit_count=1 but
    # nothing landed in Slack.
    result = await slack_post_message(
        channel=args["channel"],
        text=args["content"],
        thread_ts=args.get("thread_ts"),
        tool_context=LoaderContext(),
    )

    # Status check: the underlying tool returns
    # ``{"status": "error", "message": "<slack-api-error>"}`` for any
    # Slack-side failure (channel_not_found, not_in_channel, rate
    # limited, ...). Worker decides ``ok`` from whether the adapter
    # RAISED, not from the return dict, so we must raise here to
    # surface the failure. Otherwise the same silent ``ok: True``
    # bug recurs at a layer above this one.
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RuntimeError(
            f"slack_post: Slack rejected the message "
            f"(channel={args['channel']!r}, result={result!r})"
        )
    return result


# ---------------------------------------------------------------------------
# telegram_dm — direct-message a Telegram user
# ---------------------------------------------------------------------------


@register_adapter(
    "telegram_dm",
    required=["user_id", "text"],
)
async def telegram_dm(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Send ``args.text`` to the Telegram chat identified by
    ``args.user_id`` (the bot's canonical platform id, e.g.
    ``"330959414"`` or ``"tg_330959414"``).

    Implementation uses the same direct ``api.telegram.org``
    sendMessage path that ``app.contracts.admin_alert`` uses for
    failure DMs — bypassing ``app.tools.telegram.telegram_send_dm``
    entirely. Reasons:

      1. ``telegram_send_dm`` takes ``person`` (display name /
         username) and does a roster NAME lookup. Numeric ids never
         match a name → ``status="not_found"`` is returned. The
         pre-2026-05-14 contract adapter actually called it with
         ``user_id=`` as the kwarg name, which raised a
         ``TypeError`` on every fire — the adapter had been
         non-functional since the day it was written.
      2. The roster may not contain a recently-paired admin; direct
         send works regardless. ``_resolve_chat_id`` already handles
         roster lookup, ``tg_`` prefix variants, and numeric
         fallback (Telegram private chats: chat_id == user_id).
    """
    from app.contracts.admin_alert import (
        _resolve_chat_id,
        _send_via_telegram_direct,
    )

    if "user_id" not in args or "text" not in args:
        raise ValueError("telegram_dm requires args.user_id and args.text")

    user_id = str(args["user_id"])
    chat_id = _resolve_chat_id(user_id)
    if chat_id is None:
        raise RuntimeError(
            f"telegram_dm: cannot resolve user_id={user_id!r} to a "
            "chat_id (roster miss + non-numeric id). Either register "
            "the user in the roster (have them message the bot once) "
            "or use a numeric Telegram id directly."
        )

    ok, detail = await _send_via_telegram_direct(chat_id, str(args["text"]))
    if not ok:
        raise RuntimeError(
            f"telegram_dm: send rejected (user_id={user_id!r}, "
            f"chat_id={chat_id}, detail={detail!r})"
        )
    return {"status": "success", "chat_id": chat_id, "detail": detail}


# ---------------------------------------------------------------------------
# sheet_append — append a row to a Google Sheet (dedup log, audit log)
# ---------------------------------------------------------------------------


def _contract_author(state: dict[str, Any]) -> str:
    """Resolve the contract author's email from state for OAuth lookup."""
    meta = state.get("__contract__") or {}
    author = meta.get("author") or ""
    if not author:
        raise RuntimeError(
            "contract author missing from state.__contract__; per-user "
            "OAuth adapters cannot resolve credentials. Re-freeze the "
            "contract with an author email."
        )
    return author


async def _google_token_for_author(author_email: str) -> str:
    """Fetch a valid Google access token for the contract's author.
    Delegates to the same token-refresh path the agent tools use."""
    from app.tools.google_drive import _get_valid_token
    token = await _get_valid_token(author_email)
    if not token:
        raise RuntimeError(
            f"Google not connected for author {author_email!r}. Author "
            f"must run `google_connect` from chat to authorize the "
            f"scopes used by this contract."
        )
    return token


@register_adapter(
    "sheet_append",
    required=["spreadsheet_id", "row"],
    optional=["range"],
    aliases={"spreadsheet_id": ["source"]},
)
async def sheet_append(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Append ``args.row`` (list of cell values) to
    ``args.spreadsheet_id``. ``args.range`` (e.g. ``"Sheet1"``) targets
    the worksheet; defaults to ``"Sheet1"``. Always appends to the
    bottom — never overwrites existing rows.

    Auth: contract author's stored OAuth (via state.__contract__.author).
    """
    import httpx

    spreadsheet_id = args.get("spreadsheet_id") or args.get("source") or ""
    row = args.get("row")
    if not spreadsheet_id:
        raise ValueError("sheet_append requires args.spreadsheet_id")
    if not isinstance(row, list):
        raise ValueError("sheet_append requires args.row to be a list of cell values")

    # Accept URL or bare ID — server-side extraction prevents typo traps.
    from app.tools.google_drive import _extract_drive_id
    spreadsheet_id = _extract_drive_id(spreadsheet_id)

    rng = args.get("range") or "Sheet1"
    token = await _google_token_for_author(_contract_author(state))

    api = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}:append"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            api,
            params={
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"values": [row]},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Sheets {resp.status_code} on values:append "
                f"spreadsheet_id={spreadsheet_id!r} range={rng!r}: "
                f"{resp.text[:300]}"
            )
        data = resp.json()
    return {
        "status": "ok",
        "spreadsheet_id": spreadsheet_id,
        "updated_range": data.get("updates", {}).get("updatedRange"),
        "updated_rows": data.get("updates", {}).get("updatedRows"),
    }


# ---------------------------------------------------------------------------
# drive_doc_fill — replace {{field}} placeholders in a Google Doc
# ---------------------------------------------------------------------------


@register_adapter(
    "drive_doc_fill",
    required=["doc_id", "fields"],
    aliases={"doc_id": ["document_id"]},
)
async def drive_doc_fill(
    args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Replace ``{{field_name}}`` placeholders in a Google Doc with the
    values in ``args.fields``. ``args.doc_id`` is the Doc ID OR URL.

    Auth: contract author's stored OAuth (Docs API).
    Uses ``documents.batchUpdate`` with one ``replaceAllText`` request
    per field. Matching is case-sensitive and matches the LITERAL
    ``{{field}}`` token, not a regex.
    """
    import httpx

    doc_id_raw = args.get("doc_id") or args.get("document_id") or ""
    fields = args.get("fields") or {}
    if not doc_id_raw:
        raise ValueError("drive_doc_fill requires args.doc_id")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("drive_doc_fill requires args.fields as a non-empty {name: value} dict")

    from app.tools.google_drive import _extract_drive_id
    doc_id = _extract_drive_id(doc_id_raw)

    token = await _google_token_for_author(_contract_author(state))
    requests_body = [
        {
            "replaceAllText": {
                "containsText": {"text": "{{" + str(name) + "}}", "matchCase": True},
                "replaceText": "" if value is None else str(value),
            },
        }
        for name, value in fields.items()
    ]

    api = f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            api,
            json={"requests": requests_body},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Docs {resp.status_code} on batchUpdate doc_id={doc_id!r}: "
                f"{resp.text[:300]}"
            )
        data = resp.json()
    replies = data.get("replies", [])
    replacements_made = sum(
        r.get("replaceAllText", {}).get("occurrencesChanged", 0) for r in replies
    )
    return {
        "status": "ok",
        "doc_id": doc_id,
        "fields_attempted": len(fields),
        "replacements_made": replacements_made,
    }


# ---------------------------------------------------------------------------
# email — DEFERRED (requires gmail.send OAuth scope; current scope is
# gmail.readonly only). Intentionally NOT registered so the validator
# rejects any contract that tries to use it. To enable: add
# 'https://www.googleapis.com/auth/gmail.send' to SCOPES in
# app/tools/google_oauth/web_flow.py, prompt all users to re-auth,
# then implement this adapter via the Gmail users.messages.send API.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# memory_update — write a Memory node + relations to the graph
# ---------------------------------------------------------------------------


@register_adapter(
    "memory_update",
    required=["namespace", "text", "short_description", "category"],
    optional=[
        "tags",
        "related_memories",
        "related_people",
        "related_entities",
        "force_create",
        "reviewed_relatives",
    ],
)
async def memory_update(
    args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Persist a :Memory node produced by the reasoning chain.

    Args:
      - ``namespace`` (str): personal | professional | technical
      - ``text`` (str): full record body (embedded for semantic search)
      - ``short_description`` (str): brief title/summary
      - ``category`` (str): one of the MEMORY_CATEGORIES (idea / memory /
        knowledge / procedure / experiment / incident / project /
        technical / strategy / communication_style / policy / operational)
      - ``tags`` (list[str]): keyword tags (optional; defaults to [])
      - ``related_memories``, ``related_people``, ``related_entities``:
        same shapes as ``create_record``'s args
      - ``force_create`` (bool): bypass dedup gate after explicit
        confirmation (don't set True by default)
    """
    from app.tools.memory_tools import create_record
    from app.contracts._loader_context import LoaderContext

    namespace = args.get("namespace")
    text = args.get("text")
    short_description = args.get("short_description")
    category = args.get("category")
    if not (namespace and text and short_description and category):
        raise ValueError(
            "memory_update requires args.namespace, args.text, "
            "args.short_description, args.category"
        )

    result = await create_record(
        namespace=namespace,
        text=text,
        short_description=short_description,
        category=category,
        tags=args.get("tags") or [],
        related_people=args.get("related_people"),
        related_memories=args.get("related_memories"),
        related_entities=args.get("related_entities"),
        author=_contract_author(state),
        force_create=bool(args.get("force_create", False)),
        reviewed_relatives=bool(args.get("reviewed_relatives", True)),
        tool_context=LoaderContext(),
    )
    if isinstance(result, dict) and result.get("status") not in ("success", "ok"):
        raise RuntimeError(
            f"memory_update create_record returned non-success: "
            f"{result.get('status')!r} — {result.get('message', '')[:200]}"
        )
    return {"status": "ok", "record_id": result.get("record_id"), "create_result": result}


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


@register_gate(
    "sheet_dedup",
    required=["source", "key"],
    optional=["range"],
    aliases={"source": ["spreadsheet_id"]},
)
async def sheet_dedup(args: dict[str, Any], state: dict[str, Any]) -> bool:
    """Return True if the emit is allowed to proceed (no duplicate row
    exists for the current ``args.key`` in the first column of the
    target sheet).

    Args:
      - ``source`` (str): spreadsheet ID or URL
      - ``key`` (str): value to search for in column A
      - ``range`` (str, optional): worksheet range; defaults to ``"Sheet1!A:A"``

    Auth: contract author's stored OAuth.
    Common pattern: ``key`` is today's date → re-fires on the same day
    are no-ops, late retries still post.
    """
    import httpx

    source_raw = args.get("source") or args.get("spreadsheet_id") or ""
    key = args.get("key")
    if not source_raw:
        raise ValueError("sheet_dedup requires args.source (spreadsheet ID or URL)")
    if key is None or key == "":
        raise ValueError("sheet_dedup requires args.key (value to look for in column A)")

    from app.tools.google_drive import _extract_drive_id
    spreadsheet_id = _extract_drive_id(source_raw)
    rng = args.get("range") or "Sheet1!A:A"

    token = await _google_token_for_author(_contract_author(state))
    api = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            api, headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code >= 400:
            # Fail-closed: if we can't read the dedup column, refuse the
            # emit (better to skip one fire than to double-post). Loud
            # error so admin sees the cause.
            raise RuntimeError(
                f"sheet_dedup read failed {resp.status_code} on "
                f"spreadsheet_id={spreadsheet_id!r} range={rng!r}: "
                f"{resp.text[:300]}"
            )
        data = resp.json()
    rows = data.get("values", []) or []
    key_str = str(key)
    for row in rows:
        if row and str(row[0]) == key_str:
            return False  # duplicate found → emit BLOCKED
    return True  # no duplicate → emit ALLOWED


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
