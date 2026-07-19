$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$key = Join-Path $root 'cloud-admin.key'
while ($true) {
    & ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes `
        -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 `
        -N -R 127.0.0.1:19122:127.0.0.1:9121 admin@10.66.0.1
    Start-Sleep -Seconds 5
}
