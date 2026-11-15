# Creates Imla shortcuts (Desktop, Start Menu, optional Startup) on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1 [-StartOnLogin]
param(
    [switch]$StartOnLogin
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $PythonW)) {
    Write-Host "Could not find $PythonW"
    Write-Host "Create the environment first, from the project folder:"
    Write-Host "    uv sync --python 3.12"
    exit 1
}

$IconFile = Join-Path $Root 'assets\imla.ico'
if (Test-Path -LiteralPath $IconFile) {
    $Icon = $IconFile
} else {
    $Icon = $PythonW
}

$Shell = New-Object -ComObject WScript.Shell

function New-ImlaShortcut {
    param(
        [string]$Path,
        [string]$Arguments
    )
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $lnk = $Shell.CreateShortcut($Path)
    $lnk.TargetPath = $PythonW
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'Imla voice dictation'
    $lnk.IconLocation = $Icon
    $lnk.Save()
    Write-Host "Created: $Path"
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$Programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$Startup = [Environment]::GetFolderPath('Startup')

New-ImlaShortcut -Path (Join-Path $Desktop 'Imla.lnk') -Arguments '-m imla.main'
New-ImlaShortcut -Path (Join-Path $Programs 'Imla.lnk') -Arguments '-m imla.main'
New-ImlaShortcut -Path (Join-Path $Programs 'Imla Settings.lnk') -Arguments '-m imla.main --settings'

if ($StartOnLogin) {
    New-ImlaShortcut -Path (Join-Path $Startup 'Imla.lnk') -Arguments '-m imla.main'
}

Write-Host 'Done.'
