<#
.SYNOPSIS
    Open the combined FPSO Inspection Power BI report after dataset CSVs update.

.DESCRIPTION
    Ensures FPSO_Inspection.pbix exists under IW38\dataset\ (seeded from the CLV
    template on first run). Opens Desktop so you can click Refresh.
    For unattended consumer updates: publish to Power BI Service and schedule refresh.

.EXAMPLE
    .\refresh_fpso_powerbi.ps1
    .\refresh_fpso_powerbi.ps1 -SkipOpen
#>
[CmdletBinding()]
param(
    [switch]$OpenOnly,
    [switch]$SkipOpen
)

$ErrorActionPreference = "Stop"

$iw38 = Join-Path $env:USERPROFILE "OneDrive - TotalEnergies\IW38"
$dataset = Join-Path $iw38 "dataset"
$stablePbix = Join-Path $dataset "FPSO_Inspection.pbix"
$clvPbix = Join-Path $dataset "CLV_Inspection.pbix"
$downloadsPbix = Join-Path $env:USERPROFILE "Downloads\FPSO_Inspection.pbix"
$stamp = Join-Path $dataset "FPSO_powerbi_last_refresh_attempt.txt"

if (-not (Test-Path $dataset)) {
    throw "Dataset folder missing: $dataset"
}

# Prefer Downloads draft, else seed from CLV template if FPSO pbix missing.
if (Test-Path $downloadsPbix) {
    if (-not (Test-Path $stablePbix) -or
        ((Get-Item $downloadsPbix).LastWriteTime -gt (Get-Item $stablePbix).LastWriteTime)) {
        Copy-Item -LiteralPath $downloadsPbix -Destination $stablePbix -Force
        Write-Host "Updated stable PBIX from $downloadsPbix"
    }
} elseif (-not (Test-Path $stablePbix)) {
    if (Test-Path $clvPbix) {
        Copy-Item -LiteralPath $clvPbix -Destination $stablePbix -Force
        Write-Host "Seeded FPSO_Inspection.pbix from CLV template -> $stablePbix"
        Write-Host "Next: point the fact table at FPSO_wo_fact.csv and add a Site slicer."
    } else {
        throw "No FPSO_Inspection.pbix or CLV_Inspection.pbix found under $dataset"
    }
}

@(
    "attempted_utc=$((Get-Date).ToUniversalTime().ToString('o'))"
    "pbix=$stablePbix"
    "fact=$(Join-Path $dataset 'FPSO_wo_fact.csv')"
    "note=Use FPSO_wo_fact.csv as the single fact; Site slicer filters GIR/DAL/PAZ/CLV."
) | Set-Content -LiteralPath $stamp -Encoding UTF8

Write-Host "CSV sources ready under $dataset"
Get-ChildItem $dataset -Filter "FPSO_*.csv" | ForEach-Object {
    Write-Host ("  {0}  {1:yyyy-MM-dd HH:mm}" -f $_.Name, $_.LastWriteTime)
}

if ($SkipOpen) {
    Write-Host "SkipOpen set - not launching Power BI Desktop."
    exit 0
}

$pbi = @(
    "${env:ProgramFiles}\Microsoft Power BI Desktop\bin\PBIDesktop.exe",
    "${env:ProgramFiles}\Microsoft Power BI Desktop\PBIDesktop.exe",
    "${env:LOCALAPPDATA}\Microsoft\WindowsApps\PBIDesktopStore.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $pbi) {
    Write-Warning "Power BI Desktop not found. Open $stablePbix manually and click Refresh."
    exit 0
}

Write-Host "Opening Power BI Desktop: $stablePbix"
Start-Process -FilePath $pbi -ArgumentList "`"$stablePbix`""
if (-not $OpenOnly) {
    Write-Host "Click Home > Refresh after pointing source at FPSO_wo_fact.csv + Site slicer."
}
exit 0
