<#
.SYNOPSIS
  Build a Email Boss release on Windows.

.DESCRIPTION
  1. run the test suite
  2. build the wheel (dist\*.whl)
  3. build the PyInstaller onedir bundle (dist\EmailBoss\)
  4. zip it as dist\EmailBoss-<version>-windows-x64.zip and print its SHA256

.PARAMETER SkipTests
.PARAMETER SkipBundle   Wheel only.
.PARAMETER Python       Interpreter to use (default: python).
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipBundle,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"

$App  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Root = (Resolve-Path (Join-Path $App "..")).Path
Set-Location $App

$Version    = (Get-Content (Join-Path $Root "VERSION") -ErrorAction SilentlyContinue | Select-Object -First 1)
$PkgVersion = & $Python -c "import plannerboard; print(plannerboard.__version__)"
if (-not $Version) { $Version = $PkgVersion }
if ($Version.Trim() -ne $PkgVersion.Trim()) {
    throw "VERSION file ($Version) and plannerboard.__version__ ($PkgVersion) differ. Fix before releasing."
}
$Version = $Version.Trim()
Write-Host "Email Boss $Version (windows-x64)"

if (-not $SkipTests) {
    & $Python -m compileall -q plannerboard
    if ($LASTEXITCODE -ne 0) { throw "compileall failed" }
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

# Release channel: PB_LICENSE_DEV / --dev-license are inert in what we ship.
$ChannelFile = "plannerboard\_channel.py"
$ChannelBackup = Get-Content $ChannelFile -Raw
$IdsFile = "plannerboard\_oauth_ids.py"
$IdsBackup = Get-Content $IdsFile -Raw
try {
    # Built-in sign-in: bake the OAuth client ids from the build environment (CI secrets).
    $ids = Get-Content $IdsFile -Raw
    $ids = $ids -replace 'GMAIL_CLIENT_ID = ""', ('GMAIL_CLIENT_ID = "' + ($env:PB_GMAIL_CLIENT_ID) + '"')
    $ids = $ids -replace 'GMAIL_CLIENT_SECRET = ""', ('GMAIL_CLIENT_SECRET = "' + ($env:PB_GMAIL_CLIENT_SECRET) + '"')
    $ids = $ids -replace 'GRAPH_CLIENT_ID = ""', ('GRAPH_CLIENT_ID = "' + ($env:PB_GRAPH_CLIENT_ID) + '"')
    Set-Content $IdsFile $ids -NoNewline
    if ($env:PB_GMAIL_CLIENT_ID) { Write-Host "oauth: Gmail client id baked in" } else { Write-Host "oauth: no Gmail client id (app-password path only)" }
    if ($env:PB_GRAPH_CLIENT_ID) { Write-Host "oauth: Microsoft client id baked in" } else { Write-Host "oauth: no Microsoft client id (app-password path only)" }
    (Get-Content $ChannelFile -Raw) -replace 'CHANNEL = "dev"', 'CHANNEL = "release"' | Set-Content $ChannelFile -NoNewline
    if (-not (Select-String -Path $ChannelFile -Pattern 'CHANNEL = "release"' -Quiet)) { throw "could not set release channel" }

    & $Python -m pip install --quiet --upgrade build pyinstaller | Out-Null
    & $Python -m build --wheel --outdir dist | Out-Null
    Get-ChildItem dist\*.whl | ForEach-Object { Write-Host "wheel : $($_.FullName)" }

    if (-not $SkipBundle) {
        & $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build scripts\build_pyinstaller.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
        foreach ($f in "static", "professions", "ai\prompts", "schema.sql") {
            if (-not (Test-Path "dist\EmailBoss\_internal\plannerboard\$f")) { throw "missing resource in bundle: $f" }
        }
        $Zip = "dist\EmailBoss-$Version-windows-x64.zip"
        Compress-Archive -Path dist\EmailBoss -DestinationPath $Zip -Force
        Write-Host "bundle: $Zip"
        Write-Host "sha256: $((Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower())"
    }
} finally {
    Set-Content $ChannelFile $ChannelBackup -NoNewline
    Set-Content $IdsFile $IdsBackup -NoNewline
}
Write-Host "done"
