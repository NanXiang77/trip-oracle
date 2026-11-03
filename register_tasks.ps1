# Register Windows scheduled tasks for trip-oracle.
# Re-runnable: every run overwrites the same task names (-Force).
# Remove all:  Unregister-ScheduledTask -TaskName TripOracle-* -Confirm:$false
# NOTE: keep this file ASCII-only.

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$collect = Join-Path $dir "collect_daily.bat"
$watch = Join-Path $dir "run_watch.bat"

foreach ($f in @($collect, $watch)) {
    if (-not (Test-Path $f)) { throw "missing file: $f" }
}

function Register-DailyCollect {
    param([string]$Name, [string]$At, [string]$Desc, [string]$Mode = "")
    $arg = if ($Mode) { "/c `"$collect`" $Mode" } else { "/c `"$collect`"" }
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arg -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Desc -Force | Out-Null
    Write-Output ("[ok] {0}  daily at {1} {2}" -f $Name, $At, $Mode)
}

# Morning: 2-day window only (the "today column" needs today's ticket state; ~40s).
Register-DailyCollect -Name "TripOracle-Collect-08" -At "08:00" -Mode "fast" `
    -Desc "trip-oracle: morning fast collect (2 days, no heat) at 08:00"
# Evening: full 8-day window + heat index + push (~5 min, nobody is waiting).
Register-DailyCollect -Name "TripOracle-Collect-20" -At "20:00" `
    -Desc "trip-oracle: full collect (8 days + heat) at 20:00"

# Watch: every 2 hours from 2026-12-17 06:00 (ticket presale opens 12/17), ends 2027-01-05.
$wAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$watch`" --once" -WorkingDirectory $dir
$wStart = Get-Date "2026-12-17 06:00:00"
$wTrigger = New-ScheduledTaskTrigger -Once -At $wStart `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration (New-TimeSpan -Days 19)
$wSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "TripOracle-Watch-2h" -Action $wAction -Trigger $wTrigger `
    -Settings $wSettings -Description "trip-oracle: winter-holiday ticket watch, every 2h 12/17-01/05" -Force | Out-Null
Write-Output ("[ok] TripOracle-Watch-2h  every 2h from {0:yyyy-MM-dd HH:mm} (19 days)" -f $wStart)

Write-Output ""
Write-Output "current tasks:"
Get-ScheduledTask -TaskName "TripOracle-*" | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    Write-Output ("  {0,-24} {1,-8} next={2}" -f $_.TaskName, $_.State, $info.NextRunTime)
}
