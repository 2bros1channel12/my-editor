$ErrorActionPreference = 'Stop'
# Prevent accidental installation/registry changes on the developer's real desktop.
if ($env:USERNAME -ne 'WDAGUtilityAccount' -or $PSScriptRoot -ne 'C:\LocalCutQA\Input') { throw 'This script only runs from the mapped input directory in Windows Sandbox.' }
$result = [ordered]@{ startedAt=[DateTime]::UtcNow.ToString('o'); networking='disabled by WSB configuration'; installerVerified=$false; installation=$false; installedServiceSmoke=$false; guiWindow=$false; uninstall=$false; fullWorkflowValidated=$false; errors=@() }
$install = Join-Path $env:LOCALAPPDATA 'LocalCut-QA'
$main = $null
function Wait-Child($child, [int]$seconds) {
  if (-not $child.WaitForExit($seconds*1000)) { $child.Kill(); throw 'Validation child timed out.' }
  if ($child.ExitCode -ne 0) { throw "Validation child failed with exit code $($child.ExitCode)." }
}
try {
  $manifest = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') | ConvertFrom-Json
  $setup = Join-Path $PSScriptRoot 'LocalCut-setup.exe'
  if ((Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash -ne $manifest.installerSha256) { throw 'Installer hash does not match the prepared bundle.' }
  $result.installerVerified=$true
  if (Test-Path -LiteralPath $install) { throw 'Expected a fresh install directory. Start a new sandbox.' }
  $installerProcess=Start-Process -FilePath $setup -ArgumentList @('/S','/NS',"/D=$install") -PassThru -WindowStyle Hidden
  Wait-Child $installerProcess 120
  $service=Join-Path $install 'localcut-service.exe'
  if (-not (Test-Path -LiteralPath $service -PathType Leaf)) { throw 'Installed service is missing.' }
  $result.installation=$true
  if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'tools')) { $env:PATH=(Join-Path $PSScriptRoot 'tools')+';'+$env:PATH }
  $smokeLog=Join-Path 'C:\LocalCutQA\Output' 'installed-service-smoke.txt'
  & (Join-Path $PSScriptRoot 'smoke-sidecar.ps1') -ExecutablePath $service *>&1 | Out-File -LiteralPath $smokeLog -Encoding UTF8
  $result.installedServiceSmoke=$true
  $app=Get-ChildItem -LiteralPath $install -Filter '*.exe' | Where-Object { $_.Name -notin @('localcut-service.exe','uninstall.exe') }
  if (@($app).Count -ne 1) { throw 'Installed main executable cannot be identified uniquely.' }
  $env:LOCAL_CUT_DATA_DIR=Join-Path $env:TEMP 'localcut-gui-validation'
  $main=Start-Process -FilePath $app.FullName -PassThru
  for ($i=0;$i -lt 75;$i++) { $main.Refresh(); if ($main.HasExited) { break }; if ($main.MainWindowHandle -ne 0) { $result.guiWindow=$true; break }; Start-Sleep -Milliseconds 200 }
  if (-not $result.guiWindow) { throw 'No native application window appeared. Inspect WebView2 availability.' }
} catch { $result.errors += $_.Exception.Message }
finally {
  if ($main -and -not $main.HasExited) { $null=$main.CloseMainWindow(); if (-not $main.WaitForExit(10000)) { $main.Kill(); $null=$main.WaitForExit(5000) } }
  if ($result.installation) {
    try {
      $uninstaller=Start-Process -FilePath (Join-Path $install 'uninstall.exe') -ArgumentList '/S' -PassThru -WindowStyle Hidden
      Wait-Child $uninstaller 60
      for ($i=0;$i -lt 100;$i++) { if (-not (Test-Path -LiteralPath (Join-Path $install 'localcut-service.exe'))) { $result.uninstall=$true; break }; Start-Sleep -Milliseconds 200 }
      if (-not $result.uninstall) { throw 'Uninstall left the installed service executable.' }
    } catch { $result.errors += $_.Exception.Message }
  }
  $result.finishedAt=[DateTime]::UtcNow.ToString('o')
  $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath 'C:\LocalCutQA\Output\validation.json' -Encoding UTF8
}
