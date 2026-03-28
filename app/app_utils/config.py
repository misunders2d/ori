import hmac
import os
import shlex

from dotenv import set_key

ENV_FILE_PATH = os.environ.get("DOTENV_PATH", "./data/.env")

ALLOWED_CONFIG_KEYS = frozenset({
    "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "ADMIN_USER_IDS",
    "BOT_NAME",
})


def update_config(command_text: str, admin_passcode: str | None = None) -> str:
    """
    Parses a string in the format '/init PASSCODE KEY=VALUE KEY2=VALUE'
    and updates the .env file and current environment.
    Requires the admin passcode as the first argument and only allows known config keys.
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

    os.makedirs(os.path.dirname(os.path.abspath(ENV_FILE_PATH)), exist_ok=True)
    if not os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "w") as f:
            f.write("# Autonomous Ori Daemon Configuration\n")

    updated_keys = []
    rejected_keys = []

    for part in kv_parts:
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().upper()
            if key not in ALLOWED_CONFIG_KEYS:
                rejected_keys.append(key)
                continue
            set_key(ENV_FILE_PATH, key, value)
            os.environ[key] = value
            updated_keys.append(key)

    msgs = []
    if updated_keys:
        msgs.append(f"Configuration updated for: {', '.join(updated_keys)}")
    if rejected_keys:
        msgs.append(f"Rejected unknown keys: {', '.join(rejected_keys)}")
    if not updated_keys and not rejected_keys:
        msgs.append("No valid KEY=VALUE pairs found.")

    return " | ".join(msgs)
