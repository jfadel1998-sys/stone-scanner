# Install (or update) the nightly Stone Scanner refresh as a Windows scheduled task.
# Idempotent: safe to run repeatedly — it replaces any existing StoneScannerRefresh.
# Run once in an ELEVATED PowerShell (right-click -> Run as administrator).
#
#   .\install-refresh-task.ps1                       # 03:00, 05:00 and 07:00
#   .\install-refresh-task.ps1 -At 02:30,04:30       # different times (the LAST one is the
#                                                    #   spill deadline — see below)
#   .\install-refresh-task.ps1 -WaitMinutes 30       # wait longer for a detached project drive
#   .\install-refresh-task.ps1 -Uninstall            # remove it

param(
    [string[]]$At = @("03:00", "05:00", "07:00"),
    [int]$WaitMinutes = 20,
    [string]$LocalCopy = (Join-Path $env:ProgramData "StoneScanner"),
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "StoneScannerRefresh"
$root     = $PSScriptRoot
$script   = Join-Path $root "refresh.ps1"
$spillExe = Join-Path $LocalCopy "StoneScanner.exe"

# VALIDATE BEFORE UNREGISTERING. This script removes the existing task and then registers a
# replacement, so anything that throws in between leaves the machine with NO task at all —
# which is how a mistyped parameter turned into a silently missing nightly once already. The
# trigger builder below casts each time to a [timespan] to roll a past one forward, so a
# value the cmdlet accepts but [timespan] cannot parse ("7:00 AM") would fail here in a
# hidden window at install time rather than in front of whoever typed it.
if (-not $At -or $At.Count -lt 1) { throw "-At needs at least one time, e.g. -At 03:00" }
foreach ($t in $At) {
    try { [void][timespan]$t } catch {
        throw "-At times must be HH:mm (24-hour), e.g. 03:00,05:00,07:00 — '$t' is not."
    }
}

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
#
# THE SPILL. Giving up is still losing the night, and the night has been lost twice already
# (2026-08-04 and 2026-08-05). So as soon as a run has waited out its deadline and the drive
# is still not there, it crawls into the local copy on C: instead of going home.
#
# ON THE FIRST TRIGGER, not the last. This reverses the original rule, deliberately. The old
# design deferred to 07:00 on the theory that D: often reappears by mid-morning and an early
# spill would race it — but that trades a whole known-lost night for the chance of a crawl
# that might not come. When the drive is deliberately away, 03:20 is four hours of daylight
# better than 07:20, and a spill that later turns out to have been unnecessary costs nothing:
# AIL-29's merge takes a supplier from the spill only when it is STRICTLY newer, so a normal
# D: crawl that happens afterwards simply supersedes it.
#
# ONCE A DAY, which is what makes the above safe. A crawl takes about three hours, so a spill
# starting at 03:20 finishes around 06:20 — leaving the 07:00 trigger free to start a SECOND
# full crawl of ~135 live supplier sites the same night. MultipleInstances=IgnoreNew does not
# help, because by then the first one has finished. The date stamp is what stops it, and it
# is written BEFORE the crawl rather than after: one attempt per day, predictable, and a
# spill that dies two hours in does not get to start again from the top at 07:00.
#
# START-PROCESS -Wait, NOT `& $spill`. StoneScanner.exe is built console=False, and the call
# operator does not wait for a GUI-subsystem process: measured against the real exe, `&`
# returned in 0.33s with the crawl still running and left $LASTEXITCODE EMPTY, while
# Start-Process -PassThru -Wait blocked for the full run and returned a real code. That is
# not cosmetic — it is what happened on the night of 2026-08-06, where the log read
#
#     03:20:01  SPILL - project drive absent; crawling into the local copy instead
#     03:20:01  SPILL finished, exit            <- one second later, no exit code
#
# while the crawl actually ran until 05:24. Three things follow from the task instance
# exiting while its crawl continues: LastTaskResult can never report the spill's outcome (a
# spill that CRASHED would log the identical line), ExecutionTimeLimit cannot bound a process
# the task is not tracking, and — the dangerous one — MultipleInstances=IgnoreNew stops
# protecting anything, because by the next trigger the scheduler sees nothing running. On
# 08-06 only the once-a-day stamp stood between that and three concurrent crawls of ~135
# live supplier sites.
#
# THE SPILL WRITES NOTHING TO D:. -WorkingDirectory is the local copy, so the exe's app_dir()
# resolves there and desktop.setup_env writes ITS database, log and refresh_runs ledger.
# D: is absent in this branch by construction, but the branch must stay correct for the case
# where it comes back mid-crawl.
# -WindowStyle Hidden so the nightly run doesn't flash a console; the log is the record.
$notReachable = 200
$guard = @(
    "`$s = '$script'"
    "`$spill = '$spillExe'"
    "`$stamp = Join-Path `$env:ProgramData 'StoneScanner\last-spill.txt'"
    "`$log = Join-Path `$env:ProgramData 'StoneScanner\refresh-task.log'"
    # Hand the log to the crawl process so it can report its own phases here. Without this
    # the spill path wrote one SPILL line and then nothing for hours, because this script
    # only regains control when the process exits — which is what made a healthy nightly
    # (crawl done 05:27, image indexing until 08:1x) read as wedged.
    "`$env:STONESCAN_TASK_LOG = `$log"
    "`$d = Split-Path -LiteralPath `$log"
    "if (-not (Test-Path -LiteralPath `$d)) { New-Item -ItemType Directory -Path `$d -Force | Out-Null }"
    "function Note([string]`$m) { Add-Content -LiteralPath `$log -Encoding utf8 -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + `$m) }"
    "function GiveUp { if (-not (Test-Path -LiteralPath `$spill)) { Note ('GAVE UP after $WaitMinutes min - project not reachable and no local copy at ' + `$spill); exit $notReachable }; `$today = (Get-Date).ToString('yyyy-MM-dd'); if ((Test-Path -LiteralPath `$stamp) -and ((Get-Content -LiteralPath `$stamp -Raw -ErrorAction SilentlyContinue) -replace '\s','') -eq `$today) { Note ('GAVE UP after $WaitMinutes min - project not reachable; already spilled today, not crawling again'); exit $notReachable }; Set-Content -LiteralPath `$stamp -Value `$today -Encoding utf8; Note ('SPILL - project drive absent; crawling into the local copy instead: ' + `$spill); `$proc = Start-Process -FilePath `$spill -ArgumentList '--refresh' -WorkingDirectory (Split-Path -LiteralPath `$spill) -PassThru -Wait; `$sc = if (`$null -ne `$proc) { `$proc.ExitCode } else { 1 }; Note ('SPILL finished, exit ' + `$sc); exit `$sc }"
    "`$deadline = (Get-Date).AddMinutes($WaitMinutes)"
    "while (-not (Test-Path -LiteralPath `$s)) { if ((Get-Date) -ge `$deadline) { GiveUp }; Start-Sleep -Seconds 30 }"
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
# One trigger per time, all daily. THE TRIGGERS ARE THE RETRY MECHANISM — not
# -RestartCount, which we believed was covering this and is not. On 2026-08-05 the run gave
# up at 03:23:48 with exit 200 and no restart ever fired: refresh-task.log holds exactly one
# line where four attempts would have left four. Task Scheduler does not reliably treat a
# clean non-zero exit from the action as a failure worth restarting, so a mechanism that only
# runs when it decides one occurred cannot be relied on for the case that keeps happening.
# A fresh trigger has no such opinion. IgnoreNew (below) is what stops them overlapping.
#
# Each trigger is dated to its NEXT occurrence, not to today. `-At "03:00"` builds a trigger
# whose start boundary is today at 03:00 — already in the past by the time anyone installs
# this — and -StartWhenAvailable (below) exists precisely to run missed triggers, so it fires
# one immediately. That was survivable with a single trigger and merely surprising; with
# three it means installing at any time after 07:00 kicks off a three-hour crawl of ~135 live
# supplier sites that nobody asked for. Rolling a past time to tomorrow makes registering the
# task an inert act, which is what someone running an installer expects it to be.
$now = Get-Date
$trigger = @(foreach ($t in $At) {
    $when = $now.Date.Add([timespan]$t)
    if ($when -le $now) { $when = $when.AddDays(1) }
    New-ScheduledTaskTrigger -Daily -At $when
})
# Run under the current user so the task can reach this user's installed Playwright
# browsers and the project venv. Highest privileges avoids UAC prompts mid-run.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest
# RestartCount/Interval are kept as belt-and-braces for the case they DO cover (an action
# that dies hard), but nothing depends on them any more — see the trigger comment above.
# IgnoreNew is load-bearing and always was: the 03:00 run can still be inside its 20-minute
# wait when 05:00 fires, and two guards waiting for the same drive would race into two
# crawls the moment it appeared.
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
Write-Host "Installed '$TaskName' — daily at $($At -join ', ')." -ForegroundColor Green
Write-Host "  Next run: $($info.NextRunTime)"
Write-Host "  Run now to test:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Crawl log: $(Join-Path $root 'refresh.log')  (on the project drive)"
Write-Host "  Task log:  $(Join-Path $env:ProgramData 'StoneScanner\refresh-task.log')  (always on C:)"
Write-Host "  Each run waits up to $WaitMinutes min for the project drive. If it never shows up,"
Write-Host "  the FIRST run of the day ($($At[0])) stops waiting and"
if (Test-Path -LiteralPath $spillExe) {
    Write-Host "  crawls into the local copy instead ($spillExe)."
    Write-Host "  Later triggers that day see the stamp in $(Join-Path $env:ProgramData 'StoneScanner\last-spill.txt')"
    Write-Host "  and exit $notReachable rather than starting a second crawl."
} else {
    Write-Host "  WOULD crawl into the local copy, but there isn't one at $spillExe yet." -ForegroundColor Yellow
    Write-Host "  Run .\build_exe.ps1 to create it — until then a lost drive is still a lost night." -ForegroundColor Yellow
}
Write-Host "  Any non-zero code other than $notReachable came from the crawl. The task log says which."
