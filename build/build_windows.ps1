<# 
.SYNOPSIS
    Build VibeCheck for Windows — produces an NSIS installer.

.DESCRIPTION
    Automates the full Windows build pipeline: venv setup, dependency
    installation, PyInstaller bundling, and NSIS installer creation.

    Prerequisites:
    - Python 3.11 or 3.12
    - whisper-cli.exe downloaded and placed at build\bin\whisper-cli.exe
      (from https://github.com/ggerganov/whisper.cpp/releases)
    - NSIS installed (https://nsis.sourceforge.io/) and makensis on PATH

.EXAMPLE
    cd C:\path\to\VibeCheck
    .\build\build_windows.ps1
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host ""
Write-Host "=== VibeCheck Windows Build ===" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host ""

Set-Location $ProjectRoot

# ------------------------------------------------------------------
# 1. Virtual environment
# ------------------------------------------------------------------
Write-Host "-- Step 1/4: Setting up virtual environment --" -ForegroundColor Yellow

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .venv\Scripts\Activate.ps1

pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install pyinstaller -q

Write-Host "   OK: venv ready ($(python --version))" -ForegroundColor Green

# ------------------------------------------------------------------
# 2. Check whisper-cli.exe
# ------------------------------------------------------------------
Write-Host ""
Write-Host "-- Step 2/4: Checking whisper-cli.exe --" -ForegroundColor Yellow

$WhisperBin = Join-Path $ProjectRoot "build\bin\whisper-cli.exe"
if (Test-Path $WhisperBin) {
    Write-Host "   OK: Found $WhisperBin" -ForegroundColor Green
} else {
    Write-Host "   WARNING: whisper-cli.exe not found at $WhisperBin" -ForegroundColor Red
    Write-Host "   Download from: https://github.com/ggerganov/whisper.cpp/releases"
    Write-Host "   Place at: build\bin\whisper-cli.exe"
    Write-Host "   Continuing build without bundled whisper..."
}

# ------------------------------------------------------------------
# 3. Run PyInstaller
# ------------------------------------------------------------------
Write-Host ""
Write-Host "-- Step 3/4: Running PyInstaller --" -ForegroundColor Yellow

pyinstaller vibecheck.spec --noconfirm --clean

Write-Host "   OK: Build complete -> dist\VibeCheck\" -ForegroundColor Green

# ------------------------------------------------------------------
# 4. Create NSIS installer (if makensis is available)
# ------------------------------------------------------------------
Write-Host ""
Write-Host "-- Step 4/4: Creating NSIS installer --" -ForegroundColor Yellow

$MakeNSIS = Get-Command makensis -ErrorAction SilentlyContinue
if ($MakeNSIS) {
    makensis build\vibecheck.nsi
    Write-Host "   OK: Installer created -> dist\VibeCheckSetup.exe" -ForegroundColor Green
} else {
    Write-Host "   SKIP: NSIS not installed. Install from https://nsis.sourceforge.io/" -ForegroundColor Red
    Write-Host "   You can still run dist\VibeCheck\VibeCheck.exe directly."
}

Write-Host ""
Write-Host "=== Build Complete! ===" -ForegroundColor Cyan
