# Refresh the Stone Scanner database from suppliers' live public catalogs.
# Discovers any newly-published catalogs, then re-crawls everything so material
# availability and new arrivals stay current. Safe to run on a schedule.
#
# Install the nightly task (run once, in an ELEVATED PowerShell):
#   .\install-refresh-task.ps1
#
# Run manually any time:  .\refresh.ps1

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py  = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$log = Join-Path $PSScriptRoot "refresh.log"

# One writer, one encoding. The previous version mixed Add-Content (UTF-8/ANSI) with
# Tee-Object (UTF-16LE) into the same file, so the log came out as garbled spaced-out
# characters that couldn't be grepped — useless for diagnosing a failed run. Everything
# now goes through Write-Log, which appends UTF-8 and echoes to the console.
function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

if (-not (Test-Path $py)) {
    Write-Log "ERROR: venv python not found at $py — run from the project root with the venv set up."
    exit 1
}

Write-Log "===== refresh started ====="
# --discover finds newly-listed suppliers; --slabs --slab-cap 40 pre-caches the slab
# galleries of only the 40 most-stocked items per supplier (plenty to seed the map's
# yard locations) rather than every in-stock item — the single biggest speedup, since
# any un-cached gallery is fetched live when a user opens that item. --retry gives
# anything that trips a Cloudflare challenge one more attempt in the same run.
& $py -u -m stonescan.ingest --discover --slabs --slab-cap 40 --retry --concurrency 4 --delay 1.0 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value ([string]$_) -Encoding utf8; Write-Host $_ }
$code = $LASTEXITCODE
Write-Log "===== refresh finished (exit $code) ====="
exit $code
