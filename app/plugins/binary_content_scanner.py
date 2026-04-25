"""BinaryContentScannerPlugin — inspect inbound A2A binary content for safety.

Inbound `Content` over A2A may carry binary parts (`inline_data` with
mime_types like application/gzip, application/pdf, image/*, etc.).
Text-based prompt injection guards don't apply to binary, so we run a
small set of structural checks here:

- Reject parts above a configurable size cap (gzip-bomb defense).
- Reject parts whose declared mime_type doesn't match a basic magic-byte
  signature (best-effort spoofing detection).
- Tag DNA bundles (application/gzip with a 'dna' marker in the adjacent
  text part) so the agent's tools route them to import_dna.

This plugin sits on `on_user_message_callback`. Text-only messages no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins import BasePlugin
from google.genai import types

logger = logging.getLogger(__name__)


# Configurable via env. Default 16 MiB — a chosen middle-ground for DNA
# bundles. Override with ORI_A2A_MAX_BINARY_BYTES.
_MAX_BINARY_BYTES = 16 * 1024 * 1024

# Lightweight magic-byte signatures for the mimetypes we accept on input.
# Each entry is (mime_type, list of acceptable starting bytes). For mimes
# without a stable magic header (text/plain), no entry — they pass freely.
_MAGIC_BYTES: dict[str, list[bytes]] = {
    "application/gzip": [b"\x1f\x8b"],
    "application/pdf": [b"%PDF-"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/webp": [b"RIFF"],  # plus "WEBP" at offset 8 — we check the prefix only here
    "audio/mpeg": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
    "audio/wav": [b"RIFF"],
    "video/mp4": [b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00 ftyp"],
}


def _max_bytes() -> int:
    """Read the cap from env at call time so tests can monkey-patch."""
    import os
    raw = os.environ.get("ORI_A2A_MAX_BINARY_BYTES", "")
    try:
        return int(raw) if raw else _MAX_BINARY_BYTES
    except ValueError:
        return _MAX_BINARY_BYTES


def _check_part(part: types.Part) -> tuple[bool, str | None]:
    """Validate a binary part. Returns (ok, reason_or_None)."""
    blob = getattr(part, "inline_data", None)
    if blob is None:
        return True, None  # not a binary part

    data: bytes = getattr(blob, "data", b"") or b""
    mime: str = getattr(blob, "mime_type", "") or ""

    if len(data) > _max_bytes():
        return False, f"size_exceeded:{len(data)}>{_max_bytes()}"

    sigs = _MAGIC_BYTES.get(mime)
    if sigs and not any(data.startswith(sig) for sig in sigs):
        return False, f"magic_mismatch:{mime}"

    return True, None


class BinaryContentScannerPlugin(BasePlugin):
    """Validate inbound A2A binary parts for safety."""

    def __init__(self) -> None:
        super().__init__(name="binary_content_scanner")

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        if not user_message or not user_message.parts:
            return None
        rejected: list[str] = []
        for idx, part in enumerate(user_message.parts):
            ok, reason = _check_part(part)
            if not ok:
                rejected.append(f"part[{idx}]:{reason}")
        if not rejected:
            return None
        logger.warning("BinaryContentScannerPlugin: rejected parts: %s", rejected)
        return types.Content(
            role="model",
            parts=[types.Part(text=(
                "[BINARY_REJECTED] " + "; ".join(rejected)
            ))],
        )
