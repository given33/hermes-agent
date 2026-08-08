# Start the local Pi node agent with the Hermes coordinator tunnel so the
# mobile app can reach this PC through daxueshenmai.top from any network.
# Run from the hermes-agent checkout root (or set $PiServerRoot).

$ErrorActionPreference = 'Stop'

$PiServerRoot = if ($env:CODING_PI_SERVER_ROOT) {
  $env:CODING_PI_SERVER_ROOT
} else {
  'C:\Users\given\hermes-audit\hermes-agent'
}
$NodeAgent = Join-Path $PiServerRoot 'coding-pi-server\node_agent.py'
$Standalone = Join-Path $PiServerRoot 'coding-pi-server\standalone_server.py'
$Python = 'C:\Users\given\AppData\Local\Programs\Python\Python311\python.exe'
$SourceRoot = 'C:\Users\given\AppData\Local\hermes\coding-pi\source-private'
$Bun = 'C:\Users\given\.bun\bin\bun.exe'

if (-not $env:CODING_PI_COORDINATOR_URL) {
  $env:CODING_PI_COORDINATOR_URL = 'https://daxueshenmai.top/api/plugins/coding-pi'
}
if (-not $env:CODING_PI_COORDINATOR_TOKEN) {
  throw 'CODING_PI_COORDINATOR_TOKEN is required; set it in the environment.'
}
# Collab links handed to mobile clients point at the Hermes server relay.
if (-not $env:CODING_PI_RELAY_URL) {
  $env:CODING_PI_RELAY_URL = 'wss://daxueshenmai.top'
}

Write-Host "Starting Pi node agent with coordinator $env:CODING_PI_COORDINATOR_URL"

& $Python $NodeAgent `
  --host 0.0.0.0 `
  --port 8786 `
  --python $Python `
  --script $Standalone `
  --cwd $PiServerRoot `
  --service-arg=--host --service-arg=0.0.0.0 `
  --service-arg=--port --service-arg=8787 `
  --service-arg=--root --service-arg=$SourceRoot `
  --service-arg=--repository --service-arg=https://github.com/given33/hemres-pi.git `
  --service-arg=--ref --service-arg=3a8591a8af5b6d200088d12ca75a5517cb064fa8 `
  --service-arg=--bun --service-arg=$Bun `
  --service-arg=--workspace --service-arg=$PiServerRoot `
  --service-arg=--allow-workspace --service-arg=$PiServerRoot `
  --service-arg=--allow-workspace --service-arg=$PiServerRoot `
  --service-arg=--home --service-arg=C:\Users\given\AppData\Local\hermes\coding-pi\standalone-runtime `
  --service-arg=--public-host --service-arg=auto `
  --service-arg=--node-id --service-arg=local-pc
