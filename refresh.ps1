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

# Playwright resolves browsers from %LOCALAPPDATA%\ms-playwright by default. If they
# were installed from inside a PACKAGED (MSIX) app, that path is container-private: it
# resolves normally for the installing process but does not exist for a scheduled task,
# which then dies at launch with "Executable doesn't exist at ...\chrome-headless-shell.exe"
# — pointing at a path you can plainly see from a normal shell. That is what silently
# broke every nightly run. Point Playwright at a real directory instead: `browsers` is
# the packaged layout (beside the exe), `.browsers` the source-checkout equivalent.
foreach ($cand in @("browsers", ".browsers", "dist\StoneScanner\browsers")) {
    $dir = Join-Path $PSScriptRoot $cand
    if ((Test-Path $dir) -and
        (Get-ChildItem $dir -Directory -Filter "chromium*" -ErrorAction SilentlyContinue)) {
        $env:PLAYWRIGHT_BROWSERS_PATH = $dir
        break
    }
}

# One writer, one encoding. The previous version mixed Add-Content (UTF-8/ANSI) with
# Tee-Object (UTF-16LE) into the same file, so the log came out as garbled spaced-out
# characters that couldn't be grepped — useless for diagnosing a failed run. Everything
# now goes through Write-Log, which appends UTF-8 and echoes to the console.
# Every write to the log is best-effort. The log lives on the project drive, which is
# removable and has gone away mid-run; with $ErrorActionPreference = "Stop" a single failed
# Add-Content terminated THIS script, which broke the pipe its Python child was writing to,
# which killed a three-hour crawl on its next print. Losing log lines is a cosmetic problem.
# Losing the crawl is not. (-ErrorAction Stop is belt-and-braces: the script-level "Stop"
# preference already promotes Add-Content's non-terminating failure into the catch, but the
# explicit switch keeps this function correct if that preference is ever relaxed — as
# Invoke-Logged below now does deliberately.)
function Write-LogLine([string]$line) {
    try { Add-Content -Path $log -Value $line -Encoding utf8 -ErrorAction Stop } catch { }
}
function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-LogLine $line
    Write-Host $line
}
function Invoke-Logged {
    # Run a native command, tee-ing its output into the log in one encoding.
    param([string]$file, [string[]]$cmdArgs)
    # Windows PowerShell 5.1 wraps each line a native command writes to stderr in an
    # ErrorRecord, and "Stop" promotes the FIRST of them to a terminating error — killing
    # this script mid-pipeline and, with it, the child's stdout pipe. That is the same
    # failure this file's logging fix exists to prevent, arriving through a different door,
    # and it is not hypothetical: `imagesearch --index` emits PIL warnings (palette
    # transparency, DecompressionBomb) on stderr during any large index, which runs on every
    # nightly whose crawl succeeded. The parent would die at that line, so the crawl's real
    # exit code and the "refresh finished" line would both be lost.
    #
    # A native command's stderr is output, not a PowerShell error, so relax the preference
    # for exactly the length of the pipeline and put it back afterwards.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $file @cmdArgs 2>&1 |
            ForEach-Object { Write-LogLine ([string]$_); Write-Host $_ }
    } finally {
        $ErrorActionPreference = $prev
    }
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
    Write-Log "browsers: $(if ($env:PLAYWRIGHT_BROWSERS_PATH) { $env:PLAYWRIGHT_BROWSERS_PATH } else { 'default (%LOCALAPPDATA%\ms-playwright)' })"
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
