import hmac
import logging
import os
import shlex

from deploy.vault import set as vault_set

logger = logging.getLogger(__name__)

ALLOWED_CONFIG_KEYS = frozenset({
    # Core LLM providers
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    # Google Cloud / Vertex AI
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_APPLICATION_CREDENTIALS",
    # Runtime / lifecycle
    "AGENT_RPM",
    "ADMIN_USER_IDS",
    "BOT_NAME",
    "APP_NAME",
    "REQUIRE_2FA",
    "COMPANY_DOMAIN",
    # Telegram
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    # GitHub
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    # Google OAuth (per-user Drive/Gmail/Calendar token store) — legacy
    # names that app/tools/google_oauth/web_flow.py reads directly.
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "OAUTH_BASE_URL",
    # Amazon SP-API (LWA OAuth) — legacy names that
    # app/tools/sp_api_tools.py and sp_api_export.py read.
    "SP_API_CLIENT_ID",
    "SP_API_CLIENT_SECRET",
    "SP_API_REFRESH_TOKEN",
    "SP_API_SELLER_ID",
    # --- amazon_manager domain integrations (flat tokens) ---
    "KEEPA_API_KEY",
    "H10_API_KEY",
    "YOUTUBE_API_KEY",
    "BQ_GCP_SERVICE_ACCOUNT_INFO",
    "CLICKUP_API_TOKEN",
    "CLICKUP_TEAM_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    # OAuth integrations subsystem (multi-tenant flows for ClickUp/Slack
    # via configure_integration). Distinct from the per-user Google OAuth
    # token store above. Provider-prefixed convention matches GOOGLE_OAUTH_*
    # so the integrations subsystem and legacy per-user OAuth share keys.
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "GITHUB_OAUTH_REDIRECT_URI",
    "GOOGLE_OAUTH_REDIRECT_URI",
    "CLICKUP_OAUTH_CLIENT_ID",
    "CLICKUP_OAUTH_CLIENT_SECRET",
    "CLICKUP_OAUTH_REDIRECT_URI",
    "SLACK_OAUTH_CLIENT_ID",
    "SLACK_OAUTH_CLIENT_SECRET",
    "SLACK_OAUTH_REDIRECT_URI",
})

# Keys the agent can set via configure_integration (conversational flow).
# ADMIN_USER_IDS and REQUIRE_2FA are excluded — they must only be set via /init (requires passcode).
AGENT_CONFIG_KEYS = frozenset({
    # Core LLM providers
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    # Google Cloud / Vertex AI
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_GENAI_USE_VERTEXAI",
    # Identity
    "BOT_NAME",
    "APP_NAME",
    # Telegram / GitHub
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    # Slack
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    # Google OAuth (per-user token store)
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "OAUTH_BASE_URL",
    # Amazon SP-API
    "SP_API_CLIENT_ID",
    "SP_API_CLIENT_SECRET",
    "SP_API_REFRESH_TOKEN",
    "SP_API_SELLER_ID",
    # Amazon domain APIs
    "KEEPA_API_KEY",
    "H10_API_KEY",
    "YOUTUBE_API_KEY",
    # BigQuery / Neo4j / ClickUp
    "BQ_GCP_SERVICE_ACCOUNT_INFO",
    "CLICKUP_API_TOKEN",
    "CLICKUP_TEAM_ID",
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
})


# ---------------------------------------------------------------------------
# Pending TOTP verification state
# Maps session_id -> {"command_text": str, "attempts": int}
# ---------------------------------------------------------------------------
_pending_totp: dict[str, dict] = {}

_MAX_TOTP_ATTEMPTS = 3


def totp_enabled() -> bool:
    """Check if TOTP 2FA is configured."""
    return bool(os.environ.get("ADMIN_TOTP_SECRET", "").strip())


def has_pending_totp(session_id: str) -> bool:
    """Check if a session has a pending TOTP verification."""
    return session_id in _pending_totp


def verify_pending_totp(session_id: str, code: str) -> tuple[bool, str]:
    """
    Verify a TOTP code for a pending /init command.

    Returns (success, message). On success, the pending config update is applied.
    On failure, the pending state is preserved for retry (up to max attempts).
    """
    from app.util.totp import verify_totp

    pending = _pending_totp.get(session_id)
    if not pending:
        return False, "No pending verification for this session."

    secret = os.environ.get("ADMIN_TOTP_SECRET", "")
    if not secret:
        # TOTP was disabled between init and verification — apply directly
        command_text = _pending_totp.pop(session_id)["command_text"]
        return True, _apply_config(command_text)

    if verify_totp(secret, code):
        command_text = _pending_totp.pop(session_id)["command_text"]
        result = _apply_config(command_text)
        return True, result
    else:
        pending["attempts"] += 1
        if pending["attempts"] >= _MAX_TOTP_ATTEMPTS:
            _pending_totp.pop(session_id, None)
            logger.warning("TOTP verification failed %d times for session %s — init cancelled.", _MAX_TOTP_ATTEMPTS, session_id)
            return False, f"Verification failed {_MAX_TOTP_ATTEMPTS} times. /init cancelled for security. Try again."

        remaining = _MAX_TOTP_ATTEMPTS - pending["attempts"]
        return False, f"Invalid code. {remaining} attempt(s) remaining. Send your 6-digit authenticator code."


def update_config(command_text: str, admin_passcode: str | None = None, session_id: str = "") -> str:
    """
    Parses a string in the format '/init PASSCODE KEY=VALUE KEY2=VALUE'
    and updates the .env file and current environment.

    If TOTP is enabled, the config update is held pending until the user
    provides a valid authenticator code. Returns a prompt for the code.
    """
    body = command_text.replace("/init", "", 1).strip()
    if not body:
        return "Usage: /init <PASSCODE> KEY=VALUE [KEY2=VALUE ...]"

    try:
        parts = shlex.split(body)
    except ValueError as e:
        return f"Error parsing command: {e}"

    if not parts:
        return "Usage: /init <PASSCODE> KEY=VALUE [KEY2=VALUE ...]"

    # First argument must be the admin passcode
    provided_passcode = parts[0]
    if not admin_passcode or not hmac.compare_digest(provided_passcode, admin_passcode):
        return "Authentication failed. Usage: /init <PASSCODE> KEY=VALUE"

    kv_parts = parts[1:]
    if not kv_parts:
        return "No KEY=VALUE pairs provided after passcode."

    # Validate that there's at least one valid KEY=VALUE before prompting for TOTP
    has_valid_kv = any("=" in p for p in kv_parts)
    if not has_valid_kv:
        return "No valid KEY=VALUE pairs found."

    # If TOTP is enabled, hold the update and request verification
    if totp_enabled() and session_id:
        _pending_totp[session_id] = {
            "command_text": command_text,
            "attempts": 0,
        }
        return (
            "Passcode accepted. Two-factor authentication is enabled.\n\n"
            "Send your 6-digit authenticator code to complete the update."
        )

    # No TOTP — apply immediately
    return _apply_config(command_text)


def _apply_config(command_text: str) -> str:
    """Apply the KEY=VALUE pairs from a validated /init command to .env and environment."""
    body = command_text.replace("/init", "", 1).strip()
    parts = shlex.split(body)
    kv_parts = parts[1:]  # Skip the passcode

    updated_keys = []
    rejected_keys = []

    for part in kv_parts:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().upper()

            if key not in ALLOWED_CONFIG_KEYS:
                clean_key = "".join(key.split())
                if clean_key in ALLOWED_CONFIG_KEYS:
                    key = clean_key
                else:
                    logger.warning(f"Config: Rejected unauthorized key '{key}'")
                    rejected_keys.append(key)
                    continue

            vault_set(key, value)
            updated_keys.append(key)

    msgs = []
    if updated_keys:
        msgs.append(f"Configuration updated for: {', '.join(updated_keys)}")
    if rejected_keys:
        msgs.append(f"Rejected unknown keys: {', '.join(rejected_keys)}")
    if not updated_keys and not rejected_keys:
        msgs.append("No valid KEY=VALUE pairs found.")

    return " | ".join(msgs)
