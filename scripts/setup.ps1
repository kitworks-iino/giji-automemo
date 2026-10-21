param(
    [switch]$SkipFrontend,
    [string]$Python = "",
    [switch]$CheckPythonOnly
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

function Find-CompatiblePython {
    $checker = Join-Path $PSScriptRoot "check_python.py"
    if ($Python) {
        $selected = & $Python $checker
        Assert-NativeSuccess "Requested Python interpreter check"
        return $selected
    }
    # Prefer 3.13, including company-managed installations without py.exe.
    # A launcher with no matching runtime must not prevent the PATH fallback.
    foreach ($minor in @("3.13", "3.12", "3.11")) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            try {
                $selected = & py "-$minor" $checker --require-minor $minor 2>$null
                if ($LASTEXITCODE -eq 0) { return $selected }
            }
            catch { } # Windows PowerShell may promote native stderr to an error.
        }
        foreach ($command in @("python", "python3")) {
            if (Get-Command $command -ErrorAction SilentlyContinue) {
                try {
                    $selected = & $command $checker --require-minor $minor 2>$null
                    if ($LASTEXITCODE -eq 0) { return $selected }
                }
                catch { }
            }
        }
    }
    throw "Install standard Python 3.13 x64 (3.11/3.12 x64 also work), or pass -Python 'C:\path\python.exe'."
}

Write-Host "Setting up Giji AutoMemo in $projectRoot"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    & $venvPython (Join-Path $PSScriptRoot "check_python.py")
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .venv is incompatible. Keep your application data, move .venv aside, and run setup with standard Python 3.13 x64."
    }
    $selectedPython = $venvPython
    & $venvPython (Join-Path $PSScriptRoot "audit_dependencies.py") --environment --metadata-only
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .venv contains prohibited legacy audio packages. Move .venv aside and create a clean environment; application data is separate."
    }
}
else {
    if (Test-Path (Join-Path $projectRoot ".venv")) {
        throw "The existing .venv has no usable Windows Python. Move .venv aside and run setup again."
    }
    $selectedPython = Find-CompatiblePython
}
if ($CheckPythonOnly) {
    Write-Host "Compatible Python: $selectedPython"
    return
}
if (-not (Test-Path $venvPython)) {
    & $selectedPython -m venv .venv
    Assert-NativeSuccess "Virtual environment creation"
    & $venvPython (Join-Path $PSScriptRoot "check_python.py")
    Assert-NativeSuccess "Virtual environment interpreter check"
}
& $venvPython -m pip install --upgrade pip
Assert-NativeSuccess "pip update"
& $venvPython -m pip install -e ".[windows,local]"
Assert-NativeSuccess "Python dependency installation"
& $venvPython (Join-Path $PSScriptRoot "audit_dependencies.py") --environment
Assert-NativeSuccess "Audio dependency verification"

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
