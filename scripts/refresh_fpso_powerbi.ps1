<#
.SYNOPSIS
    Refresh FPSO_Inspection.pbix from FPSO_wo_fact.csv and publish/replace
    the workspace report so consumers see the new IW38 harvest.

.DESCRIPTION
    Power BI Desktop has no supported COM refresh/publish API. This script:
      1. Closes a leftover Desktop session so harvest can overwrite the CSVs
         (use -UnlockDataset from the pipeline, before IW38).
      2. Opens the stable PBIX under IW38\dataset\
      3. Refreshes the model (local Analysis Services, else Home > Refresh)
      4. Saves, then publishes: REST overwrite if a token is available, else
         Desktop Publish with Select / Replace / Got it clicked automatically

.EXAMPLE
    .\refresh_fpso_powerbi.ps1
    .\refresh_fpso_powerbi.ps1 -UnlockDataset
    .\refresh_fpso_powerbi.ps1 -SkipPublish
#>
[CmdletBinding()]
param(
    [switch]$OpenOnly,
    [switch]$SkipOpen,
    [switch]$SkipRefresh,
    [switch]$SkipPublish,
    [switch]$LeaveOpen,
    [switch]$UnlockDataset,
    [string]$WorkspaceName = ""
)

$ErrorActionPreference = "Stop"

if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne 'STA') {
    $extra = @()
    foreach ($key in $PSBoundParameters.Keys) {
        $val = $PSBoundParameters[$key]
        if ($val -is [System.Management.Automation.SwitchParameter]) {
            if ($val.IsPresent) { $extra += "-$key" }
        } else {
            $extra += "-$key"
            $extra += [string]$val
        }
    }
    $p = Start-Process -FilePath "powershell.exe" -ArgumentList (
        @('-STA', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath) + $extra
    ) -Wait -PassThru -NoNewWindow
    exit $p.ExitCode
}

. (Join-Path $PSScriptRoot "pbi_automate.ps1")

$repoRoot = Split-Path -Parent $PSScriptRoot
$cfg = Get-PbiPipelineConfig -RepoRoot $repoRoot
$reportName = $cfg.FpsoReport
if (-not $WorkspaceName) { $WorkspaceName = [string]$cfg.Workspace }

$iw38 = Join-Path $env:USERPROFILE "OneDrive - TotalEnergies\IW38"
$dataset = Join-Path $iw38 "dataset"
$stablePbix = Join-Path $dataset "$reportName.pbix"
$clvPbix = Join-Path $dataset "CLV_Inspection.pbix"
$downloadsPbix = Join-Path $env:USERPROFILE "Downloads\$reportName.pbix"
$stamp = Join-Path $dataset "FPSO_powerbi_last_refresh_attempt.txt"

if ($UnlockDataset) {
    Close-PbiReport -ReportName $reportName
    Write-Host "Dataset unlocked for harvest (Desktop closed for $reportName)."
    exit 0
}

if (-not (Test-Path -LiteralPath $dataset)) {
    throw "Dataset folder missing: $dataset"
}

if (Test-Path -LiteralPath $downloadsPbix) {
    if (-not (Test-Path -LiteralPath $stablePbix) -or
        ((Get-Item -LiteralPath $downloadsPbix).LastWriteTime -gt (Get-Item -LiteralPath $stablePbix).LastWriteTime)) {
        Copy-Item -LiteralPath $downloadsPbix -Destination $stablePbix -Force
        Write-Host "Updated stable PBIX from $downloadsPbix"
    }
} elseif (-not (Test-Path -LiteralPath $stablePbix)) {
    if (Test-Path -LiteralPath $clvPbix) {
        Copy-Item -LiteralPath $clvPbix -Destination $stablePbix -Force
        Write-Host "Seeded $reportName.pbix from CLV template -> $stablePbix"
        Write-Host "Point the fact table at FPSO_wo_fact.csv and add a Site slicer if this is the first run."
    } else {
        throw "No $reportName.pbix or CLV_Inspection.pbix found under $dataset"
    }
}

@(
    "attempted_utc=$((Get-Date).ToUniversalTime().ToString('o'))"
    "pbix=$stablePbix"
    "fact=$(Join-Path $dataset 'FPSO_wo_fact.csv')"
    "workspace=$WorkspaceName"
    "note=Refresh FPSO_wo_fact.csv into the PBIX, then publish/replace on the workspace."
) | Set-Content -LiteralPath $stamp -Encoding UTF8

Write-Host "CSV sources ready under $dataset"
Get-ChildItem $dataset -Filter "FPSO_*.csv" | ForEach-Object {
    Write-Host ("  {0}  {1:yyyy-MM-dd HH:mm}" -f $_.Name, $_.LastWriteTime)
}

if ($SkipOpen) {
    Write-Host "SkipOpen set - not launching Power BI Desktop."
    exit 0
}

$doPublish = $cfg.Publish -and -not $SkipPublish -and -not $OpenOnly
$doRefresh = -not $SkipRefresh -and -not $OpenOnly
$leave = $LeaveOpen -or $OpenOnly -or (-not $cfg.CloseAfter)

$params = @{
    PbixPath         = $stablePbix
    ReportName       = $reportName
    WorkspaceName    = $WorkspaceName
    OpenTimeoutS     = [int]$cfg.OpenTimeoutS
    RefreshTimeoutS  = [int]$cfg.RefreshTimeoutS
    PublishTimeoutS  = [int]$cfg.PublishTimeoutS
}
if (-not $doRefresh) { $params.SkipRefresh = $true }
if (-not $doPublish) { $params.SkipPublish = $true }
if ($leave) { $params.LeaveOpen = $true }

$fpsoFact = Join-Path $dataset "FPSO_wo_fact.csv"
$clvFact = Join-Path $dataset "CLV_wo_fact.csv"
if (Test-Path -LiteralPath $fpsoFact) {
    Copy-Item -LiteralPath $fpsoFact -Destination $clvFact -Force
    Write-Host "Mirrored FPSO_wo_fact.csv -> CLV_wo_fact.csv (PBIX query name)."
}

$result = @(Invoke-PbiRefreshAndPublish @params) | Select-Object -Last 1
Write-Host ("Done. refreshed={0} published={1}" -f $result.Refreshed, $result.Published)
exit 0
