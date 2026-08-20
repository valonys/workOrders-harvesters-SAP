<#
.SYNOPSIS
    Refresh the CLV Inspection Power BI report after the IW38 dataset CSVs update.

.DESCRIPTION
    Preferred production path: publish CLV_Inspection.pbix to Power BI Service and
    schedule dataset refresh there (twice daily), with the CSV folder on OneDrive
    or a gateway. This script covers the local Desktop fallback:

      1. Ensures a stable PBIX copy under IW38\dataset\
      2. Opens Power BI Desktop on that file (user must click Refresh once if
         auto-refresh COM is unavailable)
      3. Writes a small stamp file so Task Scheduler logs show the attempt

.EXAMPLE
    .\refresh_clv_powerbi.ps1
    .\refresh_clv_powerbi.ps1 -OpenOnly
#>
[CmdletBinding()]
param(
    [switch]$OpenOnly,
    [switch]$SkipOpen
)

$ErrorActionPreference = "Stop"

$iw38 = Join-Path $env:USERPROFILE "OneDrive - TotalEnergies\IW38"
$dataset = Join-Path $iw38 "dataset"
$stablePbix = Join-Path $dataset "CLV_Inspection.pbix"
$downloadsPbix = Join-Path $env:USERPROFILE "Downloads\CLV_Inspection.pbix"
$stamp = Join-Path $dataset "CLV_powerbi_last_refresh_attempt.txt"

if (-not (Test-Path $dataset)) {
    throw "Dataset folder missing: $dataset"
}

# Prefer the newest draft (Downloads vs dataset).
$source = $null
if ((Test-Path $downloadsPbix) -and (Test-Path $stablePbix)) {
    if ((Get-Item $downloadsPbix).LastWriteTime -gt (Get-Item $stablePbix).LastWriteTime) {
        $source = $downloadsPbix
    }
} elseif (Test-Path $downloadsPbix) {
    $source = $downloadsPbix
}

if ($source) {
    Copy-Item -LiteralPath $source -Destination $stablePbix -Force
    Write-Host "Updated stable PBIX from $source -> $stablePbix"
} elseif (-not (Test-Path $stablePbix)) {
    throw "No CLV_Inspection.pbix found in Downloads or $dataset"
}

@(
    "attempted_utc=$((Get-Date).ToUniversalTime().ToString('o'))"
    "pbix=$stablePbix"
    "fact=$(Join-Path $dataset 'CLV_wo_fact.csv')"
    "note=Local Desktop cannot schedule a silent refresh reliably; use Power BI Service scheduled refresh for unattended twice-daily updates. This script opens the PBIX so a logged-on user can Refresh, or confirms CSVs are ready."
) | Set-Content -LiteralPath $stamp -Encoding UTF8

Write-Host "CSV sources ready under $dataset"
Get-ChildItem $dataset -Filter "CLV_*.csv" | ForEach-Object {
    Write-Host ("  {0}  {1:yyyy-MM-dd HH:mm}" -f $_.Name, $_.LastWriteTime)
}

if ($SkipOpen) {
    Write-Host "SkipOpen set — not launching Power BI Desktop."
    exit 0
}

$pbi = @(
    "${env:ProgramFiles}\Microsoft Power BI Desktop\bin\PBIDesktop.exe",
    "${env:ProgramFiles}\Microsoft Power BI Desktop\PBIDesktop.exe",
    "${env:LOCALAPPDATA}\Microsoft\WindowsApps\PBIDesktopStore.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $pbi) {
    Write-Warning "Power BI Desktop executable not found. Open $stablePbix manually and click Refresh."
    exit 0
}

Write-Host "Opening Power BI Desktop: $stablePbix"
Start-Process -FilePath $pbi -ArgumentList "`"$stablePbix`""
if (-not $OpenOnly) {
    Write-Host "Click Home -> Refresh in Power BI Desktop (or rely on Service scheduled refresh)."
}
exit 0
