Option Explicit

Dim shell
Dim exitCode
Set shell = CreateObject("WScript.Shell")
exitCode = shell.Run("C:\Windows\System32\wsl.exe -d HermesUbuntu -- bash /opt/pc-team/run-pc-cloud-connector.sh", 0, True)
WScript.Quit exitCode
