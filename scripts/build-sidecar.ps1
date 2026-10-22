param(
  [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$serviceRoot = Join-Path $repoRoot "service"
$buildEnvironment = Join-Path $serviceRoot ".sidecar-build-venv"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
$distDirectory = Join-Path $serviceRoot "build\sidecar-dist"
$workDirectory = Join-Path $serviceRoot "build\pyinstaller-work"
$binariesDirectory = Join-Path $repoRoot "src-tauri\binaries"
$licenseDirectory = Join-Path $repoRoot "resources\licenses"

if (-not $PythonExecutable) { $PythonExecutable = $env:LOCAL_CUT_PYTHON }
if (-not $PythonExecutable) {
  $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pythonLauncher) {
    $PythonExecutable = (& $pythonLauncher.Source -3 -c "import sys; print(sys.executable)").Trim()
  } else {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $PythonExecutable = $pythonCommand.Source
  }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw "Python executable was not found: $PythonExecutable" }

$version = & $PythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) { throw "Could not inspect the selected Python runtime." }
$parts = $version.Trim().Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) { throw "The packaged service requires Python 3.11 or newer." }

$targetTriple = $env:TAURI_ENV_TARGET_TRIPLE
if (-not $targetTriple) { $targetTriple = $env:LOCAL_CUT_TARGET_TRIPLE }
if (-not $targetTriple) {
  $rustc = Get-Command rustc -ErrorAction SilentlyContinue
  if (-not $rustc) {
    $rustcPath = Join-Path $env:USERPROFILE "\.cargo\bin\rustc.exe"
    if (Test-Path -LiteralPath $rustcPath -PathType Leaf) { $rustc = $rustcPath }
  }
  if (-not $rustc) { throw "Rust is required to determine the sidecar target; set TAURI_ENV_TARGET_TRIPLE or LOCAL_CUT_TARGET_TRIPLE to the requested target." }
  $rustcExecutable = if ($rustc -is [string]) { $rustc } else { $rustc.Source }
  $targetTriple = (& $rustcExecutable --print host-tuple).Trim()
  if ($LASTEXITCODE -ne 0) { throw "Could not determine the Rust target triple." }
}
if ($targetTriple -ne "x86_64-pc-windows-msvc") { throw "The first-release service sidecar supports x86_64-pc-windows-msvc only; got $targetTriple." }

if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
  & $PythonExecutable -m venv $buildEnvironment
  if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated sidecar build environment." }
}
& $buildPython -m pip install --disable-pip-version-check "pyinstaller==6.22.3"
if ($LASTEXITCODE -ne 0) { throw "Could not install the pinned PyInstaller build tool." }
& $buildPython -m pip install --disable-pip-version-check $serviceRoot
if ($LASTEXITCODE -ne 0) { throw "Could not install local service runtime dependencies." }

$pythonLicense = Join-Path (Split-Path -Parent $PythonExecutable) "LICENSE.txt"
if (-not (Test-Path -LiteralPath $pythonLicense -PathType Leaf)) { throw "The selected Python runtime does not expose LICENSE.txt beside its executable: $pythonLicense" }
$pyInstallerMetadata = Get-ChildItem -LiteralPath (Join-Path $buildEnvironment "Lib\site-packages") -Directory -Filter "pyinstaller-*.dist-info" | Select-Object -First 1
if (-not $pyInstallerMetadata) { throw "Could not locate the pinned PyInstaller license metadata." }
$pyInstallerLicense = Join-Path $pyInstallerMetadata.FullName "licenses\COPYING.txt"
if (-not (Test-Path -LiteralPath $pyInstallerLicense -PathType Leaf)) { throw "Could not locate the PyInstaller COPYING.txt license." }
New-Item -ItemType Directory -Force -Path $licenseDirectory | Out-Null
Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseDirectory "Python-LICENSE.txt") -Force
Copy-Item -LiteralPath $pyInstallerLicense -Destination (Join-Path $licenseDirectory "PyInstaller-COPYING.txt") -Force
& $buildPython (Join-Path $repoRoot "scripts\prepare-python-notices.py") (Join-Path $licenseDirectory "Python-THIRD-PARTY.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not stage Python dependency license texts." }

New-Item -ItemType Directory -Force -Path $distDirectory, $workDirectory, $binariesDirectory | Out-Null
& $buildPython -m PyInstaller --noconfirm --clean --onefile --console --collect-data jsonschema_specifications --name localcut-service --paths (Join-Path $serviceRoot "src") --distpath $distDirectory --workpath $workDirectory --specpath $workDirectory (Join-Path $serviceRoot "sidecar_entry.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not package the local service." }

$sourceBinary = Join-Path $distDirectory "localcut-service.exe"
$targetBinary = Join-Path $binariesDirectory "localcut-service-$targetTriple.exe"
if (-not (Test-Path -LiteralPath $sourceBinary -PathType Leaf)) { throw "PyInstaller did not produce the expected Windows sidecar executable." }
Copy-Item -LiteralPath $sourceBinary -Destination $targetBinary -Force
$digest = (& $buildPython -c "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" $targetBinary).Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not calculate the sidecar checksum." }
Write-Output "Built $targetBinary"
Write-Output "SHA-256 $digest"
Write-Output "Staged Python and PyInstaller license texts under $licenseDirectory"
