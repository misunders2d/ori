import json
import logging
import math
import re

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

logger = logging.getLogger(__name__)


def intent_security_guardrail(*args, **kwargs) -> dict | None:
    """
    Runtime Guardrail: Intercepts tool calls before execution.
    Specifically checks SP-API updates for dangerous price drops or disallowed keywords.
    """
    tool_call = kwargs.get("tool_call")
    if not tool_call and len(args) >= 3:
        tool_call = args[2]

    if tool_call and tool_call.name == "sp_api_update_listing":
        args_payload = tool_call.args
        if args_payload.get("price", 0) < 5.0:
            return {
                "status": "error",
                "message": "Guardrail blocked: Proposed price is dangerously low (below $5.0).",
            }

    return None


def admin_tool_guardrail(*args, **kwargs) -> dict | None:
    """
    Runtime Guardrail: Intercepts highly privileged tool calls before execution.
    Specifically checks if the invoking user is an admin before allowing tools that manage API keys.
    """
    tool_call = kwargs.get("tool_call")
    if not tool_call and len(args) >= 3:
        tool_call = args[2]

    callback_context = kwargs.get("callback_context")
    if not callback_context and len(args) >= 2:
        callback_context = args[1]

    if not tool_call or not callback_context:
        return None

    if tool_call.name in ["configure_integration", "remove_integration", "schedule_system_task", "schedule_recurring_system_task"]:
        import os

        current_state = callback_context.state.to_dict()
        user_id = current_state.get("user_id", "")

        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
        admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

        if not admin_users or user_id not in admin_users:
            return {
                "status": "error",
                "message": f"Guardrail Intervention: Only Admin/Master users can invoke `{tool_call.name}` to manage API integrations and environment variables. Your user_id ({user_id}) is unauthorized.",
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


def prompt_injection_guardrail(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """
    Runtime Guardrail: Inspects the LLM request before hitting the model.
    Dynamically tracks Prompt Injection across any language using Semantic Cosine Similarity dot products against known anchor spaces (vector clusters).
    Dynamically strips the BuiltInPlanner's thinking_config from the payload if use_planner was explicitly set to False in the DB session.
    """
    use_planner = callback_context.state.to_dict().get("use_planner", False)
    if not use_planner and getattr(llm_request, "config", None):
        if hasattr(llm_request.config, "thinking_config"):
            llm_request.config.thinking_config = None

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
                if vectors:
                    import os

                    from google.genai import Client

                    client = Client(api_key=os.environ.get("GOOGLE_API_KEY"))
                    try:
                        emb_response = client.models.embed_content(
                            model="gemini-embedding-001", contents=[text_to_check]
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


def admin_only_guardrail(
    callback_context: CallbackContext
) -> types.Content | None:
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

    if user_id not in admin_users:
        return types.Content(
            parts=[
                types.Part(
                    text=f"Guardrail Intervention: Only Admin/Master users can invoke this agent. Your user_id (`{user_id}`) is unauthorized."
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

    import os
    from google.genai import Client

    # Extract a ~300-char window around the match
    start = max(0, match.start() - 100)
    end = min(len(content), match.end() + 200)
    fragment = content[start:end]

    try:
        client = Client(api_key=os.environ.get("GOOGLE_API_KEY"))
        emb_response = client.models.embed_content(
            model="gemini-embedding-001", contents=[fragment]
        )
        if not emb_response or not emb_response.embeddings:
            return None
        user_vector = emb_response.embeddings[0].values

        for v in vectors:
            sim = _cosine_similarity(user_vector, v)
            if sim >= _INDIRECT_THRESHOLD:
                logger.warning(
                    "Indirect prompt injection blocked in %s output (similarity: %.2f)",
                    tool_name, sim,
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
        logger.exception("Error in tool output injection check for %s", tool_name)

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
            current_count, _MAX_VERIFY_FAILURES,
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


async def state_setter(
    callback_context: CallbackContext, **kwargs
) -> types.Content | None:
    """
    Sets initial fundamental session state keys to prevent KeyErrors during prompt evaluation.
    """
    import os

    current_state = callback_context.state.to_dict()
    current_user = callback_context.user_id

    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    if "master_user_id" not in current_state:
        callback_context.state["master_user_id"] = admin_users
    if "user_id" not in current_state:
        callback_context.state["user_id"] = current_user

    # Load bot name from env (defaults to "Ori")
    callback_context.state["bot_name"] = os.environ.get("BOT_NAME", "Ori")

    # Load user preferences from disk into session state
    from app.tools.preferences import load_user_preferences

    effective_user = current_state.get("user_id", current_user)
    prefs = load_user_preferences(effective_user)
    callback_context.state["user_preferences"] = prefs

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
    
    _A2A_RISK_TOOLS = {"call_friend", "export_dna", "add_friend", "web_fetch"}
    if tool_name not in _A2A_RISK_TOOLS:
        return None

    # Load all current secrets
    secrets = []
    for key in ALLOWED_CONFIG_KEYS:
        val = os.environ.get(key)
        # We only match secrets that are long enough to be unique/dangerous (e.g., > 6 chars)
        if val and len(str(val)) > 6:
            secrets.append(str(val))
    
    # Also catch the admin passcode
    passcode = os.environ.get("ADMIN_PASSCODE")
    if passcode and len(str(passcode)) > 6:
        secrets.append(str(passcode))

    # 1. Check Arguments (Preventing leak via query/URL)
    args_json = json.dumps(args)
    for secret in secrets:
        if secret in args_json:
            logger.error("A2A PRIVACY VIOLATION: Secret detected in arguments for %s", tool_name)
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
                logger.error("A2A PRIVACY VIOLATION: Secret detected in output of %s", tool_name)
                return {
                    "status": "error",
                    "message": (
                        f"Guardrail Intervention: Technical DNA from `{tool_name}` was blocked. "
                        f"A system secret was found in the generated package. DNA exchange cancelled."
                    ),
                }

    return None
