# Ori "One-Liner" Installation & Detachment Script (Windows)
# This script clones the repository, removes the connection to the original repo,
# and starts the setup process.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Support custom folder name via argument
$TargetDir = if ($args[0]) { $args[0] } else { "ori-organism" }

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "    Ori - Digital Organism Birth" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Target: $TargetDir" -ForegroundColor Cyan
Write-Host ""

# Check prerequisites
$Prereqs = [ordered]@{
    "docker" = "Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
    "git"    = "Install Git for Windows from https://git-scm.com/download/win"
    "python" = "Install Python from https://www.python.org/downloads/windows/ or the Microsoft Store"
}

foreach ($item in $Prereqs.GetEnumerator()) {
    $cmd = $item.Key
    $help = $item.Value
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "----------------------------------------------------------" -ForegroundColor Red
        Write-Host "  ERROR: '$cmd' is not installed or not in your PATH." -ForegroundColor Red
        Write-Host "  HELP:  $help" -ForegroundColor Yellow
        Write-Host "----------------------------------------------------------" -ForegroundColor Red
        Write-Host ""
        Read-Host "Press Enter to exit and try again after installing..."
        exit 1
    }
}

# Clone
Write-Host "  [+] Cloning Ori (latest master)..." -ForegroundColor Green
git clone --depth=1 https://github.com/misunders2d/ori.git "$TargetDir"

# Enter directory
Set-Location "$TargetDir"

# Detach from original repo
Write-Host "  [+] Severing DNA connection (detaching from origin)..." -ForegroundColor Yellow
Remove-Item -Recurse -Force .git

# Initialize fresh local history. We keep canonical ori as 'upstream' so the
# user can `git fetch upstream` later to pull platform fixes without losing
# their independent local history.
git init -b master
git remote add upstream https://github.com/misunders2d/ori.git
git config user.email "organism@local.host"
git config user.name "Ori Birth Process"

# Prepare baseline
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }
New-Item -ItemType File -Path "data\.last_build" -Force | Out-Null

git add .
git commit -m "Initial birth of Ori Organism"

# Run setup wizard on host
Write-Host "  [+] Launching incubation wizard..." -ForegroundColor Green
Write-Host ""
python interfaces/setup_wizard.py

# Start the bot (WSL launcher or docker compose directly)
Write-Host ""
Write-Host "  [+] Starting Ori..." -ForegroundColor Green
if (Get-Command "wsl" -ErrorAction SilentlyContinue) {
    wsl ./launcher.sh
} else {
    docker compose up --build
}
