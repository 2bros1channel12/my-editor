[CmdletBinding()]
param([switch]$Install)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $projectRoot
$npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
$pythonExecutable = Join-Path $projectRoot 'service/.venv/Scripts/python.exe'
if ($Install) {
    & $npmCommand ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        & python -m venv service/.venv
        if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed. Install Python 3.11 or newer.' }
    }
    & $pythonExecutable -m pip install -e './service[test]'
    if ($LASTEXITCODE -ne 0) { throw 'Installing Python dependencies failed.' }
}
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw 'Run ./scripts/dev.ps1 -Install first.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'node_modules'))) { throw 'Run ./scripts/dev.ps1 -Install first.' }
$env:LOCAL_CUT_PYTHON = $pythonExecutable
$env:LOCAL_CUT_SERVICE_DIR = Join-Path $projectRoot 'service'
$env:PYTHONPATH = Join-Path $projectRoot 'service/src'
& $npmCommand run tauri dev
if ($LASTEXITCODE -ne 0) { throw 'Desktop startup failed. Check Rust/MSVC, Windows SDK and WebView2 prerequisites.' }
