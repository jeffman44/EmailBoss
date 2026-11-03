<#
.SYNOPSIS
  Email Boss developer tasks for Windows (PowerShell equivalent of the Makefile).

.EXAMPLE
  .\tasks.ps1 test            # app + license server test suites
  .\tasks.ps1 run             # start the app from source on :8080 with a dev license
  .\tasks.ps1 serve-license   # license server on :8600 (dev key, trials on)
  .\tasks.ps1 build           # wheel + PyInstaller onedir bundle
  .\tasks.ps1 check           # compileall + banned-literal / em-dash scan
  .\tasks.ps1 clean
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "install-dev", "test", "test-app", "test-license", "run", "serve-license", "build", "wheel", "check", "clean")]
    [string]$Task = "help",
    [string]$Python = "python",
    [int]$Port = 8080,
    [int]$LicPort = 8600
)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$App  = Join-Path $Root "app"
$Lic  = Join-Path $Root "license_server"

function Invoke-In($dir, [scriptblock]$block) {
    Push-Location $dir
    try { & $block; if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "failed in $dir (exit $LASTEXITCODE)" } }
    finally { Pop-Location }
}

switch ($Task) {
    "help" { Get-Help $PSCommandPath -Examples }
    "install-dev" {
        Invoke-In $App { & $Python -m pip install -e ".[dev,attachments,http]" }
        Invoke-In $Lic { & $Python -m pip install -e ".[test]" }
    }
    "test"         { & $PSCommandPath test-app -Python $Python; & $PSCommandPath test-license -Python $Python }
    "test-app"     { Invoke-In $App { & $Python -m compileall -q plannerboard; & $Python -m pytest -q } }
    "test-license" { Invoke-In $Lic { & $Python -m pytest -q } }
    "run" {
        $env:PB_LICENSE_DEV = "1"
        Invoke-In $App { & $Python -m plannerboard --port $Port --no-tray }
    }
    "serve-license" {
        $env:TRIAL_ENABLED = "1"
        Invoke-In $Lic { & $Python -m uvicorn licserver.main:app --reload --port $LicPort }
    }
    "wheel" {
        Invoke-In $App { & $Python -m pip install -q --upgrade build; & $Python -m build --wheel --outdir dist }
    }
    "build" { & (Join-Path $App "scripts\build_release.ps1") -Python $Python }
    "check" {
        Invoke-In $App { & $Python -m compileall -q plannerboard }
        $exts = "*.md", "*.py", "*.html", "*.yaml", "*.json", "*.ps1", "*.sh", "*.js", "*.css"
        # This script carries the pattern itself, so it is the one file excluded.
        $files = Get-ChildItem -Path $Root -Recurse -File -Include $exts |
            Where-Object { $_.FullName -notmatch '\\(tests|screens|\.git|__pycache__|dist|build)\\' } |
            Where-Object { $_.FullName -ne $PSCommandPath }
        $bad = $files | Select-String -Pattern 'amsofusa|\bAMS\b|JDeuel|QRF|SAMPro|Tucson|C:\\Workspace'
        $em  = $files | Select-String -Pattern ([string][char]0x2014)
        if ($bad) { $bad | ForEach-Object { Write-Host $_ }; throw "banned literal found" }
        if ($em)  { $em  | ForEach-Object { Write-Host $_ }; throw "em dash found" }
        Write-Host "check ok"
    }
    "clean" {
        foreach ($d in "$App\build", "$App\dist", "$Lic\build", "$Lic\dist") {
            if (Test-Path $d) { Remove-Item $d -Recurse -Force }
        }
        Get-ChildItem -Path $Root -Recurse -Directory -Include "__pycache__", ".pytest_cache", "*.egg-info" |
            ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
