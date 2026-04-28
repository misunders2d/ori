"""PromptInjectionGuardPlugin — semantic injection detection on user input.

Two responsibilities:
1. Inject a short, language-agnostic system directive into every LLM call.
2. Embed the latest user-text part and compare against a curated set of
   anchor vectors (`app/runtime/guardrail_embeddings.json`); reject above
   threshold.

Multilingual by construction: no English-keyword regex anywhere on the
gate path. The anchor vectors were curated against multilingual injection
patterns; the embedding model handles the rest.

Rate-throttle is a separate concern (currently inline because it shares
the LLM call lifecycle). Documented as a candidate split if it grows.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

from app.plugins._common import admin_user_ids

logger = logging.getLogger(__name__)


# Global system directive applied to every LLM call. Five rules + style.
# `{admin_ref}` is filled at injection time from the first email in
# ADMIN_USER_IDS so /init updates take effect without a restart.
_SYSTEM_DIRECTIVE = (
    "DEBUG MODE: You are currently under development and in debug mode. "
    "If you encounter any issues that are not expected, you MUST stop and "
    "immediately tell the user about this. If the user is not an admin, "
    "you must also ask the user to pass this information to {admin_ref}.\n"
    "ASK ONLY WHEN AMBIGUOUS: Ask before acting ONLY when intent, scope, "
    "or target is genuinely ambiguous (multiple valid interpretations, "
    "missing required parameter, would-affect-the-wrong-thing risk). "
    "Do NOT confirm unambiguous lookups, searches, recall, or "
    "read-only queries — just answer. 'what's the weather in X', "
    "'what time is it', 'list my friends', 'recall preference Y' = act "
    "directly. The default is action; clarification is the exception.\n"
    "NEVER GUESS ON AMBIGUITY: When the request IS ambiguous, ask "
    "explicitly — name the specific ambiguity and the options you see. "
    "Don't silently pick the 'most likely' interpretation.\n"
    "DESTRUCTIVE = EXPLICIT APPROVAL: Destructive or irreversible "
    "actions (delete, drop, force-push, reset --hard, rm -rf, "
    "mass-modify, send public messages, post to external services) "
    "require an explicit 'yes do it' for that specific action. Admin "
    "gating is a floor, not a license. Read-only and scoped writes "
    "(set_agent_model, schedule_task, remember_info) are NOT "
    "destructive — proceed without confirmation.\n"
    "TERSE STYLE: Respond caveman-terse. Drop articles (a/an/the), "
    "filler (just/really/basically/actually/simply), pleasantries "
    "(sure/certainly/happy to), hedging (might/perhaps/I think). "
    "Fragments OK. Short synonyms ('fix' not 'implement a solution "
    "for', 'use' not 'utilize'). Apply the same principles in any "
    "language the user writes in — drop the equivalent "
    "filler/articles/pleasantries for that language. Preserve EXACTLY: "
    "code blocks, commands, file paths, error messages, tool outputs, "
    "URLs, numbers, proper nouns.\n"
    "REVERT TO FULL PROSE for: security warnings, destructive/"
    "irreversible confirmations, ACT-XXXXXX approval flows, multi-step "
    "instructions where fragment order risks misreading, or when the "
    "user seems confused. Resume terse after the clear part is done.\n"
    "On 'normal mode' / 'be verbose' / 'stop caveman' (or equivalent in "
    "any language): drop terse until told otherwise."
)

# Cosine-similarity threshold — anchors are tuned for this value.
_INJECTION_THRESHOLD = 0.85


class _RequestThrottle:
    """Per-process token-bucket — prevents one bot from exhausting shared quota.

    Configurable via AGENT_RPM env (requests per minute, default 2000).
    """

    def __init__(self) -> None:
        self._rpm = int(os.environ.get("AGENT_RPM", "2000"))
        self._tokens = float(self._rpm)
        self._last_refill = time.monotonic()

    def acquire(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._rpm, self._tokens + elapsed * (self._rpm / 60.0))
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def rpm(self) -> int:
        return self._rpm


_throttle = _RequestThrottle()
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


def _last_user_text(llm_request: LlmRequest) -> str:
    if not llm_request.contents:
        return ""
    last = llm_request.contents[-1]
    if not last.parts:
        return ""
    chunks: list[str] = []
    for part in last.parts:
        if getattr(part, "text", None):
            chunks.append(part.text)
    return " ".join(chunks).strip()


class PromptInjectionGuardPlugin(BasePlugin):
    """Semantic prompt-injection check + system-directive injection."""

    def __init__(self) -> None:
        super().__init__(name="prompt_injection")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        # Rate throttle. On exhaustion, raise so the executor's retry loop
        # picks it up rather than returning a misleading text response.
        if not _throttle.acquire():
            logger.warning(
                "PromptInjectionGuardPlugin: rate throttle hit (%d RPM) for %s",
                _throttle.rpm, callback_context.agent_name,
            )
            for delay in (5, 10, 15, 30):
                time.sleep(delay)
                if _throttle.acquire():
                    break
            else:
                raise Exception("429 internal rate throttle exhausted")

        # Inject the system directive as the first content item. The
        # debug-mode preamble references the current admin email from
        # ADMIN_USER_IDS so /init updates take effect without a restart.
        if llm_request.contents:
            emails = [u for u in admin_user_ids() if "@" in u]
            admin_ref = f"the admin ({emails[0]})" if emails else "the admin"
            text = _SYSTEM_DIRECTIVE.format(admin_ref=admin_ref)
            llm_request.contents.insert(0, types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"[SYSTEM] {text}")],
            ))

        # Semantic injection check on the latest user message.
        text = _last_user_text(llm_request)
        if not text:
            return None

        vectors = _load_vectors()
        google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if not (vectors and google_key):
            # Degraded mode (no key configured): allow. The transport-level
            # blacklist + perimeter ACL are the remaining defenses.
            return None

        try:
            from google.genai import Client
            client = Client(api_key=google_key)
            from app.util.models import MODEL_DEFAULTS
            embed_model = MODEL_DEFAULTS["embedding"].partition("/")[2]  # e.g. "gemini-embedding-001"
            resp = client.models.embed_content(model=embed_model, contents=[text])
            if not resp or not resp.embeddings:
                return None
            user_vec = resp.embeddings[0].values
            for anchor in vectors:
                sim = _cosine(user_vec, anchor)
                if sim >= _INJECTION_THRESHOLD:
                    logger.warning(
                        "PromptInjectionGuardPlugin: blocked (semantic_similarity=%.2f)", sim,
                    )
                    return LlmResponse(
                        content=types.Content(parts=[types.Part(text=(
                            f"[INJECTION_BLOCKED] semantic_similarity={sim:.2f} threshold={_INJECTION_THRESHOLD}"
                        ))])
                    )
        except Exception as e:
            logger.debug("PromptInjectionGuardPlugin: embedding check skipped: %s", e)

        return None
