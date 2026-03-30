#!/bin/bash
# Ori Daemon — Setup & Launch
# Usage:
#   chmod +x start.sh
#   ./start.sh [--no-sync]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Colors & formatting
# ---------------------------------------------------------------------------
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
MAGENTA='\033[0;35m'
BG_CYAN='\033[46m'
BG_RED='\033[41m'

hr()     { echo -e "${DIM}$(printf '%.0s─' {1..56})${RESET}"; }
ok()     { echo -e "  ${GREEN}✓${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET} $1"; }
fail()   { echo -e "  ${RED}✗${RESET} $1"; }
info()   { echo -e "  ${CYAN}→${RESET} $1"; }
prompt() { echo -en "  ${WHITE}$1${RESET}"; }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}"
cat << 'BANNER'
         ██████╗ ██████╗ ██╗
        ██╔═══██╗██╔══██╗██║
        ██║   ██║██████╔╝██║
        ██║   ██║██╔══██╗██║
        ╚██████╔╝██║  ██║██║
         ╚═════╝ ╚═╝  ╚═╝╚═╝
BANNER
echo -e "     Autonomous Self-Evolving Agent${RESET}"
echo ""
hr

# ---------------------------------------------------------------------------
# Disclaimer
# ---------------------------------------------------------------------------
echo ""
echo -e "  ${YELLOW}${BOLD}DISCLAIMER${RESET}"
echo ""
echo -e "  ${DIM}Ori is an autonomous agent that can modify its own code,${RESET}"
echo -e "  ${DIM}access external APIs, and manage scheduled tasks.${RESET}"
echo ""
echo -e "  ${DIM}This system is designed for users with technical knowledge —${RESET}"
echo -e "  ${DIM}people who understand not to commit credentials to git, who${RESET}"
echo -e "  ${DIM}can read logs, and who know their way around a terminal.${RESET}"
echo ""
echo -e "  ${DIM}That said, anyone is welcome to try it out.${RESET}"
echo ""
echo -e "  ${DIM}The author(s) bear no liability for data loss, unexpected${RESET}"
echo -e "  ${DIM}behavior, or costs incurred through API usage. Every effort${RESET}"
echo -e "  ${DIM}is made to harden security and protect your privacy, but${RESET}"
echo -e "  ${DIM}this software is provided as-is, without warranty.${RESET}"
echo ""
echo -e "  ${DIM}By continuing, you accept these terms.${RESET}"
echo ""
hr
echo ""

prompt "Press Enter to continue (or Ctrl+C to abort)... "
read -r
echo ""

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
echo -e "  ${BOLD}Checking prerequisites${RESET}"
echo ""

prereq_ok=true
for cmd in docker git python3; do
    if command -v "$cmd" &> /dev/null; then
        ok "$cmd"
    else
        fail "$cmd — not found. Please install it."
        prereq_ok=false
    fi
done

if docker compose version &> /dev/null; then
    ok "docker compose v2"
else
    fail "docker compose v2 — not available"
    prereq_ok=false
fi

if docker info &> /dev/null; then
    ok "docker daemon running"
else
    fail "docker daemon — not running or insufficient permissions"
    prereq_ok=false
fi

echo ""
if [ "$prereq_ok" = false ]; then
    fail "Missing prerequisites. Please install the above and retry."
    exit 1
fi
hr

# ---------------------------------------------------------------------------
# Git sync
# ---------------------------------------------------------------------------
echo ""
if [[ "$1" == "--no-sync" ]]; then
    info "Skipping remote git sync (--no-sync)"
elif ! git remote get-url origin &>/dev/null; then
    info "No remote 'origin' configured — skipping sync"
    warn "Add a remote later: git remote add origin <url>"
else
    info "Fetching and syncing origin/master..."
    if git fetch origin master 2>&1; then
        git reset --hard origin/master 2>&1
        ok "Codebase synced"
    else
        warn "Could not fetch from origin — continuing with local code"
    fi
fi
chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh" 2>/dev/null || true
echo ""
hr

# ---------------------------------------------------------------------------
# Prepare data directory
# ---------------------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/data"
chmod -R 777 "$SCRIPT_DIR/data"

# ---------------------------------------------------------------------------
# Helper: read a value from .env (handles KEY="value" and KEY=value)
# ---------------------------------------------------------------------------
env_get() {
    local key="$1" file="$2"
    if [ -f "$file" ]; then
        grep -E "^${key}=" "$file" 2>/dev/null | head -1 | sed -E 's/^[^=]+=//' | tr -d '"'
    fi
}

# ---------------------------------------------------------------------------
# First-time setup wizard
# ---------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/data/.env"

# Determine if this is a fresh install or existing config
if [ -f "$ENV_FILE" ]; then
    existing_env=true
else
    existing_env=false
    touch "$ENV_FILE"
fi

echo ""
echo -e "  ${CYAN}${BOLD}Configuration${RESET}"
echo ""

# --- Google API Key (required) ---
existing_google_key=$(env_get "GOOGLE_API_KEY" "$ENV_FILE")
if [ -z "$existing_google_key" ]; then
    echo -e "  ${WHITE}Google API Key${RESET} ${RED}(required)${RESET}"
    echo -e "  ${DIM}Get one at https://aistudio.google.com/apikey${RESET}"
    prompt "  GOOGLE_API_KEY: "
    read -r google_key
    if [ -n "$google_key" ]; then
        echo "GOOGLE_API_KEY=\"$google_key\"" >> "$ENV_FILE"
        ok "Saved"
    else
        warn "Skipped — Ori cannot function without this key."
        warn "Add it later to data/.env"
    fi
    echo ""
else
    ok "GOOGLE_API_KEY already configured"
fi

# --- Bot Name (optional, default Ori) ---
existing_bot_name=$(env_get "BOT_NAME" "$ENV_FILE")
if [ -z "$existing_bot_name" ]; then
    echo ""
    echo -e "  ${WHITE}Bot Name${RESET} ${DIM}(default: Ori)${RESET}"
    echo -e "  ${DIM}Give your agent a unique identity.${RESET}"
    prompt "  Name: "
    read -r bot_name
    bot_name="${bot_name:-Ori}"
    echo "BOT_NAME=\"$bot_name\"" >> "$ENV_FILE"
    ok "Named: ${BOLD}$bot_name${RESET}"
    echo ""
else
    ok "BOT_NAME already set: ${BOLD}$existing_bot_name${RESET}"
    bot_name="$existing_bot_name"
fi

# --- GitHub Repo (optional, recommended) ---
existing_github_repo=$(env_get "GITHUB_REPO" "$ENV_FILE")
if [ -z "$existing_github_repo" ]; then
    echo ""
    echo -e "  ${WHITE}GitHub Repository${RESET} ${YELLOW}(recommended)${RESET}"
    echo -e "  ${DIM}Enables self-evolution commits and version control.${RESET}"
    echo -e "  ${DIM}Format: owner/repo (e.g., yourname/ori-instance)${RESET}"
    prompt "  GITHUB_REPO: "
    read -r github_repo
    if [ -n "$github_repo" ]; then
        echo "GITHUB_REPO=\"$github_repo\"" >> "$ENV_FILE"
        ok "Saved: $github_repo"
    else
        info "Skipped — you can add this later via data/.env"
    fi
    echo ""
else
    ok "GITHUB_REPO already set: $existing_github_repo"
fi

# --- Telegram Bot Token (optional, recommended) ---
existing_tg_token=$(env_get "TELEGRAM_BOT_TOKEN" "$ENV_FILE")
if [ -z "$existing_tg_token" ]; then
    echo ""
    echo -e "  ${WHITE}Telegram Bot Token${RESET} ${YELLOW}(recommended)${RESET}"
    echo -e "  ${DIM}Primary way to interact with your agent.${RESET}"
    echo -e "  ${DIM}Create a bot via @BotFather on Telegram.${RESET}"
    prompt "  TELEGRAM_BOT_TOKEN: "
    read -r tg_token
    if [ -n "$tg_token" ]; then
        echo "TELEGRAM_BOT_TOKEN=\"$tg_token\"" >> "$ENV_FILE"
        ok "Saved"
    else
        info "Skipped — Ori will start in CLI-only mode"
    fi
    echo ""
else
    ok "TELEGRAM_BOT_TOKEN already configured"
fi

# --- TOTP (optional) ---
existing_totp=$(env_get "ADMIN_TOTP_SECRET" "$ENV_FILE")
if [ -z "$existing_totp" ]; then
    echo ""
    echo -e "  ${WHITE}Two-Factor Authentication${RESET} ${DIM}(optional)${RESET}"
    echo -e "  ${DIM}Add TOTP (Google Authenticator, Authy, etc.) for admin actions.${RESET}"
    prompt "  Enable 2FA? [y/N]: "
    read -r enable_totp
    if [[ "$enable_totp" =~ ^[Yy] ]]; then
        # Generate a 20-byte TOTP secret (base32 encoded)
        totp_secret=$(python3 -c "import secrets, base64; print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip('='))")
        totp_issuer="${bot_name:-Ori}"
        totp_uri="otpauth://totp/${totp_issuer}:admin?secret=${totp_secret}&issuer=${totp_issuer}&digits=6&period=30"

        echo "ADMIN_TOTP_SECRET=\"$totp_secret\"" >> "$ENV_FILE"
        echo ""
        ok "TOTP secret generated"
        echo ""

        # Try to display QR code in terminal
        qr_displayed=false
        if command -v qrencode &> /dev/null; then
            echo -e "  ${DIM}Scan this QR code with your authenticator app:${RESET}"
            echo ""
            qrencode -t ANSIUTF8 -m 2 "$totp_uri"
            qr_displayed=true
        else
            # Try Python qrcode module
            python3 -c "
import sys
try:
    import qrcode
    qr = qrcode.QRCode(version=1, box_size=1, border=2)
    qr.add_data(sys.argv[1])
    qr.make(fit=True)
    qr.print_ascii(invert=True)
except ImportError:
    sys.exit(1)
" "$totp_uri" 2>/dev/null && qr_displayed=true
        fi

        if [ "$qr_displayed" = false ]; then
            echo -e "  ${YELLOW}Could not display QR code (install qrencode for next time).${RESET}"
            echo -e "  ${DIM}Manually add this to your authenticator app:${RESET}"
        else
            echo ""
            echo -e "  ${DIM}Or enter manually:${RESET}"
        fi

        echo ""
        echo -e "  ${WHITE}Account:${RESET}  ${totp_issuer}:admin"
        echo -e "  ${WHITE}Secret:${RESET}   ${GREEN}${totp_secret}${RESET}"
        echo -e "  ${WHITE}Type:${RESET}     TOTP  |  ${WHITE}Digits:${RESET} 6  |  ${WHITE}Period:${RESET} 30s"
        echo ""
        warn "Save this secret now. It will not be displayed again."
        echo ""
    else
        info "Skipped — you can enable this later"
        echo ""
    fi
else
    ok "TOTP already configured"
fi

# --- Admin Passcode (always auto-generated if missing) ---
existing_passcode=$(env_get "ADMIN_PASSCODE" "$ENV_FILE")
if [ -z "$existing_passcode" ]; then
    admin_pass=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16)
    echo "ADMIN_PASSCODE=\"$admin_pass\"" >> "$ENV_FILE"
    echo ""
    echo -e "  ${WHITE}${BOLD}Admin Passcode (auto-generated)${RESET}"
    echo ""
    echo -e "  ${GREEN}${BOLD}  $admin_pass${RESET}"
    echo ""
    warn "Save this passcode securely. It will not be displayed again."
    warn "Needed for initial admin authentication with the bot."
    echo ""
fi

hr

# ---------------------------------------------------------------------------
# Migrate legacy single-database if needed
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/data/ori.db" ] && [ ! -f "$SCRIPT_DIR/data/ori-sessions.db" ]; then
    echo ""
    info "Migrating legacy database..."
    python3 "$SCRIPT_DIR/scripts/migrate_split_db.py"
    ok "Database migrated"
    echo ""
fi

# ---------------------------------------------------------------------------
# Launch container stack
# ---------------------------------------------------------------------------
echo ""
echo -e "  ${BOLD}Building & launching${RESET}"
echo ""

info "Tearing down old instances..."
docker compose down 2>&1 | while read -r line; do echo -e "  ${DIM}$line${RESET}"; done

echo ""
info "Building fresh image..."
docker compose build --no-cache --pull 2>&1 | while read -r line; do echo -e "  ${DIM}$line${RESET}"; done

echo ""
info "Starting container..."
docker compose up -d 2>&1 | while read -r line; do echo -e "  ${DIM}$line${RESET}"; done

echo ""
info "Cleaning up old images..."
docker image prune -af --filter "label!=com.docker.compose.project" > /dev/null 2>&1
docker container prune -f > /dev/null 2>&1
ok "Container stack is running"
echo ""
hr

# ---------------------------------------------------------------------------
# Restart host watchers (deploy & rollback)
# ---------------------------------------------------------------------------
DEPLOY_PID_FILE="$SCRIPT_DIR/data/.deploy_watcher.pid"
ROLLBACK_PID_FILE="$SCRIPT_DIR/data/.rollback_watcher.pid"

for PID_FILE in "$DEPLOY_PID_FILE" "$ROLLBACK_PID_FILE"; do
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            kill "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PID_FILE"
    fi
done

nohup "$SCRIPT_DIR/deploy.sh"   >> "$SCRIPT_DIR/data/deploy.log"   2>&1 &
echo $! > "$DEPLOY_PID_FILE"
nohup "$SCRIPT_DIR/rollback.sh" >> "$SCRIPT_DIR/data/rollback.log" 2>&1 &
echo $! > "$ROLLBACK_PID_FILE"

# ---------------------------------------------------------------------------
# Post-launch summary
# ---------------------------------------------------------------------------
echo ""
echo -e "  ${GREEN}${BOLD}${bot_name:-Ori} is alive.${RESET}"
echo ""
hr
echo ""
echo -e "  ${BOLD}Quick Reference${RESET}"
echo ""
echo -e "  ${WHITE}Logs${RESET}          docker logs -f ori-agent-daemon"
echo -e "  ${WHITE}Deploy log${RESET}    tail -f data/deploy.log"
echo -e "  ${WHITE}Rollback log${RESET}  tail -f data/rollback.log"
echo -e "  ${WHITE}Stop${RESET}          docker compose down"
echo -e "  ${WHITE}Config${RESET}        data/.env"
echo ""
hr
echo ""
echo -e "  ${YELLOW}${BOLD}ADMIN AUTHENTICATION${RESET}"
echo ""
echo -e "  ${DIM}The system needs your user ID to grant admin access.${RESET}"
echo ""
echo -e "  ${WHITE}1.${RESET} Send any message to the bot on Telegram"
echo -e "  ${WHITE}2.${RESET} The bot will reject you and show your ID ${DIM}(e.g., tg_12345678)${RESET}"
echo -e "  ${WHITE}3.${RESET} Send this command to the bot:"
echo ""
echo -e "     ${CYAN}/init <PASSCODE> ADMIN_USER_IDS=\"tg_YOUR_ID\"${RESET}"
echo ""
echo -e "  ${DIM}Forgot your passcode? Check data/.env (never share it).${RESET}"
echo ""
hr
echo ""
