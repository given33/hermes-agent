$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $root 'lib'
& py -3.11 -m hermes_cli.managed_node_recovery_watchdog --config (Join-Path $root 'managed-nodes.json') --interval 30 --state-file (Join-Path $root 'watchdog.json')
exit $LASTEXITCODE
