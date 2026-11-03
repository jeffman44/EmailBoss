<#
.SYNOPSIS
  Remove the Email Boss install made by install_windows.ps1.

.DESCRIPTION
  Stops the running board, removes the logon task, the Start Menu shortcut,
  the venv under %LOCALAPPDATA%\EmailBoss and the PB_DATA_ROOT variable.
  The data folder (%USERPROFILE%\EmailBoss: database, config, mail cache,
  backups) is KEPT unless -PurgeData is passed.

.PARAMETER PurgeData
  Also delete %USERPROFILE%\EmailBoss. Irreversible.
#>
[CmdletBinding()]
param([switch]$PurgeData)

$ErrorActionPreference = "SilentlyContinue"

$InstallRoot = Join-Path $env:LOCALAPPDATA "EmailBoss"
$DataRoot    = [Environment]::GetEnvironmentVariable("PB_DATA_ROOT", "User")
if (-not $DataRoot) { $DataRoot = Join-Path $env:USERPROFILE "EmailBoss" }
$TaskName    = "Email Boss"
$ShortcutLnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Email Boss.lnk"

# 1. stop the board
Get-CimInstance Win32_Process -Filter "name = 'pythonw.exe' or name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*plannerboard*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped pid $($_.ProcessId)" }

# 2. logon task
if (Get-ScheduledTask -TaskName $TaskName) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'"
}

# 3. shortcut
if (Test-Path $ShortcutLnk) { Remove-Item $ShortcutLnk -Force; Write-Host "Removed shortcut" }

# 4. venv + launchers
if (Test-Path $InstallRoot) { Remove-Item $InstallRoot -Recurse -Force; Write-Host "Removed $InstallRoot" }

# 5. env var
[Environment]::SetEnvironmentVariable("PB_DATA_ROOT", $null, "User")

# 6. data (opt-in)
if ($PurgeData -and (Test-Path $DataRoot)) {
    Remove-Item $DataRoot -Recurse -Force
    Write-Host "Removed data folder $DataRoot"
} else {
    Write-Host "Data folder kept: $DataRoot"
}
Write-Host "Email Boss uninstalled."
