#!/bin/bash
# Ori "One-Liner" Installation & Detachment Script (Linux/macOS)
# This script clones the repository, removes the connection to the original repo,
# and starts the setup process.

set -e

echo "=========================================="
echo "    🧬 Ori — Digital Organism Birth"
echo "=========================================="
echo ""

# Check prerequisites
check_cmd() {
    local cmd=$1
    local help=$2
    if ! command -v "$cmd" &> /dev/null; then
        echo ""
        echo "----------------------------------------------------------"
        echo "  ❌ ERROR: '$cmd' is not installed."
        echo "  👉 HELP:  $help"
        echo "----------------------------------------------------------"
        echo ""
        exit 1
    fi
}

check_cmd "docker" "Install Docker: https://docs.docker.com/get-docker/"
check_cmd "git" "Install Git: https://git-scm.com/downloads"
check_cmd "python3" "Install Python 3: https://www.python.org/downloads/"
check_cmd "curl" "Install Curl: sudo apt install curl (or brew install curl)"

# Clone
echo "  [+] Cloning Ori (latest master)..."
git clone --depth=1 https://github.com/misunders2d/ori.git ori-organism

# Enter directory
cd ori-organism

# Detach DNA
echo "  [+] Severing DNA connection (detaching from origin)..."
rm -rf .git
git init -b master
git add .
git commit -m "Initial birth of Ori Organism"

# Make scripts executable
chmod +x start.sh deploy.sh rollback.sh

# Launch Setup
echo "  [+] Launching incubation wizard..."
echo ""
exec ./start.sh </dev/tty
