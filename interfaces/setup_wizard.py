import os
import sys
import secrets
import base64
import urllib.parse
import time

# Ensure we can import from app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.app_utils.totp import verify_totp

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

    # 1. Google API Key
    google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not google_key:
        cprint("[1] Google AI Studio API Key", "93")
        print("Ori requires a Gemini API key to think and evolve.")
        print("You can get a free one here: https://aistudio.google.com/app/apikey")
        while not google_key:
            google_key = input("\033[92mEnter your GOOGLE_API_KEY:\033[0m ").strip()
        set_key(ENV_FILE_PATH, "GOOGLE_API_KEY", google_key)
        cprint("✅ Saved.\n", "92")

    # 2. Telegram Token
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not tg_token:
        cprint("[2] Telegram Bot Token (Optional)", "93")
        print("To control Ori from your phone, create a bot via @BotFather on Telegram.")
        print("If you skip this, Ori will run in local CLI mode.")
        tg_token = input("\033[92mEnter your TELEGRAM_BOT_TOKEN (or press Enter to skip):\033[0m ").strip()
        if tg_token:
            set_key(ENV_FILE_PATH, "TELEGRAM_BOT_TOKEN", tg_token)
            cprint("✅ Saved.\n", "92")
        else:
            cprint("⏭️  Skipped. Ori will run in CLI mode.\n", "90")

    # 3. Admin Passcode
    admin_pass = os.environ.get("ADMIN_PASSCODE", "").strip()
    if not admin_pass:
        cprint("[3] Admin Security", "93")
        admin_pass = secrets.token_urlsafe(16)
        set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", admin_pass)
        print("A secure Admin Passcode has been generated for you:")
        cprint(f"  {admin_pass}", "91")
        print("⚠️  SAVE THIS! You will need it to authorize critical system changes.")
        print("It is also saved in your data/.env file.\n")
        input("Press Enter to continue...")
        print("")

    # 4. A2A Network Key
    a2a_key = os.environ.get("A2A_API_KEY", "").strip()
    if not a2a_key:
        a2a_key = "ori-" + secrets.token_urlsafe(24)
        set_key(ENV_FILE_PATH, "A2A_API_KEY", a2a_key)

    # 5. TOTP 2FA
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET", "").strip()
    if not totp_secret:
        cprint("[4] Two-Factor Authentication (TOTP)", "93")
        print("For maximum security during evolution, you can require a 6-digit")
        print("authenticator code (Google Auth, Authy, etc.) for admin actions.")
        enable_totp = input("\033[92mEnable TOTP 2FA? (y/N):\033[0m ").strip().lower()
        if enable_totp == 'y':
            raw_secret = os.urandom(10)
            totp_secret = base64.b32encode(raw_secret).decode('utf-8').replace('=', '')
            
            print("\nYour TOTP Secret Key is:")
            cprint(f"  {totp_secret}\n", "92")
            
            uri = f"otpauth://totp/Ori:Admin?secret={totp_secret}&issuer=Ori"
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

    cprint("🎉 Incubation Complete! Ori is waking up...", "92")
    time.sleep(1)

if __name__ == "__main__":
    main()