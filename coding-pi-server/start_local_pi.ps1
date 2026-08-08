param(
  [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  [string]$SourceRoot = "$env:LOCALAPPDATA\hermes\coding-pi\source-private",
  [string]$RuntimeHome = "$env:LOCALAPPDATA\hermes\coding-pi\standalone-runtime",
  [string]$Workspace = "C:\Users\given\hermes-audit\hermes-v20-release",
  [string]$NodeId = "local-pc",
  [string]$CoordinatorUrl = ""
)

$ErrorActionPreference = "Stop"
if (-not $CoordinatorUrl -and $env:CODING_PI_COORDINATOR_URL) {
  $CoordinatorUrl = $env:CODING_PI_COORDINATOR_URL
}
$releaseRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$serviceScript = Join-Path $PSScriptRoot "standalone_server.py"
$agentScript = Join-Path $PSScriptRoot "node_agent.py"
$bun = Join-Path $env:USERPROFILE ".bun\bin\bun.exe"
$repository = "https://github.com/given33/hemres-pi.git"
$ref = "3a8591a8af5b6d200088d12ca75a5517cb064fa8"

foreach ($path in @($Python, $SourceRoot, $serviceScript, $agentScript, $Workspace)) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Hermes Pi local node path does not exist: $path"
  }
}

$env:CODING_PI_PROVIDER = if ($env:CODING_PI_PROVIDER) { $env:CODING_PI_PROVIDER } else { "deepseek" }
$env:CODING_PI_MODEL = if ($env:CODING_PI_MODEL) { $env:CODING_PI_MODEL } else { "deepseek-v4-flash" }
$env:CODING_PI_NODE_ID = $NodeId
$env:CODING_PI_NODE_LABEL = if ($env:CODING_PI_NODE_LABEL) { $env:CODING_PI_NODE_LABEL } else { "Local PC" }
$env:CODING_PI_NODE_AGENT_PORT = "8786"
$env:CODING_PI_NODE_SERVICE_ORIGIN = "http://127.0.0.1:8787"
$env:CODING_PI_PUBLIC_HOST = "auto"
$env:CODING_PI_COORDINATOR_URL = $CoordinatorUrl

# The API key is intentionally not embedded in this script or a scheduled-task
# argument. The optional setup helper stores it as a Windows-user DPAPI blob.
$secretPath = Join-Path $RuntimeHome "secrets\DEEPSEEK_API_KEY.dpapi"
if (Test-Path -LiteralPath $secretPath) {
  $secureKey = Get-Content -LiteralPath $secretPath -Raw | ConvertTo-SecureString
  $keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
  try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)
  }
  finally {
    if ($keyPtr -ne [IntPtr]::Zero) {
      [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    }
  }
}
$serviceArgs = @(
  "--host", "0.0.0.0",
  "--port", "8787",
  "--root", $SourceRoot,
  "--repository", $repository,
  "--ref", $ref,
  "--bun", $bun,
  "--workspace", $Workspace,
  "--allow-workspace", $Workspace,
  "--allow-workspace", $releaseRoot,
  "--home", $RuntimeHome,
  "--public-host", "auto",
  "--node-id", $NodeId
)
if ($CoordinatorUrl) {
  $serviceArgs += @("--coordinator-url", $CoordinatorUrl)
  # Share/collab links must also use the stable public relay when the phone
  # is outside the PC's LAN. The coordinator's /r/<room> WebSocket route is
  # bridged through the same outbound node tunnel.
  $coordinatorOrigin = $CoordinatorUrl.TrimEnd('/') -replace '/api/(plugins/)?coding-pi$',''
  $serviceArgs += @("--relay-url", $coordinatorOrigin, "--collab-web-url", "$coordinatorOrigin/collab")
}

$agentArgs = @(
  "--host", "0.0.0.0",
  "--port", "8786",
  "--python", $Python,
  "--script", $serviceScript,
  "--cwd", $releaseRoot
)
foreach ($argument in $serviceArgs) {
  $agentArgs += "--service-arg=$argument"
}

& $Python $agentScript @agentArgs
exit $LASTEXITCODE
