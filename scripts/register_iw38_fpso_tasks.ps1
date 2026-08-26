<#
.SYNOPSIS
    Register twice-daily IW38 harvest for all four FPSOs (GIR DAL PAZ CLV).

.DESCRIPTION
    Replaces the CLV-only morning/evening tasks with the all-four FPSO pipeline
    so one schedule feeds FPSO_wo_fact.csv for the shared Power BI report.

.EXAMPLE
    .\register_iw38_fpso_tasks.ps1
    .\register_iw38_fpso_tasks.ps1 -Morning 07:30 -Evening 17:30
    .\register_iw38_fpso_tasks.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Morning = "07:30",
    [string]$Evening = "17:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pipeline = Join-Path $PSScriptRoot "run_iw38_fpso_pipeline.cmd"

if (-not (Test-Path $pipeline)) {
    throw "Missing $pipeline"
}

$fpsoTasks = @(
    @{ Name = "SAP IW38 FPSO Morning"; Time = $Morning },
    @{ Name = "SAP IW38 FPSO Evening"; Time = $Evening }
)
$legacyClv = @(
    "SAP IW38 CLV Morning",
    "SAP IW38 CLV Evening"
)

if ($Unregister) {
    foreach ($t in $fpsoTasks) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Unregistered $($t.Name)"
    }
    return
}

# Avoid double harvest: drop CLV-only tasks if present.
foreach ($name in $legacyClv) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed legacy '$name' (replaced by FPSO all-four schedule)."
    }
}

foreach ($t in $fpsoTasks) {
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
        -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Registered '$($t.Name)' weekdays at $($t.Time) -> $pipeline"
}

Write-Host ""
Write-Host "Pipeline: IW39 GIR+DAL+PAZ+CLV -> dataset\FPSO_wo_fact.csv (+ summary/matrix)"
Write-Host "Power BI: refresh FPSO_Inspection.pbix from FPSO_wo_fact.csv, then publish/replace."
Write-Host "Set [powerbi].workspace in config.toml if REST overwrite should target a named workspace."
Write-Host "Requires interactive logon (SAP GUI + Power BI Desktop)."
