#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'HermesManagedRecovery'),
    [string]$TokenSource = 'C:\Users\given\.codex\dbb3-team\widget\config.json',
    [string]$CloudAdminKey = 'C:\Users\given\.codex\aliyun-hermes\aliyun_hermes_ed25519'
)

$ErrorActionPreference = 'Stop'
$lib = Join-Path $InstallRoot 'lib'
$package = Join-Path $lib 'hermes_cli'
$account = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force -Path $package | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'logs') | Out-Null
foreach ($sensitiveFile in @('recovery.token', 'cloud-admin.key')) {
    $existing = Join-Path $InstallRoot $sensitiveFile
    if (Test-Path -LiteralPath $existing) {
        icacls $existing /grant:r "${account}:(F)" | Out-Null
    }
}

Copy-Item (Join-Path $SourceRoot 'hermes_cli\__init__.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_nodes.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_node_recovery_service.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_node_recovery_watchdog.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_constants.py') $lib -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-receiver.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-watchdog.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-tunnel.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'recover-wsl.ps1') $InstallRoot -Force
Copy-Item -LiteralPath $CloudAdminKey -Destination (Join-Path $InstallRoot 'cloud-admin.key') -Force

$token = ''
if ((Test-Path -LiteralPath $TokenSource) -and $TokenSource.EndsWith('.json')) {
    $token = [string]((Get-Content -LiteralPath $TokenSource -Raw | ConvertFrom-Json).token)
} elseif (Test-Path -LiteralPath $TokenSource) {
    $token = (Get-Content -LiteralPath $TokenSource -Raw).Trim()
}
if ([string]::IsNullOrWhiteSpace($token)) { throw 'A non-empty DBB3 status/recovery token is required.' }
[IO.File]::WriteAllText((Join-Path $InstallRoot 'recovery.token'), $token.Trim() + "`n", [Text.UTF8Encoding]::new($false))

$config = [ordered]@{
    nodes = @([ordered]@{
        id = 'hermes-fabric'
        label = 'DBB3 + Windows PC + WSL'
        status_url = 'http://10.66.0.2:8766/status'
        token_file = (Join-Path $InstallRoot 'recovery.token')
        recovery_urls = [ordered]@{
            dbb3 = 'https://daxueshenmai.top/_hermes/recovery/dbb3'
            wsl = 'https://daxueshenmai.top/_hermes/recovery/wsl'
        }
        auto_recover = $true
        timeout_seconds = 8
        recovery_cooldown_seconds = 90
    })
    recovery_receiver = [ordered]@{
        node_id = 'wsl'
        token_file = (Join-Path $InstallRoot 'recovery.token')
        command = @(
            'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
            '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $InstallRoot 'recover-wsl.ps1')
        )
        state_file = (Join-Path $InstallRoot 'receiver-state.json')
    }
}
$configJson = $config | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText(
    (Join-Path $InstallRoot 'managed-nodes.json'),
    $configJson,
    [Text.UTF8Encoding]::new($false)
)

icacls $InstallRoot /inheritance:r /grant:r "${account}:(OI)(CI)(F)" | Out-Null
icacls (Join-Path $InstallRoot 'recovery.token') /inheritance:r /grant:r "${account}:(R)" | Out-Null
icacls (Join-Path $InstallRoot 'cloud-admin.key') /inheritance:r /grant:r "${account}:(R)" | Out-Null

$powershell = (Get-Command powershell.exe).Source
function Register-HermesTask([string]$name, [string]$script) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $name
}
Register-HermesTask 'Hermes Managed Recovery Receiver' (Join-Path $InstallRoot 'run-windows-recovery-receiver.ps1')
Register-HermesTask 'Hermes Managed Recovery Watchdog' (Join-Path $InstallRoot 'run-windows-recovery-watchdog.ps1')
Register-HermesTask 'Hermes Managed Recovery Tunnel' (Join-Path $InstallRoot 'run-windows-recovery-tunnel.ps1')

[pscustomobject]@{
    InstallRoot = $InstallRoot
    Receiver = 'Hermes Managed Recovery Receiver'
    Watchdog = 'Hermes Managed Recovery Watchdog'
    Tunnel = 'Hermes Managed Recovery Tunnel'
}
