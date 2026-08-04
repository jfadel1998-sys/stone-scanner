# Install (or update) the nightly Stone Scanner refresh as a Windows scheduled task.
# Idempotent: safe to run repeatedly — it replaces any existing StoneScannerRefresh.
# Run once in an ELEVATED PowerShell (right-click -> Run as administrator).
#
#   .\install-refresh-task.ps1                  # nightly at 03:00
#   .\install-refresh-task.ps1 -At 02:30        # a different time
#   .\install-refresh-task.ps1 -WaitMinutes 30  # wait longer for a detached project drive
#   .\install-refresh-task.ps1 -Uninstall       # remove it

param(
    [string]$At = "03:00",
    [int]$WaitMinutes = 20,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "StoneScannerRefresh"
$root     = $PSScriptRoot
$script   = Join-Path $root "refresh.ps1"

# Admin is required to register a task that runs whether or not you're logged in.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs an elevated PowerShell. Right-click PowerShell -> Run as administrator, then re-run." -ForegroundColor Yellow
    exit 1
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing '$TaskName'."
}
if ($Uninstall) {
    Write-Host "Done — '$TaskName' is not installed."
    exit 0
}

if (-not (Test-Path $script)) { throw "refresh.ps1 not found next to this script ($script)." }

# The project can live on an external drive. If it is detached when the trigger fires,
# an action of "-File <path on that drive>" with a matching -WorkingDirectory cannot even
# START: Task Scheduler fails it with 0x8007010B (ERROR_DIRECTORY) before PowerShell is
# launched, so refresh.ps1 never runs, nothing reaches the log, and the only evidence is
# a hex code in LastTaskResult. Keep the action entirely on the system drive instead —
# powershell.exe, no -WorkingDirectory, and the script path passed as *data* to -Command
# — so the task always starts and the script's own existence becomes the check.
#
# A removable drive is usually absent for minutes, not the night, so the guard WAITS for it
# (polling every 30s up to -WaitMinutes) before giving up. Two rules learned from the night
# of 2026-08-04, when the drive was gone at 03:00 and every retry failed:
#   * Log to %ProgramData%, NEVER to the project drive. refresh.log lives on the very drive
#     that isn't there, so a missed night left zero on-disk evidence — the only trace was a
#     number in LastTaskResult. Now every run leaves a line on C: whatever happens.
#   * Give up with 200, not a small number. refresh.ps1 propagates the crawl's own exit code,
#     so the old sentinel `exit 3` was indistinguishable from "the crawl itself exited 3".
# -WindowStyle Hidden so the nightly run doesn't flash a console; the log is the record.
$notReachable = 200
$guard = @(
    "`$s = '$script'"
    "`$log = Join-Path `$env:ProgramData 'StoneScanner\refresh-task.log'"
    "`$d = Split-Path -LiteralPath `$log"
    "if (-not (Test-Path -LiteralPath `$d)) { New-Item -ItemType Directory -Path `$d -Force | Out-Null }"
    "function Note([string]`$m) { Add-Content -LiteralPath `$log -Encoding utf8 -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + `$m) }"
    "`$deadline = (Get-Date).AddMinutes($WaitMinutes)"
    "while (-not (Test-Path -LiteralPath `$s)) { if ((Get-Date) -ge `$deadline) { Note ('GAVE UP after $WaitMinutes min - project not reachable: ' + `$s); exit $notReachable }; Start-Sleep -Seconds 30 }"
    "Note ('starting ' + `$s)"
    "Set-Location -LiteralPath (Split-Path -LiteralPath `$s)"
    "& `$s"
    "`$c = `$LASTEXITCODE"
    "Note ('refresh.ps1 exited ' + `$c)"
    "exit `$c"
) -join '; '
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden " +
               "-Command `"$guard`"")
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# Run under the current user so the task can reach this user's installed Playwright
# browsers and the project venv. Highest privileges avoids UAC prompts mid-run.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest
# RestartCount/Interval: retries across the next two hours back the guard's own wait up,
# for a drive that stays away longer than -WaitMinutes. IgnoreNew keeps a retry from
# starting a second crawl on top of one that is already running (or still waiting).
# ExecutionTimeLimit covers the guard's wait PLUS the crawl (~3h on the full list) — at the
# old 4h a slow night would be killed mid-crawl, which is one more failure that looks like
# nothing happened. The battery settings default to "don't start / stop if on battery" and
# would silently skip the run on anything with a battery; this machine has none, so it has
# never bitten, but a skipped run is exactly the invisible failure this task keeps having.
# NOTE the inversion: the cmdlet takes the POSITIVE switches -AllowStartIfOnBatteries /
# -DontStopIfGoingOnBatteries, while the object it returns (and Get-ScheduledTask) exposes
# the NEGATIVE properties DisallowStartIfOnBatteries / StopIfGoingOnBatteries. Passing the
# property names as parameters is a ParameterBindingException, and because the installer
# unregisters before it registers, that failure leaves NO task at all.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -RestartCount 4 -RestartInterval (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Nightly refresh of the Stone Scanner catalog from public supplier catalogs." | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Installed '$TaskName' — daily at $At." -ForegroundColor Green
Write-Host "  Next run: $($info.NextRunTime)"
Write-Host "  Run now to test:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Crawl log: $(Join-Path $root 'refresh.log')  (on the project drive)"
Write-Host "  Task log:  $(Join-Path $env:ProgramData 'StoneScanner\refresh-task.log')  (always on C:)"
Write-Host "  Waits up to $WaitMinutes min for the project drive, then retries 4 times at"
Write-Host "  30-minute intervals. LastTaskResult $notReachable means it never showed up;"
Write-Host "  any other non-zero code came from the crawl itself. The task log says which."
