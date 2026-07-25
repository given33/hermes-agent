#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'HermesManagedRecovery'),
    [string]$TokenSource = 'C:\Users\given\.codex\dbb3-team\widget\config.json',
    [Parameter(Mandatory = $true)]
    [string]$ManagedInstallationTokenSource,
    [string]$CloudAdminKey = 'C:\Users\given\.codex\aliyun-hermes\aliyun_hermes_ed25519',
    [string]$WslDistro = 'HermesUbuntu'
)

$ErrorActionPreference = 'Stop'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$installVolumeRoot = [IO.Path]::GetPathRoot($InstallRoot)
$windowsRoot = [IO.Path]::GetFullPath($env:WINDIR)
if (($InstallRoot.TrimEnd('\') -eq $installVolumeRoot.TrimEnd('\')) -or
    $InstallRoot.StartsWith($windowsRoot, [StringComparison]::OrdinalIgnoreCase) -or
    ([IO.Path]::GetFullPath($SourceRoot).StartsWith($InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase))) {
    throw "InstallRoot is unsafe: $InstallRoot"
}
$lib = Join-Path $InstallRoot 'lib'
$package = Join-Path $lib 'hermes_cli'
$account = "$env:USERDOMAIN\$env:USERNAME"
$managedTasks = @(
    'Hermes Managed Recovery Receiver',
    'Hermes Managed Recovery Watchdog',
    'Hermes Managed Recovery Tunnel',
    'Hermes PC Cloud Connector'
)
$transactionStatePath = "$InstallRoot.install-transaction.json"
$transactionRoot = "$InstallRoot.install-transaction"
$wslRollbackBackup = ''

function Get-TaskSnapshotKey([string]$TaskName) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($TaskName))
        return (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally {
        $sha.Dispose()
    }
}

function Write-TransactionState([string]$Phase) {
    $state = [ordered]@{
        version = 1
        phase = $Phase
        transaction_root = $transactionRoot
        wsl_rollback_backup = $wslRollbackBackup
        updated_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $temporary = "$transactionStatePath.new.$PID"
    [IO.File]::WriteAllText(
        $temporary,
        (($state | ConvertTo-Json -Depth 4) + "`n"),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $transactionStatePath -Force
}

function ConvertTo-WslPath([string]$WindowsPath) {
    $converted = & wsl.exe -d $WslDistro -u root -- wslpath -a -u ($WindowsPath -replace '\\', '/')
    if (($LASTEXITCODE -ne 0) -or [string]::IsNullOrWhiteSpace($converted)) {
        throw "Failed to convert path for WSL: $WindowsPath"
    }
    return ([string]$converted).Trim()
}

function Restore-WindowsTransaction([object]$State) {
    $savedRoot = [string]$State.transaction_root
    if ([string]::IsNullOrWhiteSpace($savedRoot) -or -not (Test-Path -LiteralPath $savedRoot -PathType Container)) {
        throw 'Managed recovery transaction snapshot is missing.'
    }
    $taskRoot = Join-Path $savedRoot 'tasks'
    $installSnapshot = Join-Path $savedRoot 'install-root'
    if (Test-Path -LiteralPath $InstallRoot) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $installSnapshot -PathType Container) {
        Copy-Item -LiteralPath $installSnapshot -Destination $InstallRoot -Recurse -Force
    }
    foreach ($taskName in $managedTasks) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        $taskKey = Get-TaskSnapshotKey $taskName
        $taskXml = Join-Path $taskRoot "$taskKey.xml"
        if (Test-Path -LiteralPath $taskXml -PathType Leaf) {
            Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -LiteralPath $taskXml -Raw) -Force | Out-Null
            if (Test-Path -LiteralPath (Join-Path $taskRoot "$taskKey.running")) {
                Start-ScheduledTask -TaskName $taskName
            }
        }
    }
}

function Invoke-WslTransactionRollback([string]$RollbackBackup) {
    if ([string]::IsNullOrWhiteSpace($RollbackBackup)) { return }
    $wslSource = ConvertTo-WslPath $SourceRoot
    $wslTokenSource = ConvertTo-WslPath (Join-Path $InstallRoot 'managed-installation.token')
    $wslKeySource = ConvertTo-WslPath (Join-Path $InstallRoot 'cloud-admin.key')
    $installer = "$wslSource/deploy/recovery/install-wsl-managed-installation.sh"
    & wsl.exe -d $WslDistro -u root -- bash $installer $wslSource $wslTokenSource $wslKeySource "--rollback-backup=$RollbackBackup"
    if ($LASTEXITCODE -ne 0) {
        throw "WSL rollback failed with exit code $LASTEXITCODE"
    }
}

function Get-PersistedWslRollback([object]$State) {
    $candidate = [string]$State.wsl_rollback_backup
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $handlePath = Join-Path ([string]$State.transaction_root) 'wsl-rollback-handle'
        if (Test-Path -LiteralPath $handlePath -PathType Leaf) {
            $candidate = (Get-Content -LiteralPath $handlePath -Raw).Trim()
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($candidate) -and $candidate -match '[\r\n]') {
        throw 'Persisted WSL rollback handle is invalid.'
    }
    return $candidate
}

function Complete-TransactionCleanup([object]$State, [string]$MarkerName) {
    $savedRoot = [string]$State.transaction_root
    New-Item -ItemType File -Path (Join-Path $savedRoot $MarkerName) -Force | Out-Null
    # State is the rollback authority. Remove it first only after either commit
    # or rollback is durable; a remaining marked snapshot is safe to reap.
    Remove-Item -LiteralPath $transactionStatePath -Force
    Remove-Item -LiteralPath $savedRoot -Recurse -Force
}

if (Test-Path -LiteralPath $transactionStatePath -PathType Leaf) {
    $unfinished = Get-Content -LiteralPath $transactionStatePath -Raw | ConvertFrom-Json
    if ([string]$unfinished.phase -eq 'windows-committed') {
        Complete-TransactionCleanup $unfinished 'commit-complete.marker'
    } else {
        Invoke-WslTransactionRollback (Get-PersistedWslRollback $unfinished)
        Restore-WindowsTransaction $unfinished
        Complete-TransactionCleanup $unfinished 'rollback-complete.marker'
    }
} elseif (Test-Path -LiteralPath $transactionRoot -PathType Container) {
    if ((Test-Path -LiteralPath (Join-Path $transactionRoot 'commit-complete.marker')) -or
        (Test-Path -LiteralPath (Join-Path $transactionRoot 'rollback-complete.marker'))) {
        Remove-Item -LiteralPath $transactionRoot -Recurse -Force
    } else {
        throw 'Untracked managed recovery transaction snapshot requires operator review.'
    }
}

New-Item -ItemType Directory -Force -Path $transactionRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $transactionRoot 'tasks') | Out-Null
if (Test-Path -LiteralPath $InstallRoot -PathType Container) {
    Copy-Item -LiteralPath $InstallRoot -Destination (Join-Path $transactionRoot 'install-root') -Recurse -Force
}
foreach ($taskName in $managedTasks) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        $taskKey = Get-TaskSnapshotKey $taskName
        Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $transactionRoot "tasks\$taskKey.xml") -Encoding UTF8
        if ($task.State -eq 'Running') {
            New-Item -ItemType File -Path (Join-Path $transactionRoot "tasks\$taskKey.running") | Out-Null
        }
    }
}
Write-TransactionState 'snapshotted'

try {
New-Item -ItemType Directory -Force -Path $package | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot 'logs') | Out-Null
foreach ($sensitiveFile in @('recovery.token', 'cloud-admin.key')) {
    $existing = Join-Path $InstallRoot $sensitiveFile
    if (Test-Path -LiteralPath $existing) {
        icacls $existing /grant:r "${account}:(F)" | Out-Null
    }
}

Copy-Item (Join-Path $SourceRoot 'hermes_cli\__init__.py') $package -Force
# The shared HTTP module imports this file, but the Windows service remains
# recovery-only. WSL installs run in WSL below.
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_installations.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_nodes.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_node_recovery_service.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_cli\managed_node_recovery_watchdog.py') $package -Force
Copy-Item (Join-Path $SourceRoot 'hermes_constants.py') $lib -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-receiver.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-watchdog.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'run-windows-recovery-tunnel.ps1') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'run-pc-cloud-connector-hidden.vbs') $InstallRoot -Force
Copy-Item (Join-Path $PSScriptRoot 'recover-wsl.ps1') $InstallRoot -Force
Copy-Item -LiteralPath $CloudAdminKey -Destination (Join-Path $InstallRoot 'cloud-admin.key') -Force

$token = ''
if ((Test-Path -LiteralPath $TokenSource) -and $TokenSource.EndsWith('.json')) {
    $token = [string]((Get-Content -LiteralPath $TokenSource -Raw | ConvertFrom-Json).token)
} elseif (Test-Path -LiteralPath $TokenSource) {
    $token = (Get-Content -LiteralPath $TokenSource -Raw).Trim()
}
if ([string]::IsNullOrWhiteSpace($token) -or $token.Trim().Length -lt 32 -or $token.Trim().Length -gt 4096) {
    throw 'The DBB3 status/recovery token must contain 32..4096 characters.'
}
$recoveryTokenTarget = Join-Path $InstallRoot 'recovery.token'
$recoveryTokenTemporary = "$recoveryTokenTarget.new.$PID"
[IO.File]::WriteAllText($recoveryTokenTemporary, $token.Trim() + "`n", [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $recoveryTokenTemporary -Destination $recoveryTokenTarget -Force

if (-not (Test-Path -LiteralPath $ManagedInstallationTokenSource -PathType Leaf)) {
    throw 'A dedicated managed installation token file is required.'
}
$installationTokenItem = Get-Item -LiteralPath $ManagedInstallationTokenSource -Force
if (($installationTokenItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'The managed installation token source must not be a symlink or reparse point.'
}
$managedInstallationRaw = Get-Content -LiteralPath $ManagedInstallationTokenSource -Raw
$managedInstallationToken = $managedInstallationRaw.TrimEnd("`r", "`n")
if ($managedInstallationToken.Length -lt 32 -or $managedInstallationToken.Length -gt 4096) {
    throw 'The managed installation token must contain 32..4096 characters.'
}
if (($managedInstallationToken -match '[\r\n]') -or
    ($managedInstallationRaw -notin @($managedInstallationToken, "$managedInstallationToken`n", "$managedInstallationToken`r`n")) -or
    ($managedInstallationToken -ne $managedInstallationToken.Trim()) -or
    ($managedInstallationToken -eq $token.Trim())) {
    throw 'The managed installation token must be exactly one line and distinct from recovery credentials.'
}
$managedInstallationTokenTarget = Join-Path $InstallRoot 'managed-installation.token'
$managedInstallationTokenTemporary = "$managedInstallationTokenTarget.new.$PID"
[IO.File]::WriteAllText(
    $managedInstallationTokenTemporary,
    $managedInstallationToken + "`n",
    [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $managedInstallationTokenTemporary -Destination $managedInstallationTokenTarget -Force
$managedInstallationToken = $null

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
            '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $InstallRoot 'recover-wsl.ps1')
        )
        state_file = (Join-Path $InstallRoot 'receiver-state.json')
    }
}
$configJson = $config | ConvertTo-Json -Depth 8
$managedNodesTarget = Join-Path $InstallRoot 'managed-nodes.json'
$managedNodesTemporary = "$managedNodesTarget.new.$PID"
[IO.File]::WriteAllText(
    $managedNodesTemporary,
    $configJson,
    [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $managedNodesTemporary -Destination $managedNodesTarget -Force

icacls $InstallRoot /inheritance:r /grant:r "${account}:(OI)(CI)(F)" | Out-Null
icacls (Join-Path $InstallRoot 'recovery.token') /inheritance:r /grant:r "${account}:(R)" | Out-Null
icacls (Join-Path $InstallRoot 'managed-installation.token') /inheritance:r /grant:r "${account}:(R)" | Out-Null
icacls (Join-Path $InstallRoot 'cloud-admin.key') /inheritance:r /grant:r "${account}:(R)" | Out-Null

# Keep installation execution in Linux. The Windows Python service above is
# recovery-only and receives only the fixed recovery command configuration.
$wslSourceRoot = ConvertTo-WslPath $SourceRoot
$wslToken = ConvertTo-WslPath (Join-Path $InstallRoot 'managed-installation.token')
$wslKey = ConvertTo-WslPath (Join-Path $InstallRoot 'cloud-admin.key')
$wslInstaller = "$wslSourceRoot/deploy/recovery/install-wsl-managed-installation.sh"
$wslHandlePath = Join-Path $transactionRoot 'wsl-rollback-handle'
$wslHandle = ConvertTo-WslPath $wslHandlePath
& wsl.exe -d $WslDistro -u root -- bash $wslInstaller $wslSourceRoot $wslToken $wslKey "--handle-file=$wslHandle"
if ($LASTEXITCODE -ne 0) {
    throw "WSL managed installation receiver setup failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $wslHandlePath -PathType Leaf)) {
    throw 'WSL installer did not return a rollback handle.'
}
$wslRollbackBackup = (Get-Content -LiteralPath $wslHandlePath -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($wslRollbackBackup) -or $wslRollbackBackup -match '[\r\n]') {
    throw 'WSL installer returned an invalid rollback handle.'
}
Write-TransactionState 'wsl-committed'

$powershell = (Get-Command powershell.exe).Source
function Register-HermesTask([string]$name, [string]$script) {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $name
}
Register-HermesTask 'Hermes Managed Recovery Receiver' (Join-Path $InstallRoot 'run-windows-recovery-receiver.ps1')
Register-HermesTask 'Hermes Managed Recovery Watchdog' (Join-Path $InstallRoot 'run-windows-recovery-watchdog.ps1')
Register-HermesTask 'Hermes Managed Recovery Tunnel' (Join-Path $InstallRoot 'run-windows-recovery-tunnel.ps1')

$pcTask = 'Hermes PC Cloud Connector'
Stop-ScheduledTask -TaskName $pcTask -ErrorAction SilentlyContinue
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$pcScript = Join-Path $InstallRoot 'run-pc-cloud-connector-hidden.vbs'
$pcAction = New-ScheduledTaskAction -Execute $wscript -Argument ('//B //NoLogo "' + $pcScript + '"')
$pcTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$pcPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$pcSettings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $pcTask -Action $pcAction -Trigger $pcTrigger -Principal $pcPrincipal -Settings $pcSettings -Force | Out-Null
Start-ScheduledTask -TaskName $pcTask
Write-TransactionState 'windows-committed'
$committedState = Get-Content -LiteralPath $transactionStatePath -Raw | ConvertFrom-Json
Complete-TransactionCleanup $committedState 'commit-complete.marker'

[pscustomobject]@{
    InstallRoot = $InstallRoot
    Receiver = 'Hermes Managed Recovery Receiver'
    Watchdog = 'Hermes Managed Recovery Watchdog'
    Tunnel = 'Hermes Managed Recovery Tunnel'
    PcConnector = $pcTask
}
} catch {
    $originalError = $_
    try {
        if (Test-Path -LiteralPath $transactionStatePath -PathType Leaf) {
            $state = Get-Content -LiteralPath $transactionStatePath -Raw | ConvertFrom-Json
            $persistedRollback = Get-PersistedWslRollback $state
            if (-not [string]::IsNullOrWhiteSpace($persistedRollback)) {
                Invoke-WslTransactionRollback $persistedRollback
            }
            Restore-WindowsTransaction $state
            Complete-TransactionCleanup $state 'rollback-complete.marker'
        }
    } catch {
        throw "Managed recovery install failed and rollback was incomplete. Original: $originalError Rollback: $_"
    }
    throw $originalError
}
