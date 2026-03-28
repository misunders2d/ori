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
for cmd in docker git python3 curl; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "ERROR: '$cmd' is not installed. Please install it first."
        exit 1
    fi
done

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
./start.sh
