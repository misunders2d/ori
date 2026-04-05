#!/usr/bin/env bash
# ============================================================================
# Ori Bootstrap — One-liner installer for new instances
# ============================================================================
# Usage:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/misunders2d/ori/master/deploy/bootstrap.sh)" -- --name "MyAgent"
#
# Custom directory:
#   bash -c "$(curl -fsSL ...)" -- --name "Scout" --dir ./scout
#
# What it does:
#   1. Clones the repo
#   2. Detaches from Ori's remote (creates a fresh local git repo)
#   3. Sets up Python venv + dependencies
#   4. Launches the setup wizard (interactive)
#   5. Installs as a system service
# ============================================================================
set -euo pipefail

BOT_NAME="Ori"
INSTALL_DIR=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) BOT_NAME="$2"; shift 2 ;;
        --dir)  INSTALL_DIR="$2"; shift 2 ;;
        *)      echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Default install directory: current directory + bot name
if [ -z "$INSTALL_DIR" ]; then
    SAFE_NAME="$(echo "$BOT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | sed 's/[^a-z0-9-]//g')"
    INSTALL_DIR="$(pwd)/$SAFE_NAME"
fi

echo "============================================"
echo "  Ori Platform Bootstrap"
echo "  Name: $BOT_NAME"
echo "  Directory: $INSTALL_DIR"
echo "============================================"
echo ""

# --- Prerequisites ---
if ! command -v git &>/dev/null; then
    echo "Error: git is required. Install it first."
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "Error: Python 3 is required. Install Python 3.10+ first."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
    echo "Error: Python 3.10+ required (found $PY_VERSION)"
    exit 1
fi

# --- Clone ---
if [ -d "$INSTALL_DIR" ]; then
    echo "Error: $INSTALL_DIR already exists. Remove it or choose a different --dir."
    exit 1
fi

echo ":: Cloning Ori platform..."
git clone --depth 1 https://github.com/misunders2d/ori.git "$INSTALL_DIR"

# --- Detach from Ori's remote, create fresh repo ---
cd "$INSTALL_DIR"
rm -rf .git
git init
git add .
git commit -m "Initial commit — forked from Ori platform"
echo ":: Created fresh local git repository."

# --- Install uv if not present ---
if ! command -v uv &>/dev/null; then
    echo ":: Installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# --- Setup venv + deps ---
echo ":: Installing Python dependencies..."
uv sync

# --- Pre-seed BOT_NAME in vault ---
mkdir -p data/vault
PYTHON="$INSTALL_DIR/.venv/bin/python"
"$PYTHON" -c "
import json, os, tempfile
vault_dir = 'data/vault'
vault_file = os.path.join(vault_dir, 'credentials.json')
data = {}
if os.path.exists(vault_file):
    with open(vault_file) as f:
        data = json.load(f)
data['BOT_NAME'] = '$BOT_NAME'
fd, tmp = tempfile.mkstemp(dir=vault_dir, prefix='.vault.')
with os.fdopen(fd, 'w') as f:
    json.dump(data, f, indent=2)
os.rename(tmp, vault_file)
print(':: BOT_NAME set to: $BOT_NAME')
"

# --- Launch ---
echo ""
echo ":: Setup complete! Starting $BOT_NAME..."
echo ""
chmod +x deploy/start.sh deploy/install.sh
exec deploy/start.sh
