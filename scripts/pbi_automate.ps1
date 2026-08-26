# Shared Power BI Desktop helper: refresh the open model from CSV, save, then
# publish/replace on the workspace without a person clicking through dialogs.
# Dot-source from refresh_fpso_powerbi.ps1 / refresh_clv_powerbi.ps1.

Set-StrictMode -Version Latest

function Read-TomlSection {
    param(
        [string]$Path,
        [string]$Section
    )
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $map
    }
    $in = $false
    foreach ($line in Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) {
        $text = $line.Trim()
        if ($text -eq "" -or $text.StartsWith("#")) { continue }
        if ($text -match '^\[(.+)\]$') {
            $in = ($Matches[1] -eq $Section)
            continue
        }
        if (-not $in) { continue }
        if ($text -match '^([A-Za-z0-9_]+)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2 -and (
                    ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                    ($value.StartsWith("'") -and $value.EndsWith("'"))
                )) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $map[$key] = $value
        }
    }
    return $map
}

function Get-PbiPipelineConfig {
    param([string]$RepoRoot)
    $path = Join-Path $RepoRoot "config.toml"
    $raw = Read-TomlSection -Path $path -Section "powerbi"
    $get = {
        param($map, $key, $default)
        if ($map -and $map.ContainsKey($key) -and $map[$key] -ne "") { return $map[$key] }
        return $default
    }
    $asBool = {
        param($value, $default)
        if ($null -eq $value -or $value -eq "") { return [bool]$default }
        return @("true", "1", "yes") -contains ([string]$value).Trim().ToLowerInvariant()
    }
    $asInt = {
        param($value, $default)
        $parsed = 0
        if ([int]::TryParse([string]$value, [ref]$parsed) -and $parsed -gt 0) { return $parsed }
        return [int]$default
    }
    $workspace = [string](& $get $raw "workspace" "")
    if ($env:PBI_WORKSPACE) { $workspace = $env:PBI_WORKSPACE }
    return [pscustomobject]@{
        Workspace         = $workspace
        FpsoReport        = [string](& $get $raw "fpso_report" "FPSO_Inspection")
        ClvReport         = [string](& $get $raw "clv_report" "CLV_Inspection")
        Publish           = & $asBool (& $get $raw "publish" "true") $true
        CloseAfter        = & $asBool (& $get $raw "close_after" "true") $true
        OpenTimeoutS      = & $asInt (& $get $raw "open_timeout_s" 180) 180
        RefreshTimeoutS   = & $asInt (& $get $raw "refresh_timeout_s" 600) 600
        PublishTimeoutS   = & $asInt (& $get $raw "publish_timeout_s" 180) 180
    }
}

function Initialize-Uia {
    if (Get-Variable -Name PbiUiaReady -Scope Script -ErrorAction SilentlyContinue) {
        return
    }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName System.Windows.Forms
    $script:PbiUiaReady = $true
}

function Get-PbiDesktopProcesses {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -match '^(PBIDesktop|PBIDesktopStore)$' }
}

function Get-PbiProcessForReport {
    param([string]$ReportName)
    $needle = ($ReportName -replace '\.pbix$', '').ToLowerInvariant()
    foreach ($proc in Get-PbiDesktopProcesses) {
        $title = [string]$proc.MainWindowTitle
        if ($title -and $title.ToLowerInvariant().Contains($needle)) {
            return $proc
        }
    }
    return $null
}

function Get-PbiExePath {
    $running = Get-PbiDesktopProcesses | Where-Object { $_.Path } | Select-Object -First 1
    if ($running -and (Test-Path -LiteralPath $running.Path)) {
        return $running.Path
    }
    $store = Get-ChildItem "$env:ProgramFiles\WindowsApps\Microsoft.MicrosoftPowerBIDesktop*\bin\PBIDesktop.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    @(
        $store,
        "${env:ProgramFiles}\Microsoft Power BI Desktop\bin\PBIDesktop.exe",
        "${env:ProgramFiles}\Microsoft Power BI Desktop\PBIDesktop.exe",
        "${env:LOCALAPPDATA}\Microsoft\WindowsApps\PBIDesktopStore.exe"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Initialize-Win32Foreground {
    if (Get-Variable -Name PbiWin32Ready -Scope Script -ErrorAction SilentlyContinue) {
        return
    }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PbiWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
"@
    $script:PbiWin32Ready = $true
}

function Show-PbiWindow {
    param($Process)
    if (-not $Process) { return $false }
    try { $Process.Refresh() } catch { }
    if ($Process.MainWindowHandle -eq [IntPtr]::Zero) { return $false }
    Initialize-Win32Foreground
    if ([PbiWin32]::IsIconic($Process.MainWindowHandle)) {
        [void][PbiWin32]::ShowWindow($Process.MainWindowHandle, 9)
    }
    # Windows often denies SetForegroundWindow; UIA/SendKeys can still work.
    [void][PbiWin32]::SetForegroundWindow($Process.MainWindowHandle)
    return $true
}

function Wait-PbiReportWindow {
    param(
        [string]$ReportName,
        [int]$TimeoutS = 180,
        $ProcessHint = $null
    )
    $deadline = (Get-Date).AddSeconds($TimeoutS)
    do {
        $proc = Get-PbiProcessForReport -ReportName $ReportName
        if (-not $proc -and $ProcessHint) {
            try { $ProcessHint.Refresh() } catch { }
            if ($ProcessHint.MainWindowTitle) { $proc = $ProcessHint }
        }
        if ($proc -and $proc.MainWindowHandle -ne [IntPtr]::Zero) {
            $title = [string]$proc.MainWindowTitle
            if ($title -match 'Power BI Desktop' -and $title -notmatch '(?i)opening|loading') {
                return $proc
            }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Get-UiaRoot {
    Initialize-Uia
    return [System.Windows.Automation.AutomationElement]::RootElement
}

function Get-UiaWindowByPid {
    param([int]$ProcessId)
    Initialize-Uia
    $root = Get-UiaRoot
    $cond = New-Object System.Windows.Automation.AndCondition (
        (New-Object System.Windows.Automation.PropertyCondition (
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $ProcessId)),
        (New-Object System.Windows.Automation.PropertyCondition (
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Window))
    )
    return $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
}

function Find-UiaByName {
    param(
        $Root,
        [string]$NamePattern,
        [string[]]$ControlTypes = @("Button", "Hyperlink", "MenuItem", "ListItem", "TabItem")
    )
    if (-not $Root) { return $null }
    Initialize-Uia
    $nameRegex = New-Object System.Text.RegularExpressions.Regex($NamePattern, "IgnoreCase")
    foreach ($typeName in $ControlTypes) {
        $type = [System.Windows.Automation.ControlType]::$typeName
        $typeCond = New-Object System.Windows.Automation.PropertyCondition (
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $type)
        $found = $Root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $typeCond)
        foreach ($el in $found) {
            $name = [string]$el.Current.Name
            if ($name -and $nameRegex.IsMatch($name) -and $el.Current.IsEnabled) {
                return $el
            }
        }
    }
    return $null
}

function Invoke-UiaElement {
    param($Element)
    if (-not $Element) { return $false }
    try {
        $invoke = $Element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
        return $true
    } catch { }
    try {
        $select = $Element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
        $select.Select()
        return $true
    } catch { }
    try {
        $legacy = $Element.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        $legacy.DoDefaultAction()
        return $true
    } catch { }
    return $false
}

function Invoke-UiaNamed {
    param(
        $Root,
        [string]$NamePattern,
        [string[]]$ControlTypes = @("Button", "Hyperlink", "MenuItem")
    )
    $el = Find-UiaByName -Root $Root -NamePattern $NamePattern -ControlTypes $ControlTypes
    if (-not $el) { return $false }
    return Invoke-UiaElement -Element $el
}

function Get-PbiLocalPort {
    param([int]$PbiProcessId)
    $children = Get-CimInstance Win32_Process -Filter "Name='msmdsrv.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $PbiProcessId }
    foreach ($child in $children) {
        $cmd = [string]$child.CommandLine
        $workspace = $null
        if ($cmd -match '-s\s+"([^"]+)"') { $workspace = $Matches[1] }
        elseif ($cmd -match '-s\s+(\S+)') { $workspace = $Matches[1] }
        if ($workspace) {
            $portFile = Join-Path $workspace "msmdsrv.port.txt"
            if (Test-Path -LiteralPath $portFile) {
                $text = (Get-Content -LiteralPath $portFile -Raw).Trim()
                $port = 0
                if ([int]::TryParse($text, [ref]$port) -and $port -gt 0) { return $port }
            }
        }
        try {
            $listen = Get-NetTCPConnection -OwningProcess $child.ProcessId -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($listen) { return [int]$listen.LocalPort }
        } catch { }
    }
    $dirs = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\Power BI Desktop\AnalysisServicesWorkspaces"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\Power BI Desktop Store App\AnalysisServicesWorkspaces")
    )
    $newest = Get-ChildItem -Path $dirs -Filter "msmdsrv.port.txt" -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($newest) {
        $port = 0
        if ([int]::TryParse((Get-Content -LiteralPath $newest.FullName -Raw).Trim(), [ref]$port)) {
            return $port
        }
    }
    return $null
}

function Get-PbiBinDirectory {
    param([int]$PbiProcessId = 0)
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($PbiProcessId -gt 0) {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$PbiProcessId" -ErrorAction SilentlyContinue
        if ($cim -and $cim.ExecutablePath) {
            $candidates.Add((Split-Path -Parent $cim.ExecutablePath))
        }
        $proc = Get-Process -Id $PbiProcessId -ErrorAction SilentlyContinue
        if ($proc) {
            try {
                if ($proc.Path) { $candidates.Add((Split-Path -Parent $proc.Path)) }
            } catch { }
        }
    }
    $exe = Get-PbiExePath
    if ($exe) {
        $dir = Split-Path -Parent $exe
        $candidates.Add($dir)
        $candidates.Add((Join-Path $dir "bin"))
    }
    foreach ($bin in $candidates) {
        if (-not $bin) { continue }
        foreach ($dllName in @(
                "Microsoft.AnalysisServices.AdomdClient.dll",
                "Microsoft.AnalysisServices.Tabular.dll"
            )) {
            $dll = Join-Path $bin $dllName
            try {
                if (Test-Path -LiteralPath $dll) { return $bin }
            } catch { }
        }
        # Microsoft Store build: DLLs sit beside PBIDesktop.exe in WindowsApps.
        if (Test-Path -LiteralPath (Join-Path $bin "PBIDesktop.exe")) {
            return $bin
        }
    }
    return $null
}

function Invoke-PbiModelRefreshTom {
    param(
        [int]$PbiProcessId,
        [int]$TimeoutS = 600
    )
    $port = Get-PbiLocalPort -PbiProcessId $PbiProcessId
    if (-not $port) {
        Write-Host "Local Analysis Services port not found yet."
        return $false
    }
    Write-Host "Refreshing model via local Analysis Services on port $port ..."
    $bin = Get-PbiBinDirectory -PbiProcessId $PbiProcessId
    if (-not $bin) {
        Write-Host "Power BI Analysis Services DLLs not found next to the running Desktop process."
        return $false
    }
    Write-Host "Using Power BI bin $bin"

    $tabular = Join-Path $bin "Microsoft.AnalysisServices.Tabular.dll"
    $core = Join-Path $bin "Microsoft.AnalysisServices.Core.dll"
    $amo = Join-Path $bin "Microsoft.AnalysisServices.dll"
    try {
        Add-Type -Path $core -ErrorAction Stop
        try { Add-Type -Path $amo -ErrorAction SilentlyContinue } catch { }
        Add-Type -Path $tabular -ErrorAction Stop
        $server = New-Object Microsoft.AnalysisServices.Tabular.Server
        $server.Connect("Data Source=localhost:$port")
        try {
            $deadline = (Get-Date).AddSeconds(60)
            while ($server.Databases.Count -lt 1 -and (Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 2
                $server.Disconnect()
                $server.Connect("Data Source=localhost:$port")
            }
            if ($server.Databases.Count -lt 1) {
                Write-Host "TOM connected on port $port but no database was loaded yet."
                return $false
            }
            $db = $server.Databases[0]
            Write-Host "TOM refresh: $($db.Name)"
            $db.Model.RequestRefresh([Microsoft.AnalysisServices.Tabular.RefreshType]::Full)
            [void]$db.Model.SaveChanges()
            return $true
        } finally {
            $server.Disconnect()
        }
    } catch {
        Write-Host "TOM refresh failed: $($_.Exception.Message)"
    }

    $adomd = Join-Path $bin "Microsoft.AnalysisServices.AdomdClient.dll"
    try {
        Add-Type -Path $adomd -ErrorAction Stop
        $conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection("Data Source=localhost:$port")
        $conn.Open()
        try {
            $cmd = $conn.CreateCommand()
            $cmd.CommandTimeout = $TimeoutS
            $cmd.CommandText = "SELECT [CATALOG_NAME] FROM `$SYSTEM.DBSCHEMA_CATALOGS"
            $reader = $cmd.ExecuteReader()
            $catalog = $null
            if ($reader.Read()) { $catalog = [string]$reader[0] }
            $reader.Close()
            if (-not $catalog) { return $false }
            Write-Host "TMSL refresh: $catalog"
            $tmsl = @"
{"refresh":{"type":"full","objects":[{"database":"$catalog"}]}}
"@
            $cmd.CommandText = $tmsl
            $xml = $cmd.ExecuteXmlReader()
            while ($xml.Read()) { }
            $xml.Close()
            return $true
        } finally {
            $conn.Close()
        }
    } catch {
        Write-Host "Adomd refresh failed: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-PbiRibbonRefresh {
    param($Process, [int]$TimeoutS = 600)
    [void](Show-PbiWindow -Process $Process)
    Start-Sleep -Milliseconds 400
    $windows = Get-UiaWindowByPid -ProcessId $Process.Id
    $clicked = $false
    foreach ($win in $windows) {
        if (Invoke-UiaNamed -Root $win -NamePattern '^Refresh$' -ControlTypes @("Button", "MenuItem", "SplitButton")) {
            $clicked = $true
            break
        }
    }
    if (-not $clicked) {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.AppActivate($Process.Id)
        Start-Sleep -Milliseconds 300
        # Home ribbon keytips: Alt, H, R (Refresh) on English Desktop.
        [System.Windows.Forms.SendKeys]::SendWait("%")
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("h")
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("r")
        $clicked = $true
    }
    if (-not $clicked) { return $false }
    return Wait-PbiRefreshFinished -Process $Process -TimeoutS $TimeoutS
}

function Wait-PbiRefreshFinished {
    param($Process, [int]$TimeoutS = 600)
    $startedAt = Get-Date
    $deadline = $startedAt.AddSeconds($TimeoutS)
    $sawRefreshUi = $false
    Start-Sleep -Seconds 2
    do {
        $busy = $false
        foreach ($win in Get-UiaWindowByPid -ProcessId $Process.Id) {
            $title = [string]$win.Current.Name
            if ($title -match '(?i)^refresh$|refreshing|applying query') {
                $busy = $true
                $sawRefreshUi = $true
            }
            $status = Find-UiaByName -Root $win -NamePattern 'refreshing|applying query|loading data' -ControlTypes @("Text", "StatusBar", "Button")
            if ($status) {
                $busy = $true
                $sawRefreshUi = $true
            }
        }
        $elapsed = ((Get-Date) - $startedAt).TotalSeconds
        if ($sawRefreshUi -and -not $busy -and $elapsed -ge 4) { return $true }
        # Fast CSV refresh often has no modal; give the engine a few seconds.
        if (-not $sawRefreshUi -and $elapsed -ge 12) { return $true }
        Start-Sleep -Milliseconds 750
    } while ((Get-Date) -lt $deadline)
    return -not $sawRefreshUi
}

function Invoke-PbiSave {
    param($Process)
    if (-not (Show-PbiWindow -Process $Process)) { return $false }
    Start-Sleep -Milliseconds 200
    [System.Windows.Forms.SendKeys]::SendWait("^s")
    Start-Sleep -Seconds 2
    return $true
}

function Get-PbiAccessToken {
    if ($env:PBI_ACCESS_TOKEN) { return $env:PBI_ACCESS_TOKEN }
    foreach ($moduleName in @("MicrosoftPowerBIMgmt.Profile", "MicrosoftPowerBIMgmt")) {
        if (-not (Get-Module -ListAvailable -Name $moduleName)) { continue }
        Import-Module $moduleName -ErrorAction SilentlyContinue
        try {
            $token = Get-PowerBIAccessToken -AsString -ErrorAction Stop
            if ($token) { return ($token -replace '^Bearer\s+', '') }
        } catch { }
        try {
            Connect-PowerBIServiceAccount -ErrorAction Stop | Out-Null
            $token = Get-PowerBIAccessToken -AsString -ErrorAction Stop
            if ($token) { return ($token -replace '^Bearer\s+', '') }
        } catch { }
    }
    if (Get-Command az -ErrorAction SilentlyContinue) {
        try {
            $json = az account get-access-token --resource https://analysis.windows.net/powerbi/api -o json 2>$null
            if ($json) {
                $parsed = $json | ConvertFrom-Json
                if ($parsed.accessToken) { return $parsed.accessToken }
            }
        } catch { }
    }
    return $null
}

function Publish-PbiViaRest {
    param(
        [string]$PbixPath,
        [string]$WorkspaceName,
        [string]$ReportName,
        [int]$TimeoutS = 180
    )
    if (-not $WorkspaceName) { return $false }
    $token = Get-PbiAccessToken
    if (-not $token) {
        Write-Host "No Power BI REST token (install MicrosoftPowerBIMgmt or az login). Falling back to Desktop Publish."
        return $false
    }
    $headers = @{ Authorization = "Bearer $token" }
    try {
        $groups = Invoke-RestMethod -Uri "https://api.powerbi.com/v1.0/myorg/groups" -Headers $headers
    } catch {
        Write-Host "Power BI workspace list failed: $($_.Exception.Message)"
        return $false
    }
    $isMine = $WorkspaceName -match '(?i)^my(\s+)?workspace$'
    $groupId = $null
    if (-not $isMine) {
        $group = @($groups.value) | Where-Object { $_.name -eq $WorkspaceName } | Select-Object -First 1
        if (-not $group) {
            Write-Host "Workspace '$WorkspaceName' not found for this account."
            return $false
        }
        $groupId = $group.id
    }
    $encoded = [uri]::EscapeDataString($ReportName)
    if ($groupId) {
        $uri = "https://api.powerbi.com/v1.0/myorg/groups/$groupId/imports?datasetDisplayName=$encoded&nameConflict=CreateOrOverwrite"
        $statusUriBase = "https://api.powerbi.com/v1.0/myorg/groups/$groupId/imports/"
    } else {
        $uri = "https://api.powerbi.com/v1.0/myorg/imports?datasetDisplayName=$encoded&nameConflict=CreateOrOverwrite"
        $statusUriBase = "https://api.powerbi.com/v1.0/myorg/imports/"
    }
    Write-Host "Uploading $ReportName to workspace '$WorkspaceName' (replace if it exists)..."
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds([Math]::Max($TimeoutS, 60))
    $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $token)
    $stream = [IO.File]::OpenRead($PbixPath)
    try {
        $content = New-Object System.Net.Http.MultipartFormDataContent
        $fileContent = New-Object System.Net.Http.StreamContent($stream)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse("application/octet-stream")
        $content.Add($fileContent, "file", [IO.Path]::GetFileName($PbixPath))
        $response = $client.PostAsync($uri, $content).Result
        $body = $response.Content.ReadAsStringAsync().Result
        if (-not $response.IsSuccessStatusCode) {
            Write-Host "REST import failed ($($response.StatusCode)): $body"
            return $false
        }
        $parsed = $body | ConvertFrom-Json
        $importId = $parsed.id
        if (-not $importId) { return $true }
        $deadline = (Get-Date).AddSeconds($TimeoutS)
        do {
            Start-Sleep -Seconds 3
            $status = Invoke-RestMethod -Uri ($statusUriBase + $importId) -Headers $headers
            $state = [string]$status.importState
            if ($state -eq "Succeeded") {
                Write-Host "Workspace report replaced via REST import."
                return $true
            }
            if ($state -eq "Failed") {
                Write-Host "REST import reported Failed."
                return $false
            }
        } while ((Get-Date) -lt $deadline)
        Write-Host "REST import still running after timeout; check the workspace."
        return $true
    } finally {
        $stream.Dispose()
        $client.Dispose()
    }
}

function Invoke-PbiPublishUi {
    param(
        $Process,
        [string]$WorkspaceName,
        [int]$TimeoutS = 180
    )
    Initialize-Uia
    if (-not (Show-PbiWindow -Process $Process)) { return $false }
    Start-Sleep -Milliseconds 400
    $windows = Get-UiaWindowByPid -ProcessId $Process.Id
    $started = $false
    foreach ($win in $windows) {
        if (Invoke-UiaNamed -Root $win -NamePattern '^Publish$' -ControlTypes @("Button", "MenuItem", "SplitButton", "Hyperlink")) {
            $started = $true
            break
        }
    }
    if (-not $started) {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.AppActivate($Process.Id)
        Start-Sleep -Milliseconds 300
        [System.Windows.Forms.SendKeys]::SendWait("%")
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("h")
        Start-Sleep -Milliseconds 250
        [System.Windows.Forms.SendKeys]::SendWait("u")
        $started = $true
    }
    if (-not $started) { return $false }

    $deadline = (Get-Date).AddSeconds($TimeoutS)
    $clickedSelect = $false
    $clickedReplace = $false
    $clickedGotIt = $false
    $sawSuccess = $false
    while ((Get-Date) -lt $deadline) {
        foreach ($win in Get-UiaWindowByPid -ProcessId $Process.Id) {
            $title = [string]$win.Current.Name
            if ($title -match 'Power BI Desktop$' -and $title -notmatch '(?i)publish') {
                continue
            }
            if ($title -match '(?i)sign in') {
                throw "Power BI Desktop is not signed in. Sign in once, then re-run."
            }
            if ($WorkspaceName -and $title -match '(?i)publish') {
                $item = Find-UiaByName -Root $win -NamePattern ("^" + [regex]::Escape($WorkspaceName) + "$") -ControlTypes @("ListItem", "TreeItem", "DataItem", "Text")
                if ($item) { [void](Invoke-UiaElement -Element $item) }
            }
            if (-not $clickedSelect -and $title -match '(?i)publish') {
                if (Invoke-UiaNamed -Root $win -NamePattern '^Select$') {
                    $clickedSelect = $true
                    Start-Sleep -Milliseconds 800
                    continue
                }
            }
            if (-not $clickedReplace -and (Invoke-UiaNamed -Root $win -NamePattern '^(Replace|Overwrite)$')) {
                $clickedReplace = $true
                Write-Host "Accepted Replace/Overwrite."
                Start-Sleep -Seconds 2
                continue
            }
            if ($title -match '(?i)success|published') { $sawSuccess = $true }
            if ($title -match '(?i)publish|success|published' -and (Invoke-UiaNamed -Root $win -NamePattern '^Got it$')) {
                $clickedGotIt = $true
                $sawSuccess = $true
                Write-Host "Dismissed publish confirmation."
                return $true
            }
        }
        if ($clickedSelect -and $clickedReplace -and -not $clickedGotIt) {
            # Success toast can vanish on its own.
            Start-Sleep -Seconds 2
            $stillPrompt = $false
            foreach ($win in Get-UiaWindowByPid -ProcessId $Process.Id) {
                if ([string]$win.Current.Name -match '(?i)publish|replace') { $stillPrompt = $true }
            }
            if (-not $stillPrompt) { return $true }
        }
        Start-Sleep -Milliseconds 500
    }
    return ($sawSuccess -or $clickedReplace -or $clickedGotIt)
}

function Close-PbiReport {
    param(
        [string]$ReportName,
        [switch]$SaveFirst
    )
    $proc = Get-PbiProcessForReport -ReportName $ReportName
    if (-not $proc) {
        Write-Host "Power BI Desktop is not open for $ReportName."
        return $true
    }
    Write-Host "Closing Power BI Desktop ($($proc.MainWindowTitle))..."
    if ($SaveFirst) {
        Invoke-PbiSave -Process $proc
    }
    [void]$proc.CloseMainWindow()
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        try { $proc.Refresh() } catch { return $true }
        if ($proc.HasExited) { return $true }
        foreach ($win in Get-UiaWindowByPid -ProcessId $proc.Id) {
            if ($SaveFirst) {
                if (Invoke-UiaNamed -Root $win -NamePattern '^Save$') { Start-Sleep -Milliseconds 400; continue }
            } else {
                if (Invoke-UiaNamed -Root $win -NamePattern "^Don't Save$|^Do not save$|^No$") {
                    Start-Sleep -Milliseconds 400
                    continue
                }
            }
        }
        Start-Sleep -Milliseconds 400
    }
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -Filter "Name='msmdsrv.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ParentProcessId -eq $proc.Id } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 2
    return $true
}

function Start-PbiReport {
    param(
        [string]$PbixPath,
        [string]$ReportName,
        [int]$TimeoutS = 180
    )
    $existing = Get-PbiProcessForReport -ReportName $ReportName
    if ($existing) {
        Write-Host "Reusing open Desktop window: $($existing.MainWindowTitle)"
        return $existing
    }
    $exe = Get-PbiExePath
    if (-not $exe) { throw "Power BI Desktop executable not found." }
    Write-Host "Opening Power BI Desktop: $PbixPath"
    $started = Start-Process -FilePath $exe -ArgumentList "`"$PbixPath`"" -PassThru
    $proc = Wait-PbiReportWindow -ReportName $ReportName -TimeoutS $TimeoutS -ProcessHint $started
    if (-not $proc) {
        throw "Timed out waiting for Power BI Desktop to open $ReportName."
    }
    # Local AS workspace appears after the file is fully loaded.
    $asDeadline = (Get-Date).AddSeconds([Math]::Min($TimeoutS, 90))
    while ((Get-Date) -lt $asDeadline) {
        if (Get-PbiLocalPort -PbiProcessId $proc.Id) { break }
        Start-Sleep -Seconds 1
    }
    return $proc
}

function Invoke-PbiRefreshAndPublish {
    param(
        [Parameter(Mandatory = $true)][string]$PbixPath,
        [Parameter(Mandatory = $true)][string]$ReportName,
        [string]$WorkspaceName = "",
        [switch]$SkipRefresh,
        [switch]$SkipPublish,
        [switch]$LeaveOpen,
        [int]$OpenTimeoutS = 180,
        [int]$RefreshTimeoutS = 600,
        [int]$PublishTimeoutS = 180
    )
    Initialize-Uia
    $proc = Start-PbiReport -PbixPath $PbixPath -ReportName $ReportName -TimeoutS $OpenTimeoutS
    $refreshed = $false
    if (-not $SkipRefresh) {
        Write-Host "Refreshing $ReportName from CSV sources..."
        $refreshed = Invoke-PbiModelRefreshTom -PbiProcessId $proc.Id -TimeoutS $RefreshTimeoutS
        if (-not $refreshed) {
            Write-Host "Engine refresh unavailable; using Desktop Refresh button."
            $refreshed = Invoke-PbiRibbonRefresh -Process $proc -TimeoutS $RefreshTimeoutS
        }
        if (-not $refreshed) {
            throw "Power BI refresh did not complete for $ReportName."
        }
        Write-Host "Refresh completed."
        Invoke-PbiSave -Process $proc
        Start-Sleep -Seconds 2
    }
    $published = $false
    if (-not $SkipPublish) {
        $published = Publish-PbiViaRest -PbixPath $PbixPath -WorkspaceName $WorkspaceName -ReportName $ReportName -TimeoutS $PublishTimeoutS
        if (-not $published) {
            Write-Host "Publishing from Desktop (auto-confirm Replace)..."
            $published = Invoke-PbiPublishUi -Process $proc -WorkspaceName $WorkspaceName -TimeoutS $PublishTimeoutS
        }
        if (-not $published) {
            throw "Power BI publish did not complete for $ReportName."
        }
        Write-Host "Published $ReportName to the workspace."
    }
    if (-not $LeaveOpen) {
        $null = Close-PbiReport -ReportName $ReportName -SaveFirst
    }
    return [pscustomobject]@{
        Refreshed = [bool]$refreshed
        Published = [bool]$published
        PbixPath  = $PbixPath
    }
}
