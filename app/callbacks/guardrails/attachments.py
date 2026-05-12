"""File-attachment plumbing — inline tool-generated files into the agent's
response so the A2A converter ships them as FileParts.

Background: tools like ``generate_chart`` and ``generate_image`` save bytes
to disk and return ``{"status": "success", "file_path": "..."}``. The
Slack / Telegram pollers call ``extract_agent_response`` which scans
function_response parts for ``file_path`` and reads the bytes directly,
attaching them to ``AgentResponse.media_items``.

The A2A path is different: ``to_a2a()`` wraps the runner and translates
event parts via ``convert_genai_part_to_a2a_part``. Function-response
parts become A2A ``DataPart`` (JSON metadata only) — NEVER ``FilePart``.
Only ``Part(inline_data=Blob(...))`` becomes an A2A ``FilePart`` carrying
base64 bytes. Without an inline_data part in the event stream, the chart
never reaches a Streamlit / A2A peer; the agent ends up claiming
"attached" while the bytes stayed on the server.

Fix: capture file paths emitted by tools (``file_attachment_capture``,
after_tool), then attach them as inline_data parts to the agent's model
response (``file_attachment_inject``, after_model). The A2A converter
picks them up natively. For Slack / Telegram, ``extract_agent_response``
is modified to dedupe via a path marker on the inline_data so the file
isn't attached twice.

Marker convention: ``Part.inline_data.display_name`` is set to
``f"{_FILE_ATTACHMENT_MARKER}{file_path}"``. The dedupe layer matches
on the prefix and removes the path from the function-response branch
at attachment time. Other consumers ignore the marker.
"""

import logging
import mimetypes
import os

from google.genai import types

logger = logging.getLogger(__name__)


_FILE_ATTACHMENT_MARKER = "__contract_file:"
_PENDING_FILE_PARTS_KEY = "__pending_file_parts__"
_FILE_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB — match A2A_INBOUND cap


def file_attachment_capture(tool, args, tool_context, tool_response):
    """After-tool callback: stash a tool's emitted file_path so the next
    model response can inline it as an A2A-shippable Part.

    Runs AFTER ``tool_output_spillover_guardrail``: when spillover
    replaces a large response, the replacement carries ``file_payloads``
    instead of a top-level ``file_path``. We check both shapes — the
    common case is just ``file_path`` (small dicts, no spill).
    """
    if not isinstance(tool_response, dict):
        return None

    candidates: list[str] = []
    fp = tool_response.get("file_path")
    if isinstance(fp, str) and fp:
        candidates.append(fp)

    # Spillover-friendly: also grab file_path entries from file_payloads
    # metadata (the spilled response keeps the path even after bytes are
    # redacted into the scratchpad).
    payloads = tool_response.get("file_payloads")
    if isinstance(payloads, list):
        for p in payloads:
            if isinstance(p, dict):
                pp = p.get("filename") or p.get("file_path") or p.get("path")
                if isinstance(pp, str) and pp:
                    candidates.append(pp)

    if not candidates:
        return None

    # Read the existing pending list via direct ``.get`` to avoid the
    # full ``to_dict()`` copy on every tool call. State can grow large
    # in long-running sessions; ``to_dict`` snapshots the whole thing,
    # which makes the after-tool chain quietly expensive when only one
    # key matters.
    try:
        pending_raw = (
            tool_context.state.get(_PENDING_FILE_PARTS_KEY, []) if tool_context else []
        )
    except Exception:
        pending_raw = []
    pending = list(pending_raw or [])

    changed = False
    for fp in candidates:
        try:
            abs_fp = os.path.abspath(fp)
        except Exception:
            continue
        if abs_fp in pending:
            continue
        if not os.path.isfile(abs_fp):
            continue
        if os.path.getsize(abs_fp) > _FILE_ATTACHMENT_MAX_BYTES:
            logger.warning(
                "file_attachment_capture: %s exceeds %d bytes — skipping inline",
                abs_fp,
                _FILE_ATTACHMENT_MAX_BYTES,
            )
            continue
        pending.append(abs_fp)
        changed = True

    if changed and tool_context is not None:
        try:
            tool_context.state[_PENDING_FILE_PARTS_KEY] = pending
        except Exception as e:
            logger.warning("file_attachment_capture: state write failed: %s", e)

    return None  # pass-through; don't mutate the tool response


def file_attachment_inject(callback_context, llm_response):
    """After-model callback: drain ``__pending_file_parts__`` and append
    each as an inline_data Part on the model's response.

    Runs once per model turn. Each successfully-attached path is
    removed from state so subsequent model responses don't re-attach
    the same file. Failures (missing file, oversized, IO error) are
    logged and the entry is dropped from the pending list — better
    than blocking the turn forever on a stale path.
    """
    # Fast-path: read the pending list via direct ``.get`` so we don't
    # pay for a full ``state.to_dict()`` copy on every model turn. On
    # large sessions that copy is the slow part — most turns have no
    # pending files and can early-exit in O(1).
    try:
        pending_raw = (
            callback_context.state.get(_PENDING_FILE_PARTS_KEY, [])
            if callback_context
            else []
        )
    except Exception:
        return None

    pending = list(pending_raw or [])
    if not pending:
        return None

    response_content = getattr(llm_response, "content", None)
    if response_content is None or response_content.parts is None:
        # No response shape to attach to. Clear pending so we don't
        # ride this forever on a malformed turn.
        try:
            callback_context.state[_PENDING_FILE_PARTS_KEY] = []
        except Exception:
            pass
        return None

    appended_any = False
    remaining: list[str] = []

    def _emit_failure_placeholder(fp: str, reason: str) -> None:
        """Surface attach failures to the user via a text Part. Law 6:
        nothing fails silently. Logs already cover the operator side;
        this puts the failure on the wire so the agent's "attached"
        claim is contradicted in-band instead of the user wondering why
        nothing showed up."""
        nonlocal appended_any
        try:
            response_content.parts.append(
                types.Part(
                    text=f"\n\n[file attach failed — {os.path.basename(fp) or fp}: {reason}]"
                )
            )
            appended_any = True
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "file_attachment_inject: could not append failure placeholder for %s: %s",
                fp,
                e,
            )

    for fp in pending:
        if not os.path.isfile(fp):
            logger.info("file_attachment_inject: %s no longer present — dropping", fp)
            _emit_failure_placeholder(fp, "file missing at delivery time")
            continue
        try:
            size = os.path.getsize(fp)
            if size > _FILE_ATTACHMENT_MAX_BYTES:
                logger.warning(
                    "file_attachment_inject: %s grew past %d bytes — dropping",
                    fp,
                    _FILE_ATTACHMENT_MAX_BYTES,
                )
                _emit_failure_placeholder(
                    fp,
                    f"file exceeds inline cap ({size} > {_FILE_ATTACHMENT_MAX_BYTES} bytes)",
                )
                continue
            mime, _ = mimetypes.guess_type(fp)
            with open(fp, "rb") as f:
                data = f.read()
            part = types.Part(
                inline_data=types.Blob(
                    mime_type=mime or "application/octet-stream",
                    data=data,
                    display_name=f"{_FILE_ATTACHMENT_MARKER}{fp}",
                )
            )
            response_content.parts.append(part)
            appended_any = True
        except Exception as e:
            logger.warning(
                "file_attachment_inject: failed to attach %s: %s", fp, e
            )
            _emit_failure_placeholder(fp, f"read failed: {e}")

    try:
        callback_context.state[_PENDING_FILE_PARTS_KEY] = remaining
    except Exception:
        pass

    return llm_response if appended_any else None
