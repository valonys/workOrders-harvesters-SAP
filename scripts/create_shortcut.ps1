<#
.SYNOPSIS
    Puts a "SAP IW29 export" shortcut on the Desktop.

.DESCRIPTION
    The shortcut runs the app through pythonw.exe so no console window appears.

.EXAMPLE
    .\create_shortcut.ps1
    .\create_shortcut.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Name = "SAP IW29 export",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path ([Environment]::GetFolderPath("Desktop")) "$Name.lnk"

if ($Remove) {
    if (Test-Path $target) { Remove-Item $target -Force }
    Write-Host "Removed $target"
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$entry = Join-Path $root "run_gui.pyw"
if (-not (Test-Path $entry)) { throw "Cannot find $entry" }

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($target)
$link.TargetPath = $pythonw
$link.Arguments = """$entry"""
$link.WorkingDirectory = $root
$link.IconLocation = "$python,0"
$link.Description = "Export SAP IW29 notifications to the synced SharePoint folder"
$link.Save()

Write-Host "Created $target"
Write-Host "  runs: $pythonw ""$entry"""
