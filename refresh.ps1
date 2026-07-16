# Refresh the Stone Scanner database from suppliers' live public catalogs.
# Discovers any newly-published catalogs, then re-crawls everything so material
# availability and new arrivals stay current. Safe to run on a schedule.
#
# One-time setup of a daily 3:00 AM refresh (run in an elevated PowerShell):
#   schtasks /Create /SC DAILY /ST 03:00 /TN "StoneScannerRefresh" `
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PWD\refresh.ps1`""
#
# Run manually any time:  .\refresh.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path "refresh.log" -Value "`n===== refresh started $stamp ====="

# --discover finds newly-listed suppliers; --slabs pre-caches every in-stock
# item's full slab gallery so detail pages open instantly during the day.
& $py -u -m stonescan.ingest --discover --slabs --concurrency 4 --delay 1.0 2>&1 |
    Tee-Object -FilePath "refresh.log" -Append

Add-Content -Path "refresh.log" -Value "===== refresh finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="
