<#
.SYNOPSIS
    Registers the daily IW29 export as a Windows scheduled task.

.DESCRIPTION
    SAP GUI Scripting drives a real, visible SAP GUI, so the task must run in
    the interactive session of a logged-on user. "Run whether user is logged on
    or not" starts the task in session 0 where there is no desktop, and the
    export will fail. That is why this registers an interactive task instead.

.EXAMPLE
    .\register_task.ps1 -Time 06:00
    .\register_task.ps1 -Time 06:00 -Arguments "run --days 1" -Unregister:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SAP IW29 Export",
    [string]$Time = "06:00",
    [string]$Arguments = "run",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_export.cmd"

if (-not (Test-Path $runner)) {
    throw "Cannot find $runner"
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

$action = New-ScheduledTaskAction -Execute $runner -Argument $Arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName' to run $runner $Arguments daily at $Time."
Write-Host "It only runs while $env:USERNAME is logged on, which SAP GUI Scripting requires."
