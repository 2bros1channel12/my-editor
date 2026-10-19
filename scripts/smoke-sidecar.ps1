$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$targetTriple = $env:TAURI_ENV_TARGET_TRIPLE
if (-not $targetTriple) { $targetTriple = "x86_64-pc-windows-msvc" }
if ($targetTriple -ne "x86_64-pc-windows-msvc") { throw "The packaged service smoke check currently supports x86_64-pc-windows-msvc only; got $targetTriple." }
$executable = Join-Path $repoRoot "src-tauri\binaries\localcut-service-$targetTriple.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Packaged service was not found at $executable. Run npm run service:sidecar first." }
$dataRoot = Join-Path ([IO.Path]::GetTempPath()) ("localcut-sidecar-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $dataRoot | Out-Null
$previousDataDir = [Environment]::GetEnvironmentVariable("LOCAL_CUT_DATA_DIR", "Process")
$process = $null

try {
  [Environment]::SetEnvironmentVariable("LOCAL_CUT_DATA_DIR", $dataRoot, "Process")
  $start = [Diagnostics.ProcessStartInfo]::new()
  $start.FileName = $executable
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardInput = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $process = [Diagnostics.Process]::new()
  $process.StartInfo = $start
  if (-not $process.Start()) { throw "The packaged service did not start." }

  $requestId = 0
  function Invoke-SidecarRequest([string]$method, [hashtable]$params = @{}) {
    $script:requestId += 1
    $request = @{ protocolVersion = 1; requestId = $script:requestId; method = $method; params = $params } | ConvertTo-Json -Depth 12 -Compress
    $script:process.StandardInput.WriteLine($request)
    $script:process.StandardInput.Flush()
    $read = $script:process.StandardOutput.ReadLineAsync()
    if (-not $read.Wait([TimeSpan]::FromSeconds(15))) { throw "Sidecar request '$method' timed out." }
    $line = $read.GetAwaiter().GetResult()
    if ([string]::IsNullOrWhiteSpace($line)) {
      $stderr = $script:process.StandardError.ReadToEnd()
      throw "Sidecar exited before replying to '$method'. $stderr"
    }
    $response = $line | ConvertFrom-Json
    if ($response.requestId -ne $script:requestId) { throw "Sidecar response ID did not match request '$method'." }
    if ($response.error) { throw "Sidecar request '$method' failed: $($response.error.message)" }
    return $response.result
  }

  $ping = Invoke-SidecarRequest "ping"
  if ($ping.service -ne "localcut" -or $ping.protocolVersion -ne 1) { throw "Sidecar ping returned an incompatible service or protocol version." }
  $health = Invoke-SidecarRequest "health"
  if (-not $health.service.available -or -not $health.storage.available) { throw "Sidecar health check failed: $($health | ConvertTo-Json -Compress -Depth 8)" }
  $projectPath = Join-Path $dataRoot "smoke project"
  $created = Invoke-SidecarRequest "project_create" @{ path = $projectPath; name = "Sidecar smoke"; settings = @{} }
  if (-not (Test-Path -LiteralPath (Join-Path $projectPath "project.json") -PathType Leaf)) { throw "Sidecar did not create a project manifest." }
  $reopened = Invoke-SidecarRequest "project_open_last"
  if ($reopened.projectId -ne $created.projectId -or $reopened.path -ne $projectPath) { throw "Last-project reopen did not return the created local project." }

  $process.StandardInput.Close()
  if (-not $process.WaitForExit(15000)) { throw "Sidecar did not shut down after stdin closed." }
  if ($process.ExitCode -ne 0) { throw "Sidecar exited with code $($process.ExitCode). $($process.StandardError.ReadToEnd())" }
  Write-Output "Sidecar smoke passed: protocol v$($ping.protocolVersion), health, project create, last-project reopen, clean shutdown."
}
finally {
  if ($process -and -not $process.HasExited) {
    try { $process.Kill($true); $process.WaitForExit(5000) } catch { }
  }
  if ($process) { $process.Dispose() }
  [Environment]::SetEnvironmentVariable("LOCAL_CUT_DATA_DIR", $previousDataDir, "Process")
  if (Test-Path -LiteralPath $dataRoot) { Remove-Item -LiteralPath $dataRoot -Recurse -Force }
}
