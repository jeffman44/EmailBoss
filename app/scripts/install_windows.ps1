<#
.SYNOPSIS
  Install Email Boss for the current Windows user. No admin rights needed.

.DESCRIPTION
  Idempotent. Re-running upgrades the package in place and rewrites the
  shortcut and the logon task.

    venv        %LOCALAPPDATA%\EmailBoss\venv
    data        %USERPROFILE%\EmailBoss   (PB_DATA_ROOT, user environment variable)
    shortcut    Start Menu\Programs\Email Boss.lnk
    autostart   Task Scheduler task "Email Boss" (at logon, pythonw -m plannerboard.start)

.PARAMETER Source
  What to pip install. Default: the repo checkout this script lives in
  (..\ relative to scripts\). Pass a wheel path or a PyPI-style spec instead.

.PARAMETER Python
  Python launcher or interpreter to build the venv with. Default: "py -3.11"
  if the launcher is present, else "python".

.PARAMETER NoAutostart
  Skip the Task Scheduler task.

.PARAMETER Port
  Port the board listens on (sets PB_PORT for the user) and the shortcut opens. Default 8080.

.PARAMETER TaskName
  Name of the logon autostart task. Default "Email Boss". Change it if another
  task already uses that name; an existing task with this name is replaced.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Python = "",
    [switch]$NoAutostart,
    [int]$Port = 8080,
    [string]$TaskName = "Email Boss"
)

$ErrorActionPreference = "Stop"

$InstallRoot = Join-Path $env:LOCALAPPDATA "EmailBoss"
$VenvDir     = Join-Path $InstallRoot "venv"
$DataRoot    = Join-Path $env:USERPROFILE "EmailBoss"
$StartMenu   = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$ShortcutLnk = Join-Path $StartMenu "Email Boss.lnk"

if (-not $Source) {
    $Source = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Resolve-Python {
    param([string]$Pref)
    if ($Pref) { return $Pref }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "-3.13", "-3.12", "-3.11") {
            & py $v -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return "py $v" }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
    throw "Python 3.11+ was not found. Install it from python.org (check 'Add to PATH') and re-run."
}

$PyCmd = Resolve-Python $Python
Write-Host "Python   : $PyCmd"
Write-Host "Install  : $InstallRoot"
Write-Host "Data     : $DataRoot"
Write-Host "Source   : $Source"

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path $DataRoot    | Out-Null

# 1. venv (create once, reuse after)
$VenvPython  = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonW = Join-Path $VenvDir "Scripts\pythonw.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating venv..."
    Invoke-Expression "$PyCmd -m venv `"$VenvDir`""
}
& $VenvPython -m pip install --upgrade pip wheel --quiet
Write-Host "Installing plannerboard..."
& $VenvPython -m pip install --upgrade "$Source" --quiet
# optional tray icon; failure is not fatal
& $VenvPython -m pip install --upgrade "pystray>=0.19" "pillow>=10" --quiet 2>$null
& $VenvPython -m pip install --upgrade "pywebview>=5.0" "plyer>=2.1" --quiet
if ($LASTEXITCODE -ne 0) { throw "pywebview install failed; the desktop window needs it" }
$Version = & $VenvPython -c "import plannerboard; print(plannerboard.__version__)"
Write-Host "Installed plannerboard $Version"

# 2. data root env var (user scope, idempotent)
[Environment]::SetEnvironmentVariable("PB_DATA_ROOT", $DataRoot, "User")
$env:PB_DATA_ROOT = $DataRoot
[Environment]::SetEnvironmentVariable("PB_PORT", "$Port", "User")
$env:PB_PORT = "$Port"

# 3. launcher scripts
$LaunchCmd = Join-Path $InstallRoot "Start Email Boss.cmd"
@"
@echo off
set PB_DATA_ROOT=$DataRoot
set PB_PORT=$Port
start "" "$VenvPythonW" -m plannerboard.start --desktop
"@ | Set-Content -Path $LaunchCmd -Encoding ASCII

$StopCmd = Join-Path $InstallRoot "Stop Email Boss.cmd"
@"
@echo off
rem Stop every pythonw running Email Boss for this user.
for /f "tokens=2 delims=," %%p in ('wmic process where "name='pythonw.exe' and commandline like '%%plannerboard%%'" get processid /format:csv ^| findstr /r "[0-9]"') do taskkill /pid %%p /f >nul 2>&1
"@ | Set-Content -Path $StopCmd -Encoding ASCII

# 4. Start Menu shortcut
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($ShortcutLnk)
$sc.TargetPath = $LaunchCmd
$sc.WorkingDirectory = $InstallRoot
$sc.Description = "Email Boss (local project and inbox board)"
$sc.WindowStyle = 7   # minimized
$sc.Save()
Write-Host "Shortcut : $ShortcutLnk"

# 5. Task Scheduler logon task (replace if present)
if (-not $NoAutostart) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    $action   = New-ScheduledTaskAction -Execute $VenvPythonW -Argument "-m plannerboard.start --desktop" -WorkingDirectory $InstallRoot
    $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Days 0) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Starts Email Boss at logon (http://127.0.0.1:$Port/)" | Out-Null
    Write-Host "Autostart: Task Scheduler task '$TaskName' (at logon)"
    # start it now so the user does not have to log off and on
    Start-ScheduledTask -TaskName $TaskName
} else {
    Write-Host "Autostart: skipped (-NoAutostart)"
}

Write-Host ""
Write-Host "Done. Open http://127.0.0.1:$Port/ (the setup wizard runs on first start)."
Write-Host "Uninstall with: powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows.ps1"
