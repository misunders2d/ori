"""Helpers for transport-facing tool artifacts."""

import json
import logging
import os
from typing import Any, Iterator

logger = logging.getLogger(__name__)


_INLINE_FILE_REDACTION = "[inline file bytes redacted; transport delivery uses the stored scratchpad payload]"


def _get_any(data: dict, *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def iter_inline_file_payloads(value: Any) -> Iterator[dict]:
    """Recursively yield dict payloads carrying inline base64 file data."""
    if isinstance(value, dict):
        data_base64 = value.get("data_base64")
        if isinstance(data_base64, str) and data_base64:
            yield value

        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from iter_inline_file_payloads(nested)
    elif isinstance(value, list):
        for item in value:
            yield from iter_inline_file_payloads(item)


def inline_file_summaries(value: Any) -> list[dict]:
    """Return model-safe metadata for inline file payloads."""
    summaries = []
    for payload in iter_inline_file_payloads(value):
        data_base64 = payload.get("data_base64", "")
        summaries.append(
            {
                "filename": _get_any(payload, "filename", "name", "display_name", "displayName"),
                "mime_type": _get_any(payload, "mime_type", "mimeType"),
                "size_bytes": payload.get("size_bytes"),
                "base64_chars": len(data_base64),
            }
        )
    return summaries


def redact_inline_file_data(value: Any) -> Any:
    """Return a copy with inline file bytes replaced by a small placeholder."""
    if isinstance(value, dict):
        redacted = {}
        for key, nested in value.items():
            if key == "data_base64" and isinstance(nested, str) and nested:
                redacted[key] = _INLINE_FILE_REDACTION
            elif isinstance(nested, (dict, list)):
                redacted[key] = redact_inline_file_data(nested)
            else:
                redacted[key] = nested
        return redacted
    if isinstance(value, list):
        return [redact_inline_file_data(item) for item in value]
    return value


def load_spilled_tool_response(session_id: str | None, scratchpad_name: str | None) -> dict | None:
    """Load a spilled tool response from the per-session scratchpad."""
    if not session_id or not scratchpad_name:
        return None

    try:
        from app.tools.scratchpad import _pad_path

        path = _pad_path(session_id, scratchpad_name)
        if not os.path.isfile(path):
            logger.warning("Transport artifact collection could not find spilled tool output: %s", scratchpad_name)
            return None

        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None

        loaded = json.loads(content)
        return loaded if isinstance(loaded, dict) else None
    except Exception as e:
        logger.warning("Transport artifact collection could not load spilled tool output %s: %s", scratchpad_name, e)
        return None


def iter_transport_tool_responses(session_id: str | None, tool_response: Any) -> Iterator[dict]:
    """Yield the visible tool response and its spilled backing response, if any."""
    if not isinstance(tool_response, dict):
        return

    yield tool_response

    if tool_response.get("status") != "spilled":
        return

    spilled = load_spilled_tool_response(session_id, tool_response.get("scratchpad_name"))
    if spilled is not None:
        yield spilled


def iter_file_payloads(value: Any, session_id: str | None) -> Iterator[dict]:
    """Recursively yield dict payloads carrying inline base64 file data."""
    if isinstance(value, dict):
        data_base64 = value.get("data_base64")
        if isinstance(data_base64, str) and data_base64:
            yield value

        if value.get("status") == "spilled":
            spilled = load_spilled_tool_response(session_id, value.get("scratchpad_name"))
            if spilled is not None:
                yield from iter_file_payloads(spilled, session_id)

        for nested in value.values():
            if isinstance(nested, (dict, list)):
                yield from iter_file_payloads(nested, session_id)
    elif isinstance(value, list):
        for item in value:
            yield from iter_file_payloads(item, session_id)
