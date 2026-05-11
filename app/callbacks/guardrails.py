import asyncio
import json
import logging
import math
import os
import re
import time

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app.app_utils.models import get_model_name

logger = logging.getLogger(__name__)


def _initialized_model_provider(callback_context: CallbackContext) -> str:
    """Return provider for the BaseLlm instance currently attached to the agent."""
    invocation_context = getattr(callback_context, "_invocation_context", None)
    agent = getattr(invocation_context, "agent", None)
    model = getattr(agent, "model", None)
    if not model:
        return ""

    cls_name = model.__class__.__name__.lower()
    module = model.__class__.__module__.lower()

    if cls_name == "gemini" or module.endswith(".gemini_llm"):
        return "google"
    if cls_name == "claude" or module.endswith(".anthropic_llm"):
        return "anthropic"

    model_str = model if isinstance(model, str) else getattr(model, "model", "")
    if isinstance(model_str, str) and model_str:
        from app.app_utils.models import _parse_model_str

        provider, _ = _parse_model_str(model_str)
        return provider

    return ""


def _initialized_model_is_litellm(callback_context: CallbackContext) -> bool:
    """Return True when the agent is backed by ADK LiteLlm."""
    invocation_context = getattr(callback_context, "_invocation_context", None)
    agent = getattr(invocation_context, "agent", None)
    model = getattr(agent, "model", None)
    if not model:
        return False

    cls_name = model.__class__.__name__.lower()
    module = model.__class__.__module__.lower()
    return cls_name == "litellm" or module.endswith(".lite_llm")


# ---------------------------------------------------------------------------
# Per-container request throttle (token bucket)
# ---------------------------------------------------------------------------
# Prevents any single container from exhausting the shared API quota.
# Configurable via AGENT_RPM env var (requests per minute). Default: 2000.
# ---------------------------------------------------------------------------


class _RequestThrottle:
    """Simple token-bucket rate limiter."""

    def __init__(self):
        self._rpm = int(os.environ.get("AGENT_RPM", "2000"))
        self._tokens = float(self._rpm)
        self._last_refill = time.monotonic()

    def acquire(self) -> bool:
        """Try to acquire a token. Returns False if rate limit exceeded."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._rpm, self._tokens + elapsed * (self._rpm / 60.0))
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def rpm(self):
        return self._rpm


_throttle = _RequestThrottle()


def admin_tool_guardrail(tool, args, tool_context, **kwargs) -> dict | None:
    """
    Runtime Guardrail: Intercepts highly privileged tool calls before execution.
    For Admin users, it stages the intent and requires a follow-up token approval.
    For non-Admin users, it blocks execution entirely.
    """
    if not tool or not tool_context:
        return None

    # Whitelist-based transfer guard: non-admins can only transfer to explicitly safe agents.
    # New sub-agents are blocked by default until added here.
    if tool.name == "transfer_to_agent":
        _NONADMIN_ALLOWED_AGENTS = {  # Agents with their own access control
            "ClickUpAgent",
            "BigQueryAgent",
            "AmazonHeadAgent",
            "AmazonAgent",
            "AmazonMemoryAgent",
            "AmazonWorkspaceAgent",
            "AmazonDataAnalystAgent",
        }
        agent_target = args.get("agent_name", "").strip()

        if agent_target.lower() not in {a.lower() for a in _NONADMIN_ALLOWED_AGENTS}:
            current_state = tool_context.state.to_dict()
            user_id = current_state.get("user_id", "")

            admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
            admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

            is_a2a = user_id.startswith("A2A_USER_")
            if not is_a2a and (not admin_users or user_id not in admin_users):
                return {
                    "status": "error",
                    "message": f"Guardrail Intervention: Only Admin/Master users can transfer to `{agent_target}`. Your user_id ({user_id}) is unauthorized.",
                }
        return None

    # `reembed_entities` only stages when it would actually write. The
    # dry_run preview is cheap, read-only, and stating costs the admin
    # an extra TOTP round-trip for no real work.
    if tool.name == "reembed_entities" and args.get("dry_run", True):
        return None

    if tool.name in [
        "configure_integration",
        "remove_integration",
        "schedule_system_task",
        "schedule_recurring_system_task",
        "run_system_task_now",
        "update_self",
        "trigger_rollback",
        "evolution_commit_and_push",
        "evolution_git_pull",
        "evolution_git_reset",
        "evolution_sync_local_to_upstream",
        "get_my_a2a_key",
        "reembed_entities",
    ]:
        current_state = tool_context.state.to_dict()
        user_id = current_state.get("user_id", "")
        session_id = current_state.get("session_id", "")
        if not session_id:
            session = getattr(tool_context, "session", None)
            session_id = (
                getattr(session, "session_id", None)
                or getattr(session, "id", None)
                or ""
            )

        logger.info(
            f"DEBUG: admin_tool_guardrail(tool={tool.name}) - user_id='{user_id}'"
        )

        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
        admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

        # A2A callers with validated API keys are trusted
        is_a2a = user_id.startswith("A2A_USER_")
        if not is_a2a and (not admin_users or user_id not in admin_users):
            return {
                "status": "error",
                "message": f"Guardrail Intervention: Only Admin/Master users can invoke `{tool.name}`. Your user_id ({user_id}) is unauthorized.",
            }

        # FOR ADMINS: Stage the intent if not already approved
        # This replaces the framework-level confirmation UI with a messenger-agnostic token protocol.
        try:
            from app.core.pending_actions import stage_action

            token = stage_action(tool.name, args, user_id, session_id)

            logger.info(f"Admin Guardrail: Staged {tool.name} for {user_id} -> {token}")

            totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
            # 2FA Requirement: Required if ADMIN_TOTP_SECRET is set AND REQUIRE_2FA is true (default)
            require_2fa = os.environ.get("REQUIRE_2FA", "true").lower() == "true"

            if totp_secret and require_2fa:
                return {
                    "status": "error",  # Abort current execution
                    "message": (
                        f"**CRITICAL ACTION STAGED**\n\n"
                        f"To protect the system, the `{tool.name}` command requires explicit admin confirmation.\n\n"
                        f"Please reply with your token and 2FA code:\n"
                        f"`Approve {token} <your-6-digit-code>`\n\n"
                        f"_Note: This token expires in 15 minutes and is single-use._"
                    ),
                }
            else:
                return {
                    "status": "error",  # Abort current execution
                    "message": (
                        f"**CRITICAL ACTION STAGED**\n\n"
                        f"To protect the system, the `{tool.name}` command requires explicit admin confirmation.\n\n"
                        f"Please reply with:\n"
                        f"`Approve {token}`\n\n"
                        f"_Note: This token expires in 15 minutes and is single-use._"
                    ),
                }
        except Exception as e:
            logger.error(f"Failed to stage action in guardrail: {e}")
            return {
                "status": "error",
                "message": "Guardrail Error: Failed to stage your action for approval. Please check the logs.",
            }

    return None


_CACHED_VECTORS = None


def _get_cached_vectors():
    global _CACHED_VECTORS
    if _CACHED_VECTORS is not None:
        return _CACHED_VECTORS

    import os

    path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "guardrail_embeddings.json")
    )
    if not os.path.exists(path):
        _CACHED_VECTORS = []
        return _CACHED_VECTORS

    with open(path, "r") as f:
        _CACHED_VECTORS = json.load(f)
    return _CACHED_VECTORS


def _cosine_similarity(v1, v2):
    dot_product = sum(a * b for a, b in zip(v1, v2))
    magnitude1 = math.sqrt(sum(a * a for a in v1))
    magnitude2 = math.sqrt(sum(b * b for b in v2))
    if magnitude1 * magnitude2 == 0:
        return 0
    return dot_product / (magnitude1 * magnitude2)


_TOKEN_LIMIT = 900_000  # ~10% safety margin under 1M model limit
_CHARS_PER_TOKEN = 4  # Conservative estimate


def _estimate_tokens(llm_request: LlmRequest) -> int:
    """Estimate total token count from all contents in the LLM request."""
    total_chars = 0
    # System instruction
    if getattr(llm_request, "config", None) and getattr(
        llm_request.config, "system_instruction", None
    ):
        si = llm_request.config.system_instruction
        if hasattr(si, "parts"):
            for part in si.parts:
                if hasattr(part, "text") and part.text:
                    total_chars += len(part.text)
    # Conversation contents
    if llm_request.contents:
        for content in llm_request.contents:
            if content.parts:
                for part in content.parts:
                    if hasattr(part, "text") and part.text:
                        total_chars += len(part.text)
                    elif hasattr(part, "function_call") and part.function_call:
                        total_chars += len(str(part.function_call))
                    elif hasattr(part, "function_response") and part.function_response:
                        total_chars += len(str(part.function_response))
    return total_chars // _CHARS_PER_TOKEN


async def prompt_injection_guardrail(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """
    Runtime Guardrail: Inspects the LLM request before hitting the model.
    - Token gatekeeper: rejects if context exceeds 900K tokens
    - Prompt injection detection via semantic cosine similarity
    - Planner toggle and model hot-swap
    """
    # Token gatekeeper — reject and instruct agent to reduce context
    est_tokens = _estimate_tokens(llm_request)
    if est_tokens > _TOKEN_LIMIT:
        logger.warning(
            "Token estimate %d exceeds %d for %s. Rejecting.",
            est_tokens,
            _TOKEN_LIMIT,
            callback_context.agent_name,
        )
        return LlmResponse(
            content=types.Content(
                parts=[
                    types.Part(
                        text=(
                            f"CONTEXT OVERFLOW: Your request is ~{est_tokens:,} tokens, which exceeds the "
                            f"{_TOKEN_LIMIT:,} token safety limit. You MUST reduce your context before retrying:\n"
                            "1. Use `scratchpad_write` to save your intermediate findings to disk.\n"
                            "2. Summarize large tool outputs instead of keeping them in conversation.\n"
                            "3. If the session is too bloated, ask the user to /reset.\n"
                            "Do NOT retry the same request — it will fail again."
                        )
                    )
                ]
            )
        )

    # Per-container rate throttle — prevents one agent from exhausting shared API quota
    if not _throttle.acquire():
        logger.warning(
            "Rate throttle hit (%d RPM). Waiting for token refill for %s.",
            _throttle.rpm,
            callback_context.agent_name,
        )
        # Back off with increasing delays, up to ~60s total
        for delay in (5, 10, 15, 30):
            await asyncio.sleep(delay)
            if _throttle.acquire():
                break
        else:
            # Raise so agent_executor's retry loop handles it (with longer backoff)
            # instead of returning text that gets surfaced to the caller
            raise Exception("429 internal rate throttle exhausted")

    # Global system directive — injected into every LLM call, single source of truth
    _SYSTEM_DIRECTIVE = (
        "CLARIFY BEFORE ACTING: If the user's intent is ambiguous, ask before acting. "
        "Never assume and waste tokens on the wrong task.\n"
        "Prioritize quick responses, do not overthink unless explicitly asked to.\n"
        "TERSE STYLE: Respond caveman-terse. Drop articles (a/an/the), filler "
        "(just/really/basically/actually/simply), pleasantries (sure/certainly/happy to), "
        "hedging (might/perhaps/I think). Fragments OK. Short synonyms "
        "('fix' not 'implement a solution for', 'use' not 'utilize'). "
        "Preserve EXACTLY: code blocks, commands, file paths, error messages, tool outputs, "
        "URLs, numbers, proper nouns. "
        "Revert to normal prose for: security warnings, destructive/irreversible confirmations, "
        "ACT-XXXXXX approval flows, multi-step instructions where fragment order risks misreading, "
        "or when user is confused. Resume terse after the clear part is done. "
        "On 'normal mode' / 'be verbose' / 'stop caveman': drop terse until told otherwise."
    )
    llm_request.append_instructions([_SYSTEM_DIRECTIVE])

    sid = getattr(callback_context, "session", None)
    if sid:
        sid = getattr(sid, "session_id", None) or getattr(sid, "id", None)
        if sid and sid.startswith("sl_"):
            llm_request.append_instructions(
                [
                    "Reply using Slack mrkdwn (*bold*, _italics_, `code`, <url|label>), not GitHub Markdown."
                ]
            )
    # Global thinking switch — provider-agnostic. The on/off flag lives in
    # data/thinking_config.json; ``app.app_utils.thinking`` caches it with
    # mtime invalidation so this branch doesn't hit disk every turn.
    # Gemini's thinking lives on the per-call request, so we toggle it
    # here. LiteLlm-backed agents (Anthropic via OpenRouter etc.) are
    # toggled when the flag flips (``apply_to_agent_tree`` mutates each
    # model's ``_additional_args``), not per turn.
    if getattr(llm_request, "config", None) and hasattr(
        llm_request.config, "thinking_config"
    ):
        from app.app_utils import thinking as _thinking

        cfg = _thinking.load()
        if not cfg["enabled"]:
            llm_request.config.thinking_config = None
        else:
            # Leave thinking_config alone — Gemini will use whatever the
            # model defaults to (or whatever the caller pre-set). We avoid
            # constructing a ThinkingConfig here so the import stays out
            # of the callback hot path; the default-on behaviour is fine.
            pass

    # Hot-swap model override from session state
    model_key = f"model:{callback_context.agent_name}"
    model_override = callback_context.state.to_dict().get(model_key)
    if model_override:
        from app.app_utils.models import _parse_model_str

        override_provider, override_model_name = _parse_model_str(model_override)
        running_provider = _initialized_model_provider(callback_context)
        if override_provider == running_provider:
            # Gemini/Claude clients expect bare model names. LiteLLM needs the
            # provider prefix kept so openrouter/deepseek/... does not become
            # direct deepseek/... and hit the wrong backend.
            llm_request.model = (
                model_override.strip().strip("\"'")
                if _initialized_model_is_litellm(callback_context)
                else override_model_name
            )
        else:
            # Cross-provider swap: BaseLlm instance is locked at agent init,
            # cannot change backend mid-session. Persisted to .env for restart.
            logger.warning(
                "Cross-provider hot-swap requested for %s (%s -> %s). "
                "Takes effect after restart.",
                callback_context.agent_name,
                running_provider,
                override_provider,
            )

    if llm_request.contents:
        last_msg = llm_request.contents[-1]
        if last_msg.parts:
            text_to_check = ""
            for part in last_msg.parts:
                if part.text:
                    text_to_check += part.text + " "

            text_to_check = text_to_check.strip()
            if text_to_check:
                vectors = _get_cached_vectors()
                google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
                if vectors and google_key:
                    from google.genai import Client

                    client = Client(api_key=google_key)
                    try:
                        emb_response = client.models.embed_content(
                            model=get_model_name("embedding"), contents=[text_to_check]
                        )
                        if not emb_response or not emb_response.embeddings:
                            return
                        user_vector = emb_response.embeddings[0].values

                        THRESHOLD = 0.85
                        for v in vectors:
                            sim = _cosine_similarity(user_vector, v)
                            if sim >= THRESHOLD:
                                return LlmResponse(
                                    content=types.Content(
                                        parts=[
                                            types.Part(
                                                text=f"Guardrail Intervention: Prompt injection detected (Semantic Proximity: {sim:.2f}) and blocked."
                                            )
                                        ]
                                    )
                                )
                    except Exception:
                        pass
    return None


def admin_only_guardrail(callback_context: CallbackContext) -> types.Content | None:
    """
    Runtime Guardrail: Checks if the user is explicitly set in ADMIN_USER_IDS setup.
    If not, it preemptively returns Content to halt execution of the agent.
    """
    import os

    current_state = callback_context.state.to_dict()
    user_id = current_state.get("user_id", "")

    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    if not admin_users:
        return types.Content(
            parts=[
                types.Part(
                    text=f"Guardrail Intervention: ADMIN_USER_IDS is not configured in settings. Wait... Were you trying to find your ID to set this up? Here it is: `{user_id}`"
                )
            ]
        )

    # A2A callers with validated API keys are trusted (key was checked by a2a_server)
    if user_id.startswith("A2A_USER_"):
        return None  # Allow — authenticated A2A caller

    if user_id not in admin_users:
        return types.Content(
            parts=[
                types.Part(
                    text=f"Guardrail Intervention: Only Admin/Master users can invoke this agent. Your user_id (`{user_id}`) is unauthorized.\n\nTo add this ID to the admin list, run `update_self` from an already authorized platform and modify `ADMIN_USER_IDS`."
                )
            ]
        )

    return None


# ---------------------------------------------------------------------------
# After-tool guardrail: scan high-risk tool outputs for indirect injection
# ---------------------------------------------------------------------------

# Tools whose output may contain untrusted external content
_HIGH_RISK_TOOLS = {"web_fetch", "evolution_read_file"}

# Fast regex pre-filter — avoids an embedding API call on clean content
_INJECTION_REGEX = re.compile(
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


def tool_output_injection_guardrail(tool, args, tool_context, tool_response):
    """After-tool callback: scan high-risk tool outputs for prompt injection.

    Uses a two-stage approach:
      1. Fast regex pre-filter (zero latency on clean content)
      2. Semantic embedding similarity check (only when regex flags something)
    """
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")
    if tool_name not in _HIGH_RISK_TOOLS:
        return None  # pass through unmodified

    # Extract text content from the tool response
    content = ""
    if isinstance(tool_response, dict):
        content = tool_response.get("content", "")
        if not content:
            content = str(tool_response)
    elif isinstance(tool_response, str):
        content = tool_response

    if not content or len(content) < 20:
        return None

    # Stage 1: Fast regex pre-filter
    match = _INJECTION_REGEX.search(content)
    if not match:
        return None  # clean content — no embedding call needed

    # Stage 2: Semantic similarity check on the suspicious fragment
    vectors = _get_cached_vectors()
    if not vectors:
        return None

    google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not google_key:
        return None

    from google.genai import Client

    # Extract a ~300-char window around the match
    start = max(0, match.start() - 100)
    end = min(len(content), match.end() + 200)
    fragment = content[start:end]

    try:
        client = Client(api_key=google_key)
        emb_response = client.models.embed_content(
            model=get_model_name("embedding"), contents=[fragment]
        )
        if not emb_response or not emb_response.embeddings:
            return None
        user_vector = emb_response.embeddings[0].values

        for v in vectors:
            sim = _cosine_similarity(user_vector, v)
            if sim >= _INDIRECT_THRESHOLD:
                logger.warning(
                    "Indirect prompt injection blocked in %s output (similarity: %.2f)",
                    tool_name,
                    sim,
                )
                return {
                    "status": "blocked",
                    "message": (
                        f"Content from this source was blocked: potential prompt injection "
                        f"detected in external content (similarity: {sim:.2f}). "
                        f"The fetched content has been discarded for safety."
                    ),
                }
    except Exception:
        logger.exception("Error in tool_output_injection_check for %s", tool_name)

    return None


# ---------------------------------------------------------------------------
# After-tool guardrail: cap verification retry attempts for DeveloperAgent
# ---------------------------------------------------------------------------

_MAX_VERIFY_FAILURES = 3


def verify_retry_guardrail(tool, args, tool_context, tool_response):
    """After-tool callback: counts evolution_verify_sandbox failures in session state.

    After _MAX_VERIFY_FAILURES consecutive failures, blocks further verify attempts
    and instructs the agent to stop and report the issue instead of looping.
    A successful verification resets the counter.
    """
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")
    if tool_name != "evolution_verify_sandbox":
        return None

    # Determine if the verification passed or failed
    is_failure = False
    if isinstance(tool_response, dict):
        is_failure = tool_response.get("status") == "error"

    state = tool_context.state
    counter_key = "verify_failure_count"

    if not is_failure:
        # Success — reset counter
        state[counter_key] = 0
        return None

    # Increment failure counter
    current_count = state.get(counter_key, 0) + 1
    state[counter_key] = current_count

    if current_count >= _MAX_VERIFY_FAILURES:
        logger.warning(
            "DeveloperAgent hit verify retry limit (%d/%d). Halting further attempts.",
            current_count,
            _MAX_VERIFY_FAILURES,
        )
        return {
            "status": "error",
            "message": (
                f"RETRY LIMIT REACHED: Verification has failed {current_count} consecutive times. "
                f"You MUST stop attempting fixes. Report the issue back to the user with: "
                f"(1) what you were trying to do, (2) the error output, and "
                f"(3) what you found during your research. Do NOT call evolution_verify_sandbox again."
            ),
        }

    remaining = _MAX_VERIFY_FAILURES - current_count
    # Inject a nudge into the response to push toward research
    if isinstance(tool_response, dict):
        tool_response["retry_warning"] = (
            f"Verification failed ({current_count}/{_MAX_VERIFY_FAILURES} attempts used, {remaining} remaining). "
            f"You MUST research the error externally before retrying — use search_github_issues, "
            f"check_installed_package, or google_search_agent_tool."
        )

    return None


# ---------------------------------------------------------------------------
# After-tool guardrail: spill oversized tool outputs to scratchpad
# ---------------------------------------------------------------------------
# Prevents large tool outputs (BigQuery rowsets, Keepa product dumps, full
# Drive files, etc.) from ballooning the session past the LLM's context
# limit. When a tool returns more than the configured threshold, the full
# output is written to a scratchpad and the LLM only sees a lightweight
# reference + a short preview. The agent can call scratchpad_read(...) to
# load the full content if it actually needs it.
#
# Documented incident this prevents: 2026-04-30 FBA-discrepancy scheduled
# task — BigQuery output bloated session past 1M tokens, plan loop ran 25×
# against the poisoned session, exhausted paid-tier-2 input quota.
# ---------------------------------------------------------------------------

# Tools whose output should never be spilled — typically because they're
# already part of the spill machinery, or their output is structurally
# small no matter what.
_SPILL_EXEMPT_TOOLS = frozenset(
    {
        "scratchpad_write",
        "scratchpad_read",
        "scratchpad_replace",
        "scratchpad_clear",
        "scratchpad_list",
    }
)


def _tool_response_size(tool_response) -> tuple[int, str]:
    """Return (size_chars, text_repr) for a tool response. Empty/None → (0, '')."""
    if tool_response is None:
        return 0, ""
    if isinstance(tool_response, str):
        return len(tool_response), tool_response
    if isinstance(tool_response, dict):
        try:
            text = json.dumps(tool_response, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(tool_response)
        return len(text), text
    text = str(tool_response)
    return len(text), text


def tool_output_spillover_guardrail(tool, args, tool_context, tool_response):
    """After-tool callback: spill oversized outputs to scratchpad.

    Threshold via env var TOOL_OUTPUT_SPILL_THRESHOLD (default 8000 chars).
    Returns a lightweight reference dict in place of the original response;
    the agent can scratchpad_read(...) to retrieve full content on demand.
    """
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")
    if tool_name in _SPILL_EXEMPT_TOOLS:
        return None

    from app.core.tool_artifacts import inline_file_summaries, redact_inline_file_data

    threshold = int(os.environ.get("TOOL_OUTPUT_SPILL_THRESHOLD", "8000"))
    size, text = _tool_response_size(tool_response)
    if size <= threshold:
        return None  # under budget — pass through

    file_payloads = inline_file_summaries(tool_response)

    # Generate a unique scratchpad name. Short hex suffix avoids collisions
    # if the same tool spills twice in one session.
    import uuid as _uuid

    scratchpad_name = f"_spill_{tool_name or 'tool'}_{_uuid.uuid4().hex[:6]}"

    try:
        from app.tools.scratchpad import scratchpad_write

        scratchpad_write(scratchpad_name, text, tool_context=tool_context)
    except Exception as e:
        logger.warning(
            "tool_output_spillover: failed to write scratchpad %s for tool %s: %s",
            scratchpad_name,
            tool_name,
            e,
        )
        # Spillover failed — fall back to letting the original response through.
        # Better to risk context blow-up than to silently drop the data.
        return None

    logger.info(
        "tool_output_spillover: %s returned %d chars → spilled to %s",
        tool_name,
        size,
        scratchpad_name,
    )

    if file_payloads:
        safe_response = redact_inline_file_data(tool_response)
        try:
            preview = json.dumps(safe_response, default=str, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            preview = "[tool response contained inline file bytes; preview redacted]"
        read_instruction = (
            "The full tool response includes inline file bytes and is stored only for "
            "transport delivery. Do not load the stored inline bytes into context; use "
            "the filename/file_path metadata and tell the user the file is attached."
        )
    else:
        preview = text[:500]
        if len(text) > 500:
            preview += "…"
        read_instruction = (
            f"Output written to scratchpad. Call scratchpad_read('{scratchpad_name}') "
            f"to load the full content if you need it."
        )

    return {
        "status": "spilled",
        "tool": tool_name,
        "scratchpad_name": scratchpad_name,
        "summary": (
            f"Tool '{tool_name}' returned {size:,} chars (~{size // 4:,} tokens). "
            f"{read_instruction}"
        ),
        "size_chars": size,
        "size_tokens_estimate": size // 4,
        "preview": preview,
        "file_payloads": file_payloads,
    }


def plan_enforcer(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Injects active plan context into the model prompt to enforce step-by-step execution."""
    from app.tools.planner import get_active_plan_context

    session = (
        getattr(callback_context, "session", None)
        if hasattr(callback_context, "session")
        else None
    )
    if not session:
        return None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if not session_id:
        return None

    context = get_active_plan_context(session_id)
    if context and llm_request.contents:
        llm_request.append_instructions([context])

    return None


# Tools that are always allowed regardless of the active step's
# allowed_tools list — they manage the plan itself, control transport
# (the agent must be able to ask the user for confirmation / acknowledge
# completion), or are inherent to ADK's delegation primitives. Without
# this allow-list a hard-enforced plan would deadlock the agent.
_PLAN_EXEMPT_TOOLS = frozenset(
    {
        # Plan lifecycle
        "create_plan",
        "get_next_step",
        "complete_step",
        "get_plan_status",
        "abandon_plan",
        # Working memory the agent always needs access to
        "scratchpad_read",
        "scratchpad_write",
        "scratchpad_list",
        "scratchpad_replace",
        "scratchpad_clear",
        # ADK primitives + reflection
        "transfer_to_agent",
    }
)


def _glob_match(name: str, patterns: list[str]) -> bool:
    """True if `name` matches any glob pattern (fnmatch syntax)."""
    import fnmatch
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def plan_step_enforcer(tool, args, tool_context, **kwargs) -> dict | None:
    """before_tool guard: block tool calls outside the active step's allowed_tools.

    When the current step has an `allowed_tools` whitelist, only tool
    names that match one of those globs (or are in `_PLAN_EXEMPT_TOOLS`)
    are permitted through. Everything else returns a synthetic error
    response that tells the LLM what's allowed for this step, prompting
    it to either complete the step or abandon the plan.

    No active plan / no constraints / no in-progress step → pass through.
    This is layered on top of `plan_enforcer` (which still injects the
    prompt context) — the soft fence stays as guidance for the LLM, the
    hard fence here catches deviations the LLM tries anyway.
    """
    if not tool or not tool_context:
        return None

    tool_name = getattr(tool, "name", "") or ""
    if tool_name in _PLAN_EXEMPT_TOOLS:
        return None

    session = getattr(tool_context, "session", None)
    session_id = (
        getattr(session, "session_id", None)
        or getattr(session, "id", None)
        if session is not None
        else None
    )
    if not session_id:
        return None

    try:
        from app.tools.planner import get_current_step_constraints
        constraints = get_current_step_constraints(session_id)
    except Exception as e:
        logger.warning("plan_step_enforcer: planner unreachable (%s) — pass-through", e)
        return None

    if not constraints:
        return None  # no plan / no in-progress step / unconstrained step

    allowed = constraints.get("allowed_tools") or []
    if not allowed:
        return None  # only must_call set — let it through

    if _glob_match(tool_name, allowed):
        return None

    return {
        "status": "error",
        "message": (
            f"Plan-step guardrail: tool `{tool_name}` is not allowed for step "
            f"{constraints['step_id']} ({constraints['description']!r}). "
            f"Allowed for this step: {allowed}. "
            "Complete the current step (`complete_step`) or abandon the plan (`abandon_plan`) "
            "before calling other tools."
        ),
    }


_CALLER_ID_RE = re.compile(r"\[__caller_id:([^\]]+)__\]")


async def state_setter(
    callback_context: CallbackContext, **kwargs
) -> types.Content | None:
    """
    Sets initial fundamental session state keys to prevent KeyErrors during prompt evaluation.
    Extracts the real caller ID from message tags (for multi-user group chats).
    """
    import os

    current_state = callback_context.state.to_dict()
    current_user = callback_context.user_id

    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    # Extract real caller ID from the message tag (injected by extract_agent_response
    # for group chats where session user_id != actual sender).
    # callback_context.user_content is the Content that started this invocation.
    real_caller = None
    user_content = callback_context.user_content
    if user_content and hasattr(user_content, "parts") and user_content.parts:
        for part in user_content.parts:
            if hasattr(part, "text") and part.text:
                m = _CALLER_ID_RE.search(part.text)
                if m:
                    real_caller = m.group(1)
                    # Strip the tag so the model never sees it
                    part.text = _CALLER_ID_RE.sub("", part.text)
                break

    effective_user = real_caller or current_state.get("user_id") or current_user

    logger.info(
        f"DEBUG: state_setter() - current_user='{current_user}', real_caller='{real_caller}', effective='{effective_user}'"
    )

    callback_context.state["master_user_id"] = admin_users
    callback_context.state["user_id"] = effective_user

    # Load bot name from env (defaults to "Ori")
    callback_context.state["bot_name"] = os.environ.get("BOT_NAME", "Ori")

    # Load user preferences from disk into session state
    from app.tools.preferences import load_user_preferences

    prefs = load_user_preferences(effective_user)
    callback_context.state["user_preferences"] = prefs

    # Inject the current agent's model so it can answer "what model are you?"
    from app.app_utils.models import MODEL_DEFAULTS, get_model_string

    agent_name = callback_context.agent_name
    callback_context.state["current_model"] = get_model_string(agent_name) or "unknown"

    # Load persisted model overrides into session state
    for component in MODEL_DEFAULTS:
        effective_model = get_model_string(component)
        if effective_model != MODEL_DEFAULTS[component]:
            callback_context.state[f"model:{component}"] = effective_model

    return None


# ---------------------------------------------------------------------------
# A2A Privacy Guardrail: Prevent credential leaks in outbound calls/DNA
# ---------------------------------------------------------------------------


def a2a_privacy_guardrail(tool, args, tool_context, tool_response=None):
    """
    Deterministic secret-matching guardrail for A2A tools.
    Blocks any tool call or response that contains sensitive environment variables.
    """
    import os

    from app.app_utils.config import ALLOWED_CONFIG_KEYS

    # Get tool name
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")

    _A2A_RISK_TOOLS = {
        "call_friend",
        "call_agent",
        "export_dna",
        "add_friend",
        "web_fetch",
    }
    if tool_name not in _A2A_RISK_TOOLS:
        return None

    # Safe keys that are publicly known or not sensitive enough to block DNA exports
    _SAFE_KEYS = {
        "BOT_NAME",
        "GITHUB_REPO",
        "APP_NAME",
    }

    # Load all current secrets dynamically to support future evolution
    secrets = []
    for key in ALLOWED_CONFIG_KEYS:
        if key in _SAFE_KEYS:
            continue

        val = os.environ.get(key)
        # We only match secrets that are long enough to be unique/dangerous (e.g., > 6 chars)
        if val and len(str(val)) > 6:
            secrets.append(str(val))

    # Also catch the admin passcode and TOTP secret
    for extra_key in ["ADMIN_PASSCODE", "ADMIN_TOTP_SECRET"]:
        val = os.environ.get(extra_key)
        if val and len(str(val)) > 6:
            secrets.append(str(val))

    # 1. Check Arguments (Preventing leak via query/URL)
    args_json = json.dumps(args)
    for secret in secrets:
        if secret in args_json:
            logger.error(
                "A2A PRIVACY VIOLATION: Secret detected in arguments for %s", tool_name
            )
            return {
                "status": "error",
                "message": (
                    f"Guardrail Intervention: Outbound A2A tool call `{tool_name}` was blocked "
                    f"because it contains a sensitive system credential (API Key/Token). "
                    f"Privacy mandate: Technical DNA only. Never share credentials."
                ),
            }

    # 2. Check Response (Preventing leak via DNA packaging or fetching)
    if tool_response is not None:
        resp_json = json.dumps(tool_response)
        for secret in secrets:
            if secret in resp_json:
                logger.error(
                    "A2A PRIVACY VIOLATION: Secret detected in output of %s", tool_name
                )
                return {
                    "status": "error",
                    "message": (
                        f"Guardrail Intervention: Technical DNA from `{tool_name}` was blocked. "
                        f"A system secret was found in the generated package. DNA exchange cancelled."
                    ),
                }

    return None


# ---------------------------------------------------------------------------
# Pending-follow-up Guard — kill the "I'll get back to you" lie
# ---------------------------------------------------------------------------


# Tools whose successful return means "operation submitted, NOT complete".
# When one of these returns success, the agent MUST schedule a follow-up
# (via schedule_one_off_task) before responding to the user. The guardrail
# below annotates the response so the LLM sees the requirement inline.
#
# Add a tool here when:
#   - It submits work that completes asynchronously (Amazon report request,
#     ads report request, BigQuery long jobs, image generation jobs, …)
#   - Its successful return shape carries an operation/report/job id, not
#     the actual result
_LONG_RUNNING_SUBMIT_TOOLS: set[str] = {
    "sp_request_report",
    # Add Amazon Ads / BigQuery / image gen / etc. tools as they're wired:
    # "amazon_ads_request_report",
    # "bigquery_request_long_query",
    # "generate_image_async",
}

# Response keys the guard checks to find the operation id to thread into
# the agent's follow-up task. First match wins. New service trackers can
# add their key here.
_OPERATION_ID_KEYS: tuple[str, ...] = (
    "report_id",
    "operation_id",
    "job_id",
    "request_id",
    "task_id",
)

# Tools that are FOLLOWUPS or status-checks themselves — skip the guard
# so we don't recurse infinitely on rescheduling logic.
_FOLLOWUP_EXEMPT_TOOLS: set[str] = {
    "schedule_one_off_task",
    "schedule_recurring_task",
    "edit_scheduled_task",
    "delete_scheduled_task",
    "sp_check_report",
    "sp_download_report",
    # Contract pipeline tools never trigger the guard — contracts own
    # their polling discipline internally.
    "contract_freeze",
    "contract_schedule",
    "contract_dry_run",
    "contract_revise",
}


def pending_followup_guard(tool, args, tool_context, tool_response):
    """After-tool callback: detect long-running submissions + force a
    scheduled follow-up.

    When a tool from ``_LONG_RUNNING_SUBMIT_TOOLS`` returns success with
    an operation id, this callback annotates the tool response so the
    next LLM turn sees an explicit, hard-to-miss instruction:

        ⚠ This is a PENDING long-running op (key=value). Before you
        respond to the user, you MUST call schedule_one_off_task with
        strict steps to poll the op and post the result. Verbal
        promises without a scheduled follow-up are a lie.

    The annotation is added under ``__followup_required__`` so it does
    NOT pollute the response data for any tool consumers that read by
    known keys. It DOES surface in the LLM's tool-result message
    because the response dict is serialised whole.

    The agent is also free to read ``__followup_required__.suggested_steps``
    as a template for the steps[] list to pass to schedule_one_off_task —
    these steps follow the polling discipline (check, post-or-reschedule,
    cap on attempts).
    """
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")
    if tool_name in _FOLLOWUP_EXEMPT_TOOLS:
        return None
    if tool_name not in _LONG_RUNNING_SUBMIT_TOOLS:
        return None
    if not isinstance(tool_response, dict):
        return None
    if tool_response.get("status") not in ("success", "ok", None):
        return None  # already an error — nothing to follow up on

    op_id_key = None
    op_id_value = None
    for key in _OPERATION_ID_KEYS:
        val = tool_response.get(key)
        if val:
            op_id_key = key
            op_id_value = val
            break
    if not op_id_value:
        return None

    # Best-effort: extract the originating channel so the agent can wire
    # ``deliver_to`` for the follow-up. Falls back to "current chat".
    deliver_to = ""
    try:
        state = tool_context.state.to_dict() if tool_context else {}
        deliver_to = (
            state.get("deliver_to_session")
            or state.get("origin_session_id")
            or ""
        )
    except Exception:
        deliver_to = ""

    suggested_steps = [
        f"Call sp_check_report with report_id='{op_id_value}'. Capture processing_status verbatim.",
        (
            f"If processing_status == 'DONE': call sp_download_report with the "
            f"report_document_id; format a concise Slack mrkdwn summary "
            f"(orders, revenue, time window); post to the deliver_to channel; STOP."
        ),
        (
            f"If processing_status in ('IN_PROGRESS', 'IN_QUEUE'): call "
            f"schedule_one_off_task to re-fire this same self-check in +60 "
            f"seconds (decrement attempts in the task_prompt); STOP. Do NOT "
            f"poll synchronously."
        ),
        (
            f"If processing_status in ('CANCELLED', 'FATAL'): post "
            f"'Report {op_id_value} failed: <status>' to the channel; STOP."
        ),
        (
            "If you've already attempted 20+ self-checks (track count in "
            "task_prompt), post 'Report still pending after 20 minutes — "
            f"check sp_check_report(report_id={op_id_value!r}) manually' "
            "and STOP rescheduling."
        ),
    ]

    annotated = dict(tool_response)
    annotated["__followup_required__"] = {
        "operation_id_key": op_id_key,
        "operation_id": op_id_value,
        "tool_called": tool_name,
        "deliver_to_hint": deliver_to,
        "instruction": (
            f"⚠ PENDING long-running operation: {op_id_key}={op_id_value!r}. "
            "BEFORE you respond to the user with any 'I'll get back to you' / "
            "'will check later' language, you MUST call schedule_one_off_task "
            "with strict `steps` (see suggested_steps below) so the operation "
            "actually gets polled and the result actually gets posted. A "
            "verbal promise without a scheduled follow-up is a LIE — the "
            "turn ends and nothing fires."
        ),
        "suggested_steps": suggested_steps,
        "suggested_first_run_in_seconds": 60,
    }
    return annotated
