param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path $PSScriptRoot -Parent
Push-Location $Repository
try {
    if ($env:OS -ne "Windows_NT") { throw "Build this package on Windows x64." }
    & $Python (Join-Path $PSScriptRoot "check_python.py") --require-minor 3.13
    if ($LASTEXITCODE -ne 0) { throw "Standard Python 3.13 x64 is required for portable builds." }

    Push-Location (Join-Path $Repository "frontend")
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "The frontend build failed." }
    } finally { Pop-Location }

    # Pre-generate the stable Windows UI Automation COM wrapper so PyInstaller
    # can discover it; end users do not need a writable install directory.
    & $Python -c "import comtypes.client; comtypes.client.GetModule('UIAutomationCore.dll')"
    if ($LASTEXITCODE -ne 0) { throw "UI Automation wrapper generation failed." }
    & $Python -m PyInstaller --noconfirm --clean scripts/portable.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $Distribution = Join-Path $Repository "dist/GijiAutoMemo"
    & "$PSScriptRoot/bundle_vc_runtime.ps1" -Destination $Distribution
    & $Python scripts/bundle_models.py --cache build/model-cache --destination $Distribution
    if ($LASTEXITCODE -ne 0) { throw "Model bundling failed." }
    & $Python scripts/collect_portable_licenses.py --destination $Distribution
    if ($LASTEXITCODE -ne 0) { throw "Third-party license/source collection failed." }
    Copy-Item LICENSE (Join-Path $Distribution "LICENSE-APP-MIT.txt")
    Copy-Item docs/distribution.md (Join-Path $Distribution "START-HERE.md")

    & $Python scripts/package_portable.py --directory $Distribution --output dist/giji-automemo-windows-x64.zip
    if ($LASTEXITCODE -ne 0) { throw "ZIP verification failed." }
    & $Python scripts/smoke_portable.py --archive dist/giji-automemo-windows-x64.zip
    if ($LASTEXITCODE -ne 0) { throw "The extracted application smoke test failed." }
} finally {
    Pop-Location
}
