# Ori "One-Liner" Installation & Detachment Script (Windows)
# This script clones the repository, removes the connection to the original repo,
# and starts the setup process.

# Force UTF-8 encoding for the session to handle emojis/DNA icons
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Support custom folder name via argument
$TargetDir = if ($args[0]) { $args[0] } else { "ori-organism" }

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "    🧬 Ori — Digital Organism Birth" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Target: $TargetDir" -ForegroundColor Cyan
Write-Host ""

# Check prerequisites
$Prereqs = [ordered]@{
    "docker" = "Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
    "git"    = "Install Git for Windows from https://git-scm.com/download/win"
    "python" = "Install Python from https://www.python.org/downloads/windows/ or the Microsoft Store"
    "curl"   = "Curl is missing. It is usually built-in to Windows 10/11. Please check your system updates."
}

foreach ($item in $Prereqs.GetEnumerator()) {
    $cmd = $item.Key
    $help = $item.Value
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Host "----------------------------------------------------------" -ForegroundColor Red
        Write-Host "  ❌ ERROR: '$cmd' is not installed or not in your PATH." -ForegroundColor Red
        Write-Host "  👉 HELP:  $help" -ForegroundColor Yellow
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

# Detach DNA
Write-Host "  [+] Severing DNA connection (detaching from origin)..." -ForegroundColor Yellow
Remove-Item -Recurse -Force .git

# Initialize fresh history
git init -b master
git config user.email "organism@local.host"
git config user.name "Ori Birth Process"

# Prepare Rootless baseline
if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" }
New-Item -ItemType File -Path "data\.last_build"

git add .
git commit -m "Initial birth of Ori Organism"

# Launch Setup
Write-Host "  [+] Launching incubation wizard..." -ForegroundColor Green
Write-Host ""
.\start.bat
