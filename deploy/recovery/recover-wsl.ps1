$ErrorActionPreference = 'Stop'

# WSL may be stopped, so this script runs on Windows and starts the distro
# before touching its systemd/user services.
$linux = @'
set -eu
systemctl start ssh.service 2>/dev/null || systemctl start sshd.service 2>/dev/null || true
if systemctl list-unit-files hermes-gateway.service >/dev/null 2>&1; then
  systemctl restart hermes-gateway.service || true
fi
uid="$(id -u hermes)"
runtime="/run/user/${uid}"
bus="unix:path=${runtime}/bus"
for unit in hermes-gateway.service pc-cloud-connector.service dbb3-proxy-relay.service; do
  if runuser -u hermes -- env XDG_RUNTIME_DIR="${runtime}" DBUS_SESSION_BUS_ADDRESS="${bus}" systemctl --user list-unit-files "${unit}" >/dev/null 2>&1; then
    runuser -u hermes -- env XDG_RUNTIME_DIR="${runtime}" DBUS_SESSION_BUS_ADDRESS="${bus}" systemctl --user restart "${unit}" || true
  fi
done
'@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($linux))
$command = "echo $encoded | base64 -d | bash"
& wsl.exe -d HermesUbuntu -u root -- bash -c $command
if ($LASTEXITCODE -ne 0) { throw "WSL recovery command failed with exit code $LASTEXITCODE" }

# Recreate the Windows-side SSH relay and PC connector after WSL is ready.
Start-ScheduledTask -TaskName 'Hermes WSL SSH Relay' -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'Hermes PC Cloud Connector' -ErrorAction SilentlyContinue
