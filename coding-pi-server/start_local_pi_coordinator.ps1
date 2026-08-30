param(
  [string]$CoordinatorUrl = "https://daxueshenmai.top/api/plugins/coding-pi",
  [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  [string]$SourceRoot = "$env:LOCALAPPDATA\hermes\coding-pi\source-private",
  [string]$RuntimeHome = "$env:LOCALAPPDATA\hermes\coding-pi\standalone-runtime",
  [string]$Workspace = "",
  [string]$NodeId = "local-pc"
)

# The coordinator tunnel is outbound. Keep both local listeners on loopback
# and reuse the hardened scheduled-task launcher instead of exposing 8786/8787.
$ErrorActionPreference = "Stop"
if (-not $env:CODING_PI_COORDINATOR_TOKEN) {
  throw "Set CODING_PI_COORDINATOR_TOKEN before starting the coordinator tunnel"
}

$launcher = Join-Path $PSScriptRoot "start_local_pi.ps1"
& $launcher `
  -Python $Python `
  -SourceRoot $SourceRoot `
  -RuntimeHome $RuntimeHome `
  -Workspace $Workspace `
  -NodeId $NodeId `
  -CoordinatorUrl $CoordinatorUrl
exit $LASTEXITCODE
