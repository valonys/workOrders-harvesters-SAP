<#
.SYNOPSIS
    Refresh CLV_Inspection.pbix from the IW38 dataset CSVs and publish/replace
    the workspace report.

.EXAMPLE
    .\refresh_clv_powerbi.ps1
    .\refresh_clv_powerbi.ps1 -UnlockDataset
    .\refresh_clv_powerbi.ps1 -OpenOnly
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
$reportName = $cfg.ClvReport
if (-not $WorkspaceName) { $WorkspaceName = [string]$cfg.Workspace }

$iw38 = Join-Path $env:USERPROFILE "OneDrive - TotalEnergies\IW38"
$dataset = Join-Path $iw38 "dataset"
$stablePbix = Join-Path $dataset "$reportName.pbix"
$downloadsPbix = Join-Path $env:USERPROFILE "Downloads\$reportName.pbix"
$stamp = Join-Path $dataset "CLV_powerbi_last_refresh_attempt.txt"

if ($UnlockDataset) {
    Close-PbiReport -ReportName $reportName
    Write-Host "Dataset unlocked for harvest (Desktop closed for $reportName)."
    exit 0
}

if (-not (Test-Path -LiteralPath $dataset)) {
    throw "Dataset folder missing: $dataset"
}

$source = $null
if ((Test-Path -LiteralPath $downloadsPbix) -and (Test-Path -LiteralPath $stablePbix)) {
    if ((Get-Item -LiteralPath $downloadsPbix).LastWriteTime -gt (Get-Item -LiteralPath $stablePbix).LastWriteTime) {
        $source = $downloadsPbix
    }
} elseif (Test-Path -LiteralPath $downloadsPbix) {
    $source = $downloadsPbix
}

if ($source) {
    Copy-Item -LiteralPath $source -Destination $stablePbix -Force
    Write-Host "Updated stable PBIX from $source -> $stablePbix"
} elseif (-not (Test-Path -LiteralPath $stablePbix)) {
    throw "No $reportName.pbix found in Downloads or $dataset"
}

@(
    "attempted_utc=$((Get-Date).ToUniversalTime().ToString('o'))"
    "pbix=$stablePbix"
    "fact=$(Join-Path $dataset 'CLV_wo_fact.csv')"
    "workspace=$WorkspaceName"
    "note=Refresh local CSVs into the PBIX, then publish/replace on the workspace."
) | Set-Content -LiteralPath $stamp -Encoding UTF8

Write-Host "CSV sources ready under $dataset"
Get-ChildItem $dataset -Filter "CLV_*.csv" | ForEach-Object {
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

$result = @(Invoke-PbiRefreshAndPublish @params) | Select-Object -Last 1
Write-Host ("Done. refreshed={0} published={1}" -f $result.Refreshed, $result.Published)
exit 0
