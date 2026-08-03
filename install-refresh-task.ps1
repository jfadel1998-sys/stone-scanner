# Install (or update) the nightly Stone Scanner refresh as a Windows scheduled task.
# Idempotent: safe to run repeatedly — it replaces any existing StoneScannerRefresh.
# Run once in an ELEVATED PowerShell (right-click -> Run as administrator).
#
#   .\install-refresh-task.ps1              # nightly at 03:00
#   .\install-refresh-task.ps1 -At 02:30    # a different time
#   .\install-refresh-task.ps1 -Uninstall   # remove it

param(
    [string]$At = "03:00",
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
# — so the task always starts and the script's own existence becomes the check. Exit 3
# means "project not reachable", which the restart settings below then re-attempt in case
# the drive is plugged in later.
# -WindowStyle Hidden so the nightly run doesn't flash a console; the log is the record.
$guard = "`$s = '$script'; " +
         "if (Test-Path -LiteralPath `$s) { Set-Location -LiteralPath (Split-Path -LiteralPath `$s); & `$s } " +
         "else { exit 3 }"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden " +
               "-Command `"$guard`"")
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# Run under the current user so the task can reach this user's installed Playwright
# browsers and the project venv. Highest privileges avoids UAC prompts mid-run.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest
# RestartCount/Interval: a detached drive is usually detached for minutes, not the night,
# so retry across the next two hours rather than writing the day off. IgnoreNew keeps a
# retry from starting a second crawl on top of one that is already running.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 4 -RestartInterval (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Nightly refresh of the Stone Scanner catalog from public supplier catalogs." | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Installed '$TaskName' — daily at $At." -ForegroundColor Green
Write-Host "  Next run: $($info.NextRunTime)"
Write-Host "  Run now to test:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Log:  $(Join-Path $root 'refresh.log')"
Write-Host "  If LastTaskResult is 3, the project drive was not attached when it ran;"
Write-Host "  it retries 4 times at 30-minute intervals before giving up for the day."
