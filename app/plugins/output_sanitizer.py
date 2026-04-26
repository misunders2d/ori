"""OutputSanitizerPlugin — scan high-risk tool outputs for prompt injection.

Multilingual-safe by construction: when an embedding key is configured,
Stage 2 (semantic similarity vs anchor vectors) runs unconditionally —
the legacy English-language regex is no longer a fast-path skip but a
no-key fallback only.

Cost: ~one extra embedding API call per high-risk-tool result. At
~$0.0001 per call this is negligible against the cost of a successful
indirect injection from web_fetch / evolution_read_file output.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any

from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


# Tools whose responses may carry untrusted external content.
_HIGH_RISK_TOOLS: frozenset[str] = frozenset({"web_fetch", "evolution_read_file"})

# English-only regex — used ONLY in degraded mode (no embedding key).
# Documented and accepted: when no key is available, this is best-effort
# and English-only. With a key, it's never the gate.
_ENGLISH_INJECTION_REGEX = re.compile(
    r"(?i)"
    r"(?:ignore|disregard|forget|override|bypass)\s+"
    r"(?:all|any|your|previous|prior|above|the)\s+"
    r"(?:instructions?|directives?|rules?|context|prompts?|guidelines?)"
    r"|(?:you\s+are\s+now|new\s+system\s+prompt|act\s+as\s+if)"
    r"|(?:print|reveal|show|output)\s+(?:your|the|system)\s+(?:prompt|instructions?|rules?)"
    r"|<\s*(?:system|instruction|prompt)\s*>"
    r"|\[INST\]|\[/INST\]|<<SYS>>|<\|im_start\|>"
    r"|(?:important\s+message\s+from\s+the\s+developer)"
    r"|(?:end\s+of\s+document\.?\s+new\s+system\s+message)"
    r"|(?:begin\s+admin\s+override)"
)

_INDIRECT_THRESHOLD = 0.82
_VECTORS_CACHE: list[list[float]] | None = None


def _load_vectors() -> list[list[float]]:
    global _VECTORS_CACHE
    if _VECTORS_CACHE is not None:
        return _VECTORS_CACHE
    path = Path(__file__).parent.parent / "runtime" / "guardrail_embeddings.json"
    if not path.exists():
        _VECTORS_CACHE = []
        return _VECTORS_CACHE
    with open(path) as f:
        _VECTORS_CACHE = json.load(f)
    return _VECTORS_CACHE


def _cosine(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    m1 = math.sqrt(sum(a * a for a in v1))
    m2 = math.sqrt(sum(b * b for b in v2))
    if m1 * m2 == 0:
        return 0.0
    return dot / (m1 * m2)


def _extract_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content", "")
        return content if content else str(result)
    if isinstance(result, str):
        return result
    return ""


class OutputSanitizerPlugin(BasePlugin):
    """Scan high-risk tool outputs for prompt injection signals."""

    def __init__(self) -> None:
        super().__init__(name="output_sanitizer")

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        if tool.name not in _HIGH_RISK_TOOLS:
            return None

        text = _extract_text(result)
        if not text or len(text) < 20:
            return None

        vectors = _load_vectors()
        google_key = os.environ.get("GOOGLE_API_KEY", "").strip()

        if not (vectors and google_key):
            # Degraded mode — English regex is best-effort.
            if _ENGLISH_INJECTION_REGEX.search(text):
                logger.warning(
                    "OutputSanitizerPlugin: regex match in %s output (no embedding key — English-only coverage)",
                    tool.name,
                )
                return {
                    "status": "blocked",
                    "error_code": "INJECTION_DETECTED_REGEX",
                    "message": (
                        "Content from this source was blocked: prompt-injection "
                        "patterns detected. Configure GOOGLE_API_KEY to enable "
                        "multilingual semantic checks."
                    ),
                }
            return None

        # Multilingual safety: always run Stage 2 when key+vectors available.
        match = _ENGLISH_INJECTION_REGEX.search(text)
        if match:
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 200)
            fragment = text[start:end]
        else:
            # Front-load focus — injection prologues typically appear early.
            fragment = text[:500]

        try:
            from google.genai import Client
            client = Client(api_key=google_key)
            from app.util.models import MODEL_DEFAULTS
            embed_model = MODEL_DEFAULTS["embedding"].partition("/")[2]
            resp = client.models.embed_content(model=embed_model, contents=[fragment])
            if not resp or not resp.embeddings:
                return None
            user_vec = resp.embeddings[0].values
            for anchor in vectors:
                sim = _cosine(user_vec, anchor)
                if sim >= _INDIRECT_THRESHOLD:
                    logger.warning(
                        "OutputSanitizerPlugin: blocked %s output (similarity=%.2f)",
                        tool.name, sim,
                    )
                    return {
                        "status": "blocked",
                        "error_code": "INJECTION_DETECTED_SEMANTIC",
                        "message": (
                            f"Content from this source was blocked: potential prompt "
                            f"injection detected in external content "
                            f"(similarity={sim:.2f}). The fetched content has been "
                            "discarded for safety."
                        ),
                    }
        except Exception:
            logger.exception("OutputSanitizerPlugin: embedding check failed for %s", tool.name)
        return None
