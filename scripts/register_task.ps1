<#
.SYNOPSIS
    Registers a SAP harvest job as a Windows scheduled task.

.DESCRIPTION
    SAP GUI Scripting drives a real, visible SAP GUI, so the task must run in
    the interactive session of a logged-on user. "Run whether user is logged on
    or not" starts the task in session 0 where there is no desktop, and the
    export will fail. That is why this registers an interactive task instead.

.EXAMPLE
    .\register_task.ps1 -Time 12:30 -Weekdays
    .\register_task.ps1 -Time 08:00 -Days Wednesday -TaskName "SAP IW22 Attachments" -Arguments "iw22-attachments"
    .\register_task.ps1 -Time 12:00 -DailyForDays 14 -TaskName "SAP IW22 Attachments Noon" -Arguments "iw22-attachments"
    .\register_task.ps1 -Unregister -TaskName "SAP IW22 Attachments"
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SAP IW29 Export",
    [string]$Time = "06:00",
    [string]$Arguments = "run",
    [switch]$Weekdays,
    [ValidateSet("Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday")]
    [string[]]$Days = @(),
    # When > 0, register a daily trigger that ends after this many calendar days
    # (inclusive of today). Example: 14 = today through today+13.
    [int]$DailyForDays = 0,
    [datetime]$EndDate = [datetime]::MinValue,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_export.cmd"

if (-not (Test-Path $runner)) {
    throw "Cannot find $runner"
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$action = New-ScheduledTaskAction -Execute $runner -Argument $Arguments -WorkingDirectory $root

$endBoundary = $null
if ($DailyForDays -gt 0) {
    $endBoundary = (Get-Date).Date.AddDays($DailyForDays).AddDays(-1).AddHours(23).AddMinutes(59).AddSeconds(59)
} elseif ($EndDate -gt [datetime]::MinValue) {
    $endBoundary = $EndDate
}

if ($Days.Count -gt 0) {
    $trigger = New-ScheduledTaskTrigger -Weekly -At $Time -DaysOfWeek $Days
    $when = "every $($Days -join ', ') at $Time"
} elseif ($Weekdays) {
    $trigger = New-ScheduledTaskTrigger -Weekly -At $Time `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday
    $when = "every weekday at $Time"
} else {
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $when = "daily at $Time"
}

if ($null -ne $endBoundary) {
    $trigger.EndBoundary = $endBoundary.ToString("s")
    $when = "$when until $($endBoundary.ToString('yyyy-MM-dd'))"
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName' to run $runner $Arguments $when."
Write-Host "It only runs while $env:USERNAME is logged on, which SAP GUI Scripting requires."
