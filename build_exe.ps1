# Build the standalone Stone Scanner app (onedir) and bundle Chromium into it.
# Usage:  .\build_exe.ps1            (full build)
#         .\build_exe.ps1 -SkipBuild (only re-copy Chromium into an existing dist)
param([switch]$SkipBuild)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pyi = ".\.venv\Scripts\pyinstaller.exe"
if (-not $SkipBuild) {
    Write-Output "== Building with PyInstaller (this takes a few minutes) =="
    & $pyi stonescan.spec --noconfirm
    if (-not $?) { throw "PyInstaller build failed" }
}

$dist = Join-Path $PSScriptRoot "dist\StoneScanner"
$browsers = Join-Path $dist "browsers"
New-Item -ItemType Directory -Force $browsers | Out-Null

$src = Join-Path $env:LOCALAPPDATA "ms-playwright"
Write-Output "== Copying Chromium from $src into the app =="
Get-ChildItem $src -Directory |
    Where-Object { $_.Name -match '^(chromium|winldd)' } |
    ForEach-Object {
        $dest = Join-Path $browsers $_.Name
        if (-not (Test-Path $dest)) { Copy-Item $_.FullName $dest -Recurse -Force }
        Write-Output "   + $($_.Name)"
    }

# Ship the nightly-refresh scripts INTO the app folder. refresh.ps1 detects it's next
# to StoneScanner.exe (no venv) and drives the exe's --refresh; install-refresh-task.ps1
# registers it. Without these here, a packaged install has no way to stay current.
Write-Output "== Copying refresh scripts into the app =="
foreach ($f in @("refresh.ps1", "install-refresh-task.ps1")) {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $dist $f) -Force
    Write-Output "   + $f"
}

$size = "{0:N0} MB" -f ((Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Output ""
Write-Output "Done. App folder: $dist  ($size)"
Write-Output "Run it:  $dist\StoneScanner.exe"
