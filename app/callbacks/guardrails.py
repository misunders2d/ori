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

    if tool.name in [
        "configure_integration",
        "remove_integration",
        "schedule_system_task",
        "schedule_recurring_system_task",
        "run_system_task_now",
        "update_self",
        "trigger_rollback",
        "evolution_commit_and_push",
    ]:
        current_state = tool_context.state.to_dict()
        user_id = current_state.get("user_id", "")

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

            token = stage_action(tool.name, args, user_id, "")

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
    if llm_request.contents:
        llm_request.contents.insert(
            0,
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"[SYSTEM] {_SYSTEM_DIRECTIVE}")],
            ),
        )

    use_planner = callback_context.state.to_dict().get("use_planner", False)
    if not use_planner and getattr(llm_request, "config", None):
        if hasattr(llm_request.config, "thinking_config"):
            llm_request.config.thinking_config = None

    # Hot-swap model override from session state
    model_key = f"model:{callback_context.agent_name}"
    model_override = callback_context.state.to_dict().get(model_key)
    if model_override:
        from app.app_utils.models import _parse_model_str, get_model_string

        override_provider, override_model_name = _parse_model_str(model_override)
        # Determine the provider the agent was actually initialized with
        running_str = get_model_string(callback_context.agent_name)
        running_provider, _ = _parse_model_str(running_str)
        if override_provider == running_provider:
            # Same provider: safe to hot-swap via request model string
            llm_request.model = override_model_name
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
        # Prepend plan context as a system-level instruction
        llm_request.contents.insert(
            0,
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=context)],
            ),
        )

    return None


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
