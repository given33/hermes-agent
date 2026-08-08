param(
  [string]$VariableName = "DEEPSEEK_API_KEY",
  [string]$RuntimeHome = "$env:LOCALAPPDATA\hermes\coding-pi\standalone-runtime"
)

$ErrorActionPreference = "Stop"
$secure = Read-Host "Enter the Pi provider key (input is hidden)" -AsSecureString
$secretRoot = Join-Path $RuntimeHome "secrets"
$secretPath = Join-Path $secretRoot "$VariableName.dpapi"
New-Item -ItemType Directory -Force -Path $secretRoot | Out-Null
$secure | ConvertFrom-SecureString | Set-Content -LiteralPath $secretPath -Encoding UTF8
Write-Output "Saved the DPAPI-protected $VariableName credential for the current Windows user. It is not written to the Hermes or Pi source tree."
Write-Output "Restart the 'Hermes Pi Local Node' scheduled task to apply it."
