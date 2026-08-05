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

# Mirror the built app to the local fallback copy on C:.
#
# The nightly runs from this project on D:, a removable drive that was absent at 03:00 on two
# consecutive nights (2026-08-04, 2026-08-05), losing the whole night both times because
# there was nothing on the machine to start. install-refresh-task.ps1's last trigger now
# falls back to this copy; keeping it in step with the source is what makes that fallback
# worth having. A fallback quietly rotting a few releases behind the source is worse than
# none — it would crawl with old parsing rules into an old schema and report success.
#
# The exclusion is the important flag, and it must name the ONE directory it means. The local
# copy owns its own top-level data\ folder: desktop.setup_env seeds stonescan.db,
# suppliers.json, denylist.json and locations.json there on first launch, and a spill crawl's
# results and its refresh_runs ledger live in it. /MIR would delete the lot on the next build.
#
# `/XD data` — the bare NAME — was wrong, and the first real build proved it: robocopy matches
# a bare name at every depth, so it also skipped _internal\geonamescache\data (164 MB of
# offline city coordinates, which is the entire source geocode.py resolves the map from) and
# _internal\stonescan\data\us_zips.json.gz. The copy launched and looked fine; its map would
# simply have had nothing to resolve against. A FULL PATH matches only that one directory.
$local = Join-Path $env:ProgramData "StoneScanner"
Write-Output "== Syncing to the local fallback copy: $local =="
robocopy $dist $local /MIR /XD (Join-Path $local "data") /NFL /NDL /NJH /NJS /NP | Out-Null
# robocopy's exit code is a BITMASK, not a status: 1 = files copied, 2 = extra files present,
# 4 = mismatches, and only >= 8 is a real failure. `if ($LASTEXITCODE)` would treat every
# build that actually copied something as broken.
$sync = $LASTEXITCODE
if ($sync -ge 8) {
    Write-Warning "Local copy NOT updated (robocopy $sync) - it is now STALE."
    Write-Warning "  Usually this means the copy is running: close Stone Scanner and re-run"
    Write-Warning "  .\build_exe.ps1 -SkipBuild. Until then a lost drive is still a lost night."
} else {
    Write-Output "   + local copy updated"
}
$global:LASTEXITCODE = 0   # else robocopy's bitmask becomes this SCRIPT's exit code (a
                           # successful build exited 3, which any caller reads as failure)

$size = "{0:N0} MB" -f ((Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Output ""
Write-Output "Done. App folder: $dist  ($size)"
Write-Output "Run it:  $dist\StoneScanner.exe"
Write-Output "Nightly fallback copy: $local"
exit 0
