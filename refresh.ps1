# Refresh the Stone Scanner database from suppliers' live public catalogs.
# Discovers any newly-published catalogs, then re-crawls everything so material
# availability and new arrivals stay current. Safe to run on a schedule.
#
# Install the nightly task (run once, in an ELEVATED PowerShell):
#   .\install-refresh-task.ps1
#
# Run manually any time:  .\refresh.ps1
#
# Works from EITHER a source checkout (uses the project venv) OR a packaged install
# (uses StoneScanner.exe --refresh). The nightly task ships in the app folder too, so
# a distributed install refreshes on schedule instead of silently going stale — the
# previous version only knew about .venv\Scripts\python.exe, which the exe doesn't have.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py  = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$exe = Join-Path $PSScriptRoot "StoneScanner.exe"
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
function Invoke-Logged {
    # Run a native command, tee-ing its output into the log in one encoding.
    param([string]$file, [string[]]$cmdArgs)
    & $file @cmdArgs 2>&1 |
        ForEach-Object { Add-Content -Path $log -Value ([string]$_) -Encoding utf8; Write-Host $_ }
    return $LASTEXITCODE
}

if (Test-Path $py) {
    # --- Source checkout: run the module directly in the venv. ---
    # --discover finds newly-listed suppliers; --slabs --slab-cap 40 pre-caches the slab
    # galleries of only the 40 most-stocked items per supplier (plenty to seed the map's
    # yard locations) rather than every in-stock item — the single biggest speedup, since
    # any un-cached gallery is fetched live when a user opens that item. --retry gives
    # anything that trips a Cloudflare challenge one more attempt in the same run.
    Write-Log "===== refresh started (venv) ====="
    $code = Invoke-Logged $py @("-u", "-m", "stonescan.ingest", "--discover", "--slabs",
                                "--slab-cap", "40", "--retry", "--concurrency", "4", "--delay", "1.0")
    # Best-effort: refresh the search-by-photo index, but ONLY if the CLIP model has been
    # downloaded (it's git-ignored / not bundled). A missing model must not fail the run.
    $model = Join-Path $PSScriptRoot "stonescan\models\clip\clip_vision.onnx"
    if ($code -eq 0 -and (Test-Path $model)) {
        Write-Log "indexing catalog images for search-by-photo..."
        Invoke-Logged $py @("-u", "-m", "stonescan.imagesearch", "--index") | Out-Null
    }
    Write-Log "===== refresh finished (exit $code) ====="
    exit $code
}
elseif (Test-Path $exe) {
    # --- Packaged install: the exe's --refresh does the crawl internally. ---
    Write-Log "===== refresh started (packaged exe) ====="
    $code = Invoke-Logged $exe @("--refresh", "--discover")
    Write-Log "===== refresh finished (exit $code) ====="
    exit $code
}
else {
    Write-Log "ERROR: found neither the venv python ($py) nor StoneScanner.exe ($exe). " +
              "Run from a project checkout with the venv set up, or from the packaged app folder."
    exit 1
}
