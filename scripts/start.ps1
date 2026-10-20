$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
Set-Location $projectRoot
$env:PYTHONUTF8 = "1"

if (-not (Test-Path $venvPython)) {
    throw "Run scripts\setup.ps1 first."
}
if (-not (Test-Path "frontend\dist\index.html")) {
    throw "The frontend is not built. Run scripts\setup.ps1 first."
}

& $venvPython -m giji @args
if ($LASTEXITCODE -ne 0) {
    throw "Giji AutoMemo stopped with exit code $LASTEXITCODE."
}
