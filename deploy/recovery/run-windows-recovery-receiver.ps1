$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'lib'
& py -3.11 -m hermes_cli.managed_node_recovery_service --host 127.0.0.1 --port 9121 --config (Join-Path $root 'managed-nodes.json')
exit $LASTEXITCODE
