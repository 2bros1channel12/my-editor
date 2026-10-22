param([string]$ExecutablePath)
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$targetTriple = $env:TAURI_ENV_TARGET_TRIPLE
if (-not $targetTriple) { $targetTriple = "x86_64-pc-windows-msvc" }
if ($targetTriple -ne "x86_64-pc-windows-msvc") { throw "The packaged service smoke check currently supports x86_64-pc-windows-msvc only; got $targetTriple." }
$executable = Join-Path $repoRoot "src-tauri\binaries\localcut-service-$targetTriple.exe"
if ($ExecutablePath) { $executable = (Resolve-Path -LiteralPath $ExecutablePath).Path }
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

  $sampleChecked = $false
  if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    $sample = Invoke-SidecarRequest "sample_create"
    if (-not $sample.path -or $sample.license -ne "CC0-1.0") { throw "Packaged sample creation returned invalid provenance." }
    $timeline = Invoke-SidecarRequest "timeline_get" @{ path = $sample.path }
    if ($timeline.timeline.clips.Count -lt 2 -or $timeline.timeline.captions.Count -lt 1) { throw "Packaged sample did not retain its editable clip/caption." }
    $interchange = Invoke-SidecarRequest "export_otio" @{ path = $sample.path }
    if (-not (Test-Path -LiteralPath (Join-Path $sample.path $interchange.relativePath) -PathType Leaf)) { throw "Packaged sample interchange export was not saved." }
    $render = Invoke-SidecarRequest "export_generate" @{ path = $sample.path; options = @{ mode = "preview"; resolution = 480; frameRate = 30; normalize = $true } }
    $renderDeadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
      $renderJobs = Invoke-SidecarRequest "job_list" @{ projectId = $sample.projectId; limit = 100 }
      $renderJob = $renderJobs.jobs | Where-Object { $_.id -eq $render.id } | Select-Object -First 1
      if ($renderJob.state -in @("completed", "failed", "cancelled")) { break }
      if ([DateTime]::UtcNow -ge $renderDeadline) { throw "Packaged sample render timed out." }
      Start-Sleep -Milliseconds 200
    } while ($true)
    if ($renderJob.state -ne "completed") { throw "Packaged sample render failed: $($renderJob.error | ConvertTo-Json -Compress)" }
    $sampleChecked = $true
  }

  $process.StandardInput.Close()
  if (-not $process.WaitForExit(15000)) { throw "Sidecar did not shut down after stdin closed." }
  if ($process.ExitCode -ne 0) { throw "Sidecar exited with code $($process.ExitCode). $($process.StandardError.ReadToEnd())" }
  Write-Output "Sidecar smoke passed: protocol v$($ping.protocolVersion), health, project create, last-project reopen, clean shutdown."
  if ($sampleChecked) { Write-Output "Packaged synthetic sample creation, timeline restore, editable OTIO export and normalized H.264 preview render passed." }
  else { Write-Output "Sample media checks skipped: FFmpeg/FFprobe not both on PATH." }
}
finally {
  if ($process -and -not $process.HasExited) {
    try { $process.Kill($true); $process.WaitForExit(5000) } catch { }
  }
  if ($process) { $process.Dispose() }
  [Environment]::SetEnvironmentVariable("LOCAL_CUT_DATA_DIR", $previousDataDir, "Process")
  if (Test-Path -LiteralPath $dataRoot) {
    $cleanupTarget = (Resolve-Path -LiteralPath $dataRoot).Path
    $temporaryParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ((Split-Path -Parent $cleanupTarget).TrimEnd('\') -ne $temporaryParent -or (Split-Path -Leaf $cleanupTarget) -notmatch '^localcut-sidecar-smoke-[a-f0-9]{32}$') { throw "Refusing cleanup outside the unique smoke test directory." }
    Remove-Item -LiteralPath $cleanupTarget -Recurse -Force
  }
}
