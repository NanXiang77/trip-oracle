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
    param([string]$Name, [string]$At, [string]$Desc)
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$collect`"" -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Description $Desc -Force | Out-Null
    Write-Output ("[ok] {0}  daily at {1}" -f $Name, $At)
}

Register-DailyCollect -Name "TripOracle-Collect-08" -At "08:00" -Desc "trip-oracle: collect 12306 / weather / heat at 08:00"
Register-DailyCollect -Name "TripOracle-Collect-20" -At "20:00" -Desc "trip-oracle: collect 12306 / weather / heat at 20:00"

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
