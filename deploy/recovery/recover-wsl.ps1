$ErrorActionPreference = 'Stop'

# WSL may be stopped, so this script runs on Windows and starts the distro
# before touching the worker connector and managed-installation services.
$linux = @'
set -eu
systemctl restart hermes-fabric-update.timer
uid="$(id -u hermes)"
runtime="/run/user/${uid}"
bus="unix:path=${runtime}/bus"
for unit in pc-cloud-connector.service hermes-wsl-managed-installation-receiver.service hermes-wsl-managed-installation-tunnel.service; do
  if runuser -u hermes -- env XDG_RUNTIME_DIR="${runtime}" DBUS_SESSION_BUS_ADDRESS="${bus}" systemctl --user list-unit-files "${unit}" >/dev/null 2>&1; then
    runuser -u hermes -- env XDG_RUNTIME_DIR="${runtime}" DBUS_SESSION_BUS_ADDRESS="${bus}" systemctl --user restart "${unit}" || true
  fi
done
'@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($linux))
$command = "echo $encoded | base64 -d | bash"
& wsl.exe -d HermesUbuntu -u root -- bash -c $command
if ($LASTEXITCODE -ne 0) { throw "WSL recovery command failed with exit code $LASTEXITCODE" }

# Recreate the installed Windows-side recovery paths and PC connector.
foreach ($task in @(
  'Hermes Managed Recovery Receiver',
  'Hermes Managed Recovery Watchdog',
  'Hermes Managed Recovery Tunnel',
  'Hermes PC Cloud Connector'
)) {
  Start-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
}
