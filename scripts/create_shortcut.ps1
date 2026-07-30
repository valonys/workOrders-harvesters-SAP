<#
.SYNOPSIS
    Puts two shortcuts on the Desktop: a one-click export and the app window.

.DESCRIPTION
    "Run IW29 export now"  - does the whole job on a double-click, shows progress
                             in a console and leaves the result on screen.
    "SAP IW29 export"      - the app window, for changing criteria before running.

.EXAMPLE
    .\create_shortcut.ps1
    .\create_shortcut.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$shortcuts = @(
    @{
        Name        = "Run IW29 export now"
        Target      = Join-Path $root "run_now.cmd"
        Arguments   = ""
        Description = "Export SAP IW29 to the synced SharePoint folder"
    },
    @{
        Name        = "SAP IW29 export"
        Target      = $pythonw
        Arguments   = """$(Join-Path $root 'run_gui.pyw')"""
        Description = "Open the IW29 export app to change criteria before running"
    }
)

if ($Remove) {
    foreach ($s in $shortcuts) {
        $link = Join-Path $desktop "$($s.Name).lnk"
        if (Test-Path $link) { Remove-Item $link -Force; Write-Host "Removed $link" }
    }
    return
}

$shell = New-Object -ComObject WScript.Shell
foreach ($s in $shortcuts) {
    if (-not (Test-Path $s.Target)) { throw "Cannot find $($s.Target)" }
    $link = $shell.CreateShortcut((Join-Path $desktop "$($s.Name).lnk"))
    $link.TargetPath = $s.Target
    $link.Arguments = $s.Arguments
    $link.WorkingDirectory = $root
    $link.IconLocation = "$python,0"
    $link.Description = $s.Description
    $link.Save()
    Write-Host "Created '$($s.Name)' -> $($s.Target)"
}
