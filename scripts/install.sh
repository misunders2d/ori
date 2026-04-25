#!/bin/bash
# Ori "One-Liner" Installation & Detachment Script (Linux/macOS)
# This script clones the repository, removes the connection to the original repo,
# and starts the setup process.

set -e

# Support custom folder name via argument
TARGET_DIR="${1:-ori-organism}"

echo "=========================================="
echo "    Ori — Digital Organism Birth"
echo "=========================================="
echo "  Target: $TARGET_DIR"
echo ""

# Check prerequisites
check_cmd() {
    local cmd=$1
    local help=$2
    if ! command -v "$cmd" &> /dev/null; then
        echo ""
        echo "----------------------------------------------------------"
        echo "  ERROR: '$cmd' is not installed."
        echo "  HELP:  $help"
        echo "----------------------------------------------------------"
        echo ""
        exit 1
    fi
}

check_cmd "docker" "Install Docker: https://docs.docker.com/get-docker/"
check_cmd "git" "Install Git: https://git-scm.com/downloads"
check_cmd "python3" "Install Python 3: https://www.python.org/downloads/"

# Clone
echo "  [+] Cloning Ori (latest master)..."
git clone --depth=1 https://github.com/misunders2d/ori.git "$TARGET_DIR"

# Enter directory
cd "$TARGET_DIR"

# Detach from original repo
echo "  [+] Severing DNA connection (detaching from origin)..."
rm -rf .git

# Initialize fresh local history. We keep canonical ori as 'upstream' so the
# user can `git fetch upstream` later to pull platform fixes without losing
# their independent local history.
git init -b master
git remote add upstream https://github.com/misunders2d/ori.git
git config user.email "organism@local.host"
git config user.name "Ori Birth Process"

# Prepare baseline
mkdir -p data
touch data/.last_build

git add .
git commit -m "Initial birth of Ori Organism"

# Make scripts executable
chmod +x launcher.sh install.sh deploy.sh start.sh

# Run setup wizard on host (not in container — needs gcloud, browser, etc.)
echo "  [+] Launching incubation wizard..."
echo ""
python3 app/transports/setup_wizard.py </dev/tty

# Start the bot
echo ""
echo "  [+] Starting Ori..."
exec ./launcher.sh </dev/tty
