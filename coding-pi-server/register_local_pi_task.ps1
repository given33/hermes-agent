param(
  [string]$Launcher = "",
  [string]$TaskName = "Hermes Pi Local Node",
  [string]$CoordinatorUrl = ""
)

$ErrorActionPreference = "Stop"
if (-not $Launcher) {
  $Launcher = Join-Path $PSScriptRoot "start_local_pi.ps1"
}
$resolvedLauncher = (Resolve-Path -LiteralPath $Launcher).Path
$taskUser = "$env:USERDOMAIN\$env:USERNAME"
$actionArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$resolvedLauncher`""
if ($CoordinatorUrl) {
  $actionArgs += " -CoordinatorUrl `"$CoordinatorUrl`""
}
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered scheduled task: $TaskName"
Write-Output "Launcher: $resolvedLauncher"
