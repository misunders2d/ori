import os
import sys
import secrets
import base64
import urllib.parse
import urllib.request
import urllib.error
import json
import time
import hashlib
import hmac
import struct

def _decode_secret(secret: str) -> bytes:
    """Decode a base32-encoded TOTP secret, tolerating missing padding."""
    secret = secret.strip().upper()
    padding = 8 - (len(secret) % 8)
    if padding != 8:
        secret += "=" * padding
    return base64.b32decode(secret)

def _generate_code(secret: str, time_step: int) -> str:
    """Generate a 6-digit TOTP code for a given time step."""
    key = _decode_secret(secret)
    msg = struct.pack(">Q", time_step)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)

def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Verify a TOTP code against a secret."""
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False

    current_step = int(time.time()) // 30
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_generate_code(secret, current_step + offset), code):
            return True
    return False

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def cprint(text, color="0"):
    print(f"\033[{color}m{text}\033[0m")

def main():
    ENV_FILE_PATH = os.path.abspath("./data/.env")
    os.makedirs(os.path.dirname(ENV_FILE_PATH), exist_ok=True)
    if not os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, "w") as f:
            f.write("# Ori Daemon Configuration\n")

    from dotenv import set_key, load_dotenv
    load_dotenv(ENV_FILE_PATH)

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

    # 1. Agent Name
    bot_name = os.environ.get("BOT_NAME", "").strip()
    if not bot_name:
        cprint("[1] Agent Name", "93")
        print("What would you like to call your autonomous agent?")
        bot_name = input("\033[92mEnter a name (default: Ori):\033[0m ").strip()
        if not bot_name:
            bot_name = "Ori"
        set_key(ENV_FILE_PATH, "BOT_NAME", bot_name)
        cprint(f"✅ Hello, {bot_name}.\n", "92")

    # 2. Google API Key
    google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not google_key:
        cprint("[2] Google AI Studio API Key", "93")
        print("Ori requires a Gemini API key to think and evolve.")
        print("You can get a free one here: https://aistudio.google.com/app/apikey")
        while not google_key:
            google_key = input("\033[92mEnter your GOOGLE_API_KEY:\033[0m ").strip()
        set_key(ENV_FILE_PATH, "GOOGLE_API_KEY", google_key)
        cprint("✅ Saved.\n", "92")

    # 3. Telegram Token
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tg_token:
        cprint("[3] Telegram Bot Token (Optional)", "93")
        print("To control your agent from your phone, create a bot via @BotFather on Telegram.")
        print("If you skip this, the agent will run in local CLI mode.")
        tg_token = input("\033[92mEnter your TELEGRAM_BOT_TOKEN (or press Enter to skip):\033[0m ").strip()
        if tg_token:
            set_key(ENV_FILE_PATH, "TELEGRAM_BOT_TOKEN", tg_token)
            cprint("✅ Saved.\n", "92")
        else:
            cprint("⏭️  Skipped. Running in CLI mode.\n", "90")

    # 3.5 Admin User ID (Telegram Verification)
    if tg_token:
        admin_ids = os.environ.get("ADMIN_USER_IDS", "").strip()
        if not admin_ids:
            cprint("[3.5] Secure Telegram Binding", "93")
            print("To secure your bot, we need to link it to your Telegram account.")
            verification_code = secrets.token_hex(3).upper()
            print(f"Please open your bot on Telegram and send this exact code: \033[96m{verification_code}\033[0m")
            print("Waiting for message... (Press Ctrl+C to skip)")
            
            try:
                offset = 0
                chat_id_found = None
                # Polling loop with safety timeout for non-interactive environments
                start_time = time.time()
                while not chat_id_found and (time.time() - start_time) < 300: # 5 min timeout
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
                        if chat_id_found: break
                        time.sleep(1)
                    except Exception:
                        time.sleep(2)
                        continue
                        
                if chat_id_found:
                    set_key(ENV_FILE_PATH, "ADMIN_USER_IDS", chat_id_found)
                    cprint(f"✅ Verified! Chat ID {chat_id_found} saved as Admin.\n", "92")
                else:
                    cprint("⚠️  Verification timed out. Set ADMIN_USER_IDS manually in data/.env\n", "93")
                    
            except KeyboardInterrupt:
                print("\n")
                cprint("⏭️  Skipped Telegram verification. Remember to set ADMIN_USER_IDS manually in data/.env\n", "90")

    # 4. GitHub Evolution Habitat
    github_repo = os.environ.get("GITHUB_REPO", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_repo or not github_token:
        cprint("[4] GitHub Evolution Habitat (Highly Recommended)", "93")
        print("To truly self-evolve, the agent needs a GitHub repository to push its code changes to.")
        print("Without this, it cannot write permanent updates to its own source code.")
        print("\nHow to set this up:")
        print("  1. Create a new, empty private repository on GitHub.")
        print("  2. Create a Personal Access Token (Classic) with 'repo' scope at: https://github.com/settings/tokens")
        
        setup_github = input("\n\033[92mConfigure GitHub now? (y/N):\033[0m ").strip().lower()
        if setup_github == 'y':
            github_repo = input("\033[92mEnter your GitHub Repo (e.g., username/my-bot):\033[0m ").strip()
            github_token = input("\033[92mEnter your GitHub PAT (ghp_...):\033[0m ").strip()
            
            if github_repo and github_token:
                github_repo = github_repo.replace("https://github.com/", "").replace(".git", "")
                set_key(ENV_FILE_PATH, "GITHUB_REPO", github_repo)
                set_key(ENV_FILE_PATH, "GITHUB_TOKEN", github_token)
                cprint("✅ Habitat Saved.\n", "92")
            else:
                cprint("❌ Missing repo or token. Skipped GitHub setup.\n", "91")
        else:
            cprint("⏭️  Skipped. You can configure this later via /init or editing data/.env.\n", "90")

    # 5. Admin Passcode
    admin_pass = os.environ.get("ADMIN_PASSCODE", "").strip()
    if not admin_pass:
        cprint("[5] Admin Security", "93")
        admin_pass = secrets.token_urlsafe(16)
        set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", admin_pass)
        print("A secure Admin Passcode has been generated for you:")
        cprint(f"  {admin_pass}", "91")
        print("⚠️  SAVE THIS! You will need it to authorize critical system changes.")
        print("It is also saved in your data/.env file.\n")
        input("Press Enter to continue...")
        print("")

    # 6. A2A Network Key
    a2a_key = os.environ.get("A2A_API_KEY", "").strip()
    if not a2a_key:
        a2a_key = "ori-" + secrets.token_urlsafe(24)
        set_key(ENV_FILE_PATH, "A2A_API_KEY", a2a_key)

    # 7. TOTP 2FA
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET", "").strip()
    if not totp_secret:
        cprint("[6] Two-Factor Authentication (TOTP)", "93")
        print("For maximum security during evolution, you can require a 6-digit")
        print("authenticator code (Google Auth, Authy, etc.) for admin actions.")
        enable_totp = input("\033[92mEnable TOTP 2FA? (y/N):\033[0m ").strip().lower()
        if enable_totp == 'y':
            raw_secret = os.urandom(10)
            totp_secret = base64.b32encode(raw_secret).decode('utf-8').replace('=', '')
            
            print("\nYour TOTP Secret Key is:")
            cprint(f"  {totp_secret}\n", "92")
            
            uri = f"otpauth://totp/{bot_name}:Admin?secret={totp_secret}&issuer={bot_name}"
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(uri)}"
            print(f"Scan this QR Code URL in your browser: \n\033[94m{qr_url}\033[0m\n")
            print("Or enter the Secret Key manually into your authenticator app.\n")
            
            while True:
                code = input("\033[92mEnter the 6-digit code from your app to verify:\033[0m ").strip()
                if verify_totp(totp_secret, code):
                    set_key(ENV_FILE_PATH, "ADMIN_TOTP_SECRET", totp_secret)
                    cprint("✅ TOTP Verified and Enabled!\n", "92")
                    break
                else:
                    cprint("❌ Invalid code. Try again.", "91")
        else:
            cprint("⏭️  Skipped TOTP.\n", "90")

    cprint(f"🎉 Incubation Complete! {bot_name} is waking up...", "92")
    time.sleep(1)

if __name__ == "__main__":
    main()
