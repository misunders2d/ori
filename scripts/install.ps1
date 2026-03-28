# Ori "One-Liner" Installation & Detachment Script (Windows)
# This script clones the repository, removes the connection to the original repo,
# and starts the setup process.

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "    🧬 Ori — Digital Organism Birth" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# Check prerequisites
foreach ($cmd in "docker", "git", "python", "curl") {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "ERROR: '$cmd' is not installed or not in your PATH. Please install it first."
        exit 1
    }
}

# Clone
Write-Host "  [+] Cloning Ori (latest master)..." -ForegroundColor Green
git clone --depth=1 https://github.com/misunders2d/ori.git ori-organism

# Enter directory
Set-Location ori-organism

# Detach DNA
Write-Host "  [+] Severing DNA connection (detaching from origin)..." -ForegroundColor Yellow
Remove-Item -Recurse -Force .git
git init -b master
git add .
git commit -m "Initial birth of Ori Organism"

# Launch Setup
Write-Host "  [+] Launching incubation wizard..." -ForegroundColor Green
Write-Host ""
.\start.bat
