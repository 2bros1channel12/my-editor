param([switch]$IncludeMediaTools)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$installer = Join-Path $repo 'src-tauri\target\release\bundle\nsis\Local Cut_0.1.0_x64-setup.exe'
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Build the Windows installer first.' }
$bundle = Join-Path $repo ('validation-artifacts\sandbox-' + [guid]::NewGuid().ToString('N'))
$inputFolder = Join-Path $bundle 'input'
$outputFolder = Join-Path $bundle 'output'
New-Item -ItemType Directory -Path $inputFolder,$outputFolder | Out-Null
Copy-Item -LiteralPath $installer -Destination (Join-Path $inputFolder 'LocalCut-setup.exe')
foreach ($name in @('run-sandbox-validation.ps1','smoke-sidecar.ps1')) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $inputFolder }
if ($IncludeMediaTools) {
  $toolDir = Join-Path $inputFolder 'tools'; New-Item -ItemType Directory -Path $toolDir | Out-Null
  foreach ($name in @('ffmpeg','ffprobe')) {
    $tool = Get-Command $name -ErrorAction Stop
    Copy-Item -LiteralPath $tool.Source -Destination (Join-Path $toolDir ($name + '.exe'))
  }
}
$manifest = @{ installerSha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash; mediaToolsIncluded = [bool]$IncludeMediaTools; createdAt = [DateTime]::UtcNow.ToString('o') }
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $inputFolder 'manifest.json') -Encoding UTF8
$escapedInput = [Security.SecurityElement]::Escape($inputFolder)
$escapedOutput = [Security.SecurityElement]::Escape($outputFolder)
$configuration = @"
<Configuration>
  <Networking>Disable</Networking>
  <VGpu>Disable</VGpu>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MemoryInMB>16384</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>$escapedInput</HostFolder><SandboxFolder>C:\LocalCutQA\Input</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$escapedOutput</HostFolder><SandboxFolder>C:\LocalCutQA\Output</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\LocalCutQA\Input\run-sandbox-validation.ps1</Command></LogonCommand>
</Configuration>
"@
$configuration | Set-Content -LiteralPath (Join-Path $bundle 'LocalCut-offline.wsb') -Encoding UTF8
Write-Output "Validation bundle prepared: $bundle"
Write-Output 'Not executed. On another host, regenerate from its checkout or update the two HostFolder paths to the copied input/output directories before opening the .wsb. Only output/ is writable from the guest.'
