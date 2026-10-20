param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$env:PYTHONUTF8 = "1"

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

Write-Host "Setting up Giji AutoMemo in $projectRoot"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.11 -m venv .venv
        Assert-NativeSuccess "Python 3.11 environment creation"
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c "import sys; assert sys.version_info[:2] == (3, 11), 'Install Python 3.11 x64 first'"
        Assert-NativeSuccess "Python version check"
        & python -m venv .venv
        Assert-NativeSuccess "Virtual environment creation"
    }
    else {
        throw "Install Python 3.11 x64 (with the Python launcher), then run this script again."
    }
}

$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
Assert-NativeSuccess "pip update"
& $venvPython -m pip install -e ".[windows,local]"
Assert-NativeSuccess "Python dependency installation"

if (-not $SkipFrontend) {
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "Install Node.js 22.12 or later, reopen PowerShell, and run setup again."
    }
    Push-Location (Join-Path $projectRoot "frontend")
    try {
        & npm.cmd ci
        Assert-NativeSuccess "Frontend dependency installation"
        & npm.cmd run build
        Assert-NativeSuccess "Frontend build"
    }
    finally {
        Pop-Location
    }
}
elseif (-not (Test-Path "frontend\dist\index.html")) {
    throw "No built frontend was found. Run setup without -SkipFrontend."
}

Write-Host "Setup complete. Run: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1"
Write-Host "Audio models are downloaded separately from the app settings."
