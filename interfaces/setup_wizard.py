"""Ori Incubation Wizard — First-time setup that runs before the container starts.

Collects LLM provider config (mandatory), Telegram (optional), GitHub (optional),
and security credentials. Writes everything to data/.env.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# TOTP helpers (no external deps)
# ---------------------------------------------------------------------------

def _decode_secret(secret: str) -> bytes:
    secret = secret.strip().upper()
    padding = 8 - (len(secret) % 8)
    if padding != 8:
        secret += "=" * padding
    return base64.b32decode(secret)


def _generate_code(secret: str, time_step: int) -> str:
    key = _decode_secret(secret)
    msg = struct.pack(">Q", time_step)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False
    current_step = int(time.time()) // 30
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_generate_code(secret, current_step + offset), code):
            return True
    return False


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def cprint(text, color="0"):
    print(f"\033[{color}m{text}\033[0m")


def prompt(label, required=False, secret=False):
    """Prompt the user for input. Loops if required=True and input is empty."""
    while True:
        val = input(f"\033[92m{label}\033[0m ").strip()
        if val or not required:
            return val
        cprint("This field is required.", "91")


def confirm(label, default=False):
    suffix = "(Y/n)" if default else "(y/N)"
    val = input(f"\033[92m{label} {suffix}:\033[0m ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


# ---------------------------------------------------------------------------
# Provider setup flows
# ---------------------------------------------------------------------------

def _run_interactive(cmd, description):
    """Run a command interactively (inherits stdin/stdout for browser auth flows)."""
    print(f"\n  Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        cprint(f"  {description} exited with code {result.returncode}.", "91")
        return False
    return True


def setup_google_api_key(env_path, set_key_fn):
    """Collect a Google AI Studio API key."""
    cprint("  Auth method: API Key (Google AI Studio)", "96")
    print("  Get a free key at: https://aistudio.google.com/app/apikey")
    key = prompt("  Enter your GOOGLE_API_KEY:", required=True)
    set_key_fn(env_path, "GOOGLE_API_KEY", key)
    set_key_fn(env_path, "GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
    cprint("  Saved.\n", "92")
    return True


def setup_google_login(env_path, set_key_fn):
    """Authenticate with Google Cloud via browser login (ADC)."""
    cprint("  Auth method: Google Cloud Login (Application Default Credentials)", "96")
    print("  This opens a browser link for you to sign in with your Google account.")
    print("  Both Gemini and Claude models are available via Vertex AI.\n")

    # Check if gcloud is installed
    gcloud_check = subprocess.run(["which", "gcloud"], capture_output=True)
    if gcloud_check.returncode != 0:
        cprint("  Error: gcloud CLI not found. Install it from: https://cloud.google.com/sdk/docs/install", "91")
        print("  After installing, re-run this setup.\n")
        return False

    project = prompt("  Enter your GOOGLE_CLOUD_PROJECT:", required=True)
    location = prompt("  Enter your GOOGLE_CLOUD_LOCATION (default: us-central1):") or "us-central1"

    set_key_fn(env_path, "GOOGLE_CLOUD_PROJECT", project)
    set_key_fn(env_path, "GOOGLE_CLOUD_LOCATION", location)
    set_key_fn(env_path, "GOOGLE_GENAI_USE_VERTEXAI", "TRUE")

    # Check if already authenticated
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.exists(adc_path):
        cprint("  Existing credentials found.", "92")
        if not confirm("  Re-authenticate?"):
            cprint("  Keeping existing credentials.\n", "92")
            return True

    # Run gcloud auth interactively — user clicks the link, logs in
    print("\n  A browser link will appear. Click it to sign in with your Google account.")
    print("  If you're on a headless server, copy the URL to your local browser.\n")

    success = _run_interactive(
        ["gcloud", "auth", "application-default", "login",
         "--project", project, "--no-launch-browser"],
        "Google Cloud authentication",
    )

    if success and os.path.exists(adc_path):
        cprint("\n  Authenticated successfully.\n", "92")
        return True
    elif success:
        # gcloud succeeded but ADC file might be elsewhere
        cprint("\n  Login completed. If Vertex AI fails, run: gcloud auth application-default login\n", "93")
        return True
    else:
        cprint("\n  Authentication failed. You can retry or switch to API key mode.\n", "91")
        return False


def setup_anthropic_api_key(env_path, set_key_fn):
    """Collect an Anthropic API key."""
    cprint("  Auth method: API Key (Anthropic)", "96")
    print("  Get a key at: https://console.anthropic.com/settings/keys")
    key = prompt("  Enter your ANTHROPIC_API_KEY:", required=True)
    set_key_fn(env_path, "ANTHROPIC_API_KEY", key)
    cprint("  Saved.\n", "92")
    return True


def select_default_model(env_path, set_key_fn, providers):
    """Let user pick the default model for agents."""
    # Build model menu based on selected providers
    models = []
    if "google" in providers:
        models.extend([
            ("google/gemini-3-flash-preview", "Gemini 3 Flash (fast, free tier)"),
            ("google/gemini-3.1-pro-preview", "Gemini 3.1 Pro (most capable Google model)"),
        ])
    if "anthropic" in providers:
        models.extend([
            ("anthropic/claude-sonnet-4-6", "Claude Sonnet 4.6 (balanced)"),
            ("anthropic/claude-opus-4-6", "Claude Opus 4.6 (most capable)"),
            ("anthropic/claude-haiku-4-5-20251001", "Claude Haiku 4.5 (fast, cheap)"),
        ])

    cprint("  Select default model for agents:", "96")
    for i, (model_id, desc) in enumerate(models, 1):
        print(f"    {i}. {desc}  [{model_id}]")

    while True:
        choice = prompt(f"  Enter choice (1-{len(models)}):", required=True)
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            selected = models[int(choice) - 1][0]
            break
        cprint(f"  Please enter a number between 1 and {len(models)}.", "91")

    # Set the main agents to this model
    for component in ("CoordinatorAgent", "DeveloperAgent", "KnowledgeAgent"):
        set_key_fn(env_path, f"MODEL_{component.upper()}", selected)

    cprint(f"  Default model set to: {selected}\n", "92")
    return selected


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

def _set_key_stdlib(env_path, key, value):
    """Write a KEY=VALUE to .env without requiring python-dotenv."""
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f'{key}="{value}"\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'{key}="{value}"\n')
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ[key] = value


def _load_dotenv_stdlib(env_path):
    """Load .env into os.environ without requiring python-dotenv."""
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), val)


def main():
    ENV_FILE_PATH = os.path.abspath("./data/.env")
    os.makedirs(os.path.dirname(ENV_FILE_PATH), exist_ok=True)
    if not os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "w") as f:
            f.write("# Ori Daemon Configuration\n")

    # Use python-dotenv if available, fall back to stdlib for host-side execution
    try:
        from dotenv import set_key, load_dotenv
        load_dotenv(ENV_FILE_PATH)
    except ImportError:
        set_key = _set_key_stdlib
        _load_dotenv_stdlib(ENV_FILE_PATH)

    clear_screen()
    cprint(r"""
  ██████╗  ██████╗  ██╗
 ██╔═══██╗ ██╔══██╗ ██║
 ██║   ██║ ██████╔╝ ██║
 ██║   ██║ ██╔══██╗ ██║
 ╚██████╔╝ ██║  ██║ ██║
  ╚═════╝  ╚═╝  ╚═╝ ╚═╝
    """, "96")
    cprint(" Welcome to the Ori Incubation Wizard.\n", "96")

    # -----------------------------------------------------------------------
    # 1. Agent Name
    # -----------------------------------------------------------------------
    bot_name = os.environ.get("BOT_NAME", "").strip()
    if not bot_name:
        cprint("[1] Agent Name", "93")
        print("What would you like to call your autonomous agent?")
        bot_name = prompt("Enter a name (default: Ori):") or "Ori"
        set_key(ENV_FILE_PATH, "BOT_NAME", bot_name)
        cprint(f"  Hello, {bot_name}.\n", "92")

    # -----------------------------------------------------------------------
    # 2. LLM Provider (MANDATORY)
    # -----------------------------------------------------------------------
    # Check if provider is already configured
    has_google = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
    has_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    has_any_provider = has_google or has_vertex or has_anthropic

    if not has_any_provider:
        cprint("[2] LLM Provider Setup (Required)", "93")
        print("You must configure at least one AI provider for your agent to think.\n")
        cprint("  NOTE: A Google API key (option 1) is strongly recommended even if you", "93")
        cprint("  choose Claude as your primary model. It powers the embedding-based", "93")
        cprint("  security guardrails (prompt injection defense). Without it, security", "93")
        cprint("  features will be reduced. Use option 4 to combine providers (e.g. 1,3).\n", "93")

        print("  How would you like to authenticate?\n")
        print("    1. Google Gemini — paste an API key (free tier available)")
        print("    2. Google Cloud login — opens a link, you sign in (covers Gemini + Claude)")
        print("    3. Anthropic Claude — paste an API key")
        print("    4. Multiple — combine options (e.g. 1,3 for both API keys)")
        print()

        providers_configured = set()

        while not providers_configured:
            choice = prompt("  Select option(s) — comma-separated (e.g. 1,3):", required=True)
            choices = [c.strip() for c in choice.split(",")]

            for c in choices:
                if c == "1":
                    if setup_google_api_key(ENV_FILE_PATH, set_key):
                        providers_configured.add("google")
                elif c == "2":
                    if setup_google_login(ENV_FILE_PATH, set_key):
                        providers_configured.add("google")
                        providers_configured.add("anthropic")  # Vertex covers both
                elif c == "3":
                    if setup_anthropic_api_key(ENV_FILE_PATH, set_key):
                        providers_configured.add("anthropic")
                elif c == "4":
                    cprint("  Use comma-separated numbers (e.g. 1,3) to combine options.", "93")
                else:
                    cprint(f"  Unknown option: {c}", "91")

            if not providers_configured:
                cprint("  At least one provider is required.\n", "91")

        # Reload env after provider setup
        try:
            load_dotenv(ENV_FILE_PATH, override=True)
        except TypeError:
            # stdlib fallback doesn't support override param
            _load_dotenv_stdlib(ENV_FILE_PATH)

        # Model selection
        cprint("[2b] Default Model", "93")
        select_default_model(ENV_FILE_PATH, set_key, providers_configured)

    # -----------------------------------------------------------------------
    # 3. Telegram (Optional)
    # -----------------------------------------------------------------------
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tg_token:
        cprint("[3] Telegram Bot (Optional)", "93")
        print("Control your agent from your phone via Telegram.")
        print("Create a bot via @BotFather on Telegram to get a token.")
        print("If you skip this, the agent runs in local CLI mode.\n")
        tg_token = prompt("Enter your TELEGRAM_BOT_TOKEN (or press Enter to skip):")
        if tg_token:
            set_key(ENV_FILE_PATH, "TELEGRAM_BOT_TOKEN", tg_token)
            cprint("  Saved.\n", "92")
        else:
            cprint("  Skipped. Running in CLI mode.\n", "90")

    # 3b. Telegram admin verification
    if tg_token:
        admin_ids = os.environ.get("ADMIN_USER_IDS", "").strip()
        if not admin_ids:
            cprint("[3b] Secure Telegram Binding", "93")
            print("Link your Telegram account to secure the bot.")
            verification_code = secrets.token_hex(3).upper()
            print(f"Open your bot on Telegram and send this code: \033[96m{verification_code}\033[0m")
            print("Waiting for message... (Ctrl+C to skip)\n")

            try:
                offset = 0
                chat_id_found = None
                start_time = time.time()
                while not chat_id_found and (time.time() - start_time) < 300:
                    try:
                        url = f"https://api.telegram.org/bot{tg_token}/getUpdates?offset={offset}&timeout=5"
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req, timeout=10) as response:
                            data = json.loads(response.read().decode())
                            if data.get("ok"):
                                for result in data.get("result", []):
                                    offset = result["update_id"] + 1
                                    message = result.get("message", {})
                                    if message.get("text", "").strip() == verification_code:
                                        chat_id_found = str(message["from"]["id"])
                                        break
                        if chat_id_found:
                            break
                        time.sleep(1)
                    except Exception:
                        time.sleep(2)

                if chat_id_found:
                    tg_id = f"tg_{chat_id_found}"
                    set_key(ENV_FILE_PATH, "ADMIN_USER_IDS", tg_id)

                    whitelist_path = os.path.abspath("./data/whitelist.json")
                    whitelist = []
                    if os.path.exists(whitelist_path):
                        try:
                            with open(whitelist_path) as f:
                                whitelist = json.load(f)
                                if not isinstance(whitelist, list):
                                    whitelist = []
                        except Exception:
                            pass
                    if tg_id not in whitelist:
                        whitelist.append(tg_id)
                        with open(whitelist_path, "w") as f:
                            json.dump(whitelist, f, indent=2)

                    cprint(f"  Verified! {tg_id} saved as Admin.\n", "92")
                else:
                    cprint("  Timed out. Set ADMIN_USER_IDS manually in data/.env\n", "93")
            except KeyboardInterrupt:
                print()
                cprint("  Skipped. Set ADMIN_USER_IDS manually in data/.env\n", "90")

    # -----------------------------------------------------------------------
    # 4. GitHub Evolution Habitat (Optional)
    # -----------------------------------------------------------------------
    github_repo = os.environ.get("GITHUB_REPO", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_repo or not github_token:
        cprint("[4] GitHub Evolution Habitat (Recommended)", "93")
        print("For self-evolution, the agent needs a GitHub repo to push code to.")
        print("Without this, code changes won't persist across rebuilds.\n")
        print("  1. Create a private repo on GitHub")
        print("  2. Create a PAT with 'repo' scope at: https://github.com/settings/tokens\n")

        if confirm("  Configure GitHub now?"):
            github_repo = prompt("  Enter repo (e.g. username/my-bot):") or ""
            github_token = prompt("  Enter PAT (ghp_...):") or ""
            if github_repo and github_token:
                github_repo = github_repo.replace("https://github.com/", "").replace(".git", "")
                set_key(ENV_FILE_PATH, "GITHUB_REPO", github_repo)
                set_key(ENV_FILE_PATH, "GITHUB_TOKEN", github_token)
                cprint("  Saved.\n", "92")
            else:
                cprint("  Missing repo or token. Skipped.\n", "91")
        else:
            cprint("  Skipped.\n", "90")

    # -----------------------------------------------------------------------
    # 5. Security: Admin Passcode + A2A Key (auto-generated)
    # -----------------------------------------------------------------------
    admin_pass = os.environ.get("ADMIN_PASSCODE", "").strip()
    if not admin_pass:
        cprint("[5] Admin Security", "93")
        admin_pass = secrets.token_urlsafe(16)
        set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", admin_pass)
        print("  A secure Admin Passcode has been generated:")
        cprint(f"    {admin_pass}", "91")
        print("  SAVE THIS — you'll need it for critical system changes.")
        print("  It is also saved in data/.env.\n")
        input("  Press Enter to continue...")
        print()

    a2a_key = os.environ.get("A2A_API_KEY", "").strip()
    if not a2a_key:
        a2a_key = "ori-" + secrets.token_urlsafe(24)
        set_key(ENV_FILE_PATH, "A2A_API_KEY", a2a_key)

    # -----------------------------------------------------------------------
    # 6. TOTP 2FA (Optional)
    # -----------------------------------------------------------------------
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET", "").strip()
    if not totp_secret:
        cprint("[6] Two-Factor Authentication (Optional)", "93")
        print("  Require a 6-digit authenticator code for admin actions.\n")
        if confirm("  Enable TOTP 2FA?"):
            raw_secret = os.urandom(10)
            totp_secret = base64.b32encode(raw_secret).decode("utf-8").replace("=", "")

            print(f"\n  Your TOTP Secret Key: \033[92m{totp_secret}\033[0m\n")

            uri = f"otpauth://totp/{bot_name}:Admin?secret={totp_secret}&issuer={bot_name}"
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(uri)}"
            print(f"  Scan QR: \033[94m{qr_url}\033[0m\n")
            print("  Or enter the Secret Key manually into your authenticator.\n")

            while True:
                code = prompt("  Enter 6-digit code to verify:", required=True)
                if verify_totp(totp_secret, code):
                    set_key(ENV_FILE_PATH, "ADMIN_TOTP_SECRET", totp_secret)
                    cprint("  TOTP verified and enabled.\n", "92")
                    break
                else:
                    cprint("  Invalid code. Try again.", "91")
        else:
            cprint("  Skipped.\n", "90")

    # -----------------------------------------------------------------------
    # Done
    # -----------------------------------------------------------------------
    cprint(f"  Incubation complete! {bot_name} is waking up...\n", "92")
    time.sleep(1)


if __name__ == "__main__":
    main()
