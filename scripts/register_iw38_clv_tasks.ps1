<#
.SYNOPSIS
    Register twice-daily IW38 CLV harvest (+ dataset rebuild) for Power BI.

.EXAMPLE
    .\register_iw38_clv_tasks.ps1
    .\register_iw38_clv_tasks.ps1 -Morning 07:30 -Evening 17:30
    .\register_iw38_clv_tasks.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Morning = "07:30",
    [string]$Evening = "17:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$register = Join-Path $PSScriptRoot "register_task.ps1"
$pipeline = Join-Path $PSScriptRoot "run_iw38_clv_pipeline.cmd"

if (-not (Test-Path $pipeline)) {
    throw "Missing $pipeline"
}

$tasks = @(
    @{ Name = "SAP IW38 CLV Morning"; Time = $Morning },
    @{ Name = "SAP IW38 CLV Evening"; Time = $Evening }
)

if ($Unregister) {
    foreach ($t in $tasks) {
        & $register -Unregister -TaskName $t.Name
    }
    return
}

# register_task.ps1 always points at run_export.cmd; for the CLV pipeline we
# register actions directly against run_iw38_clv_pipeline.cmd.
foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $pipeline -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -At $t.Time `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Registered '$($t.Name)' weekdays at $($t.Time) -> $pipeline"
}

Write-Host ""
Write-Host "Pipeline: IW39 CLV-PG2026 harvest -> dataset\CLV_*.csv -> open PBIX for Refresh."
Write-Host "For unattended Power BI updates, publish the PBIX to Power BI Service and schedule refresh there."
Write-Host "Requires interactive logon (SAP GUI Scripting)."
