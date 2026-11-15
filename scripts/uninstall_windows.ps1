# Removes the shortcuts created by install_windows.ps1.
$ErrorActionPreference = 'Stop'

$Desktop = [Environment]::GetFolderPath('Desktop')
$Programs = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
$Startup = [Environment]::GetFolderPath('Startup')

$Targets = @(
    (Join-Path $Desktop 'Imla.lnk'),
    (Join-Path $Programs 'Imla.lnk'),
    (Join-Path $Programs 'Imla Settings.lnk'),
    (Join-Path $Startup 'Imla.lnk')
)

foreach ($t in $Targets) {
    if (Test-Path -LiteralPath $t) {
        Remove-Item -LiteralPath $t -Force
        Write-Host "Removed: $t"
    }
}

Write-Host 'Done.'
