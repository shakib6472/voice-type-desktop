<#
  Creates Voice Bridge shortcuts in the Start Menu and on the Desktop.

  The shortcuts launch pythonw.exe, which is Python without a console, so no
  black terminal window ever appears. Once the Start Menu entry exists you can
  right click it and choose "Pin to taskbar".

  Run with -Remove to delete the shortcuts again.
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'

$here     = $PSScriptRoot
$name     = 'Voice Bridge.lnk'
$programs = [Environment]::GetFolderPath('Programs')
$desktop  = [Environment]::GetFolderPath('Desktop')
$startup  = [Environment]::GetFolderPath('Startup')

$startMenuLink = Join-Path $programs $name
$desktopLink   = Join-Path $desktop  $name
$startupLink   = Join-Path $startup  $name

if ($Remove) {
    foreach ($p in @($startMenuLink, $desktopLink, $startupLink)) {
        if (Test-Path $p) {
            Remove-Item $p -Force
            Write-Host "removed  $p"
        }
    }
    Write-Host ''
    Write-Host 'Voice Bridge shortcuts removed.'
    return
}

$script = Join-Path $here 'voicebridge.py'
$icon   = Join-Path $here 'favicon.ico'

if (-not (Test-Path $script)) {
    Write-Host 'voicebridge.py is not in this folder. Keep install.ps1 next to it.'
    exit 1
}

# pythonw.exe is Python with no console attached. pyw.exe is the launcher
# equivalent and works just as well.
$pythonw = $null
foreach ($candidate in @('pythonw.exe', 'pyw.exe')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source; break }
}
if (-not $pythonw) {
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $guess = Join-Path (Split-Path -Parent $python.Source) 'pythonw.exe'
        if (Test-Path $guess) { $pythonw = $guess }
    }
}
if (-not $pythonw) {
    Write-Host 'Could not find pythonw.exe on this machine.'
    Write-Host 'Install Python 3 from python.org and tick "Add python.exe to PATH".'
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

function New-VoiceBridgeShortcut([string]$Path) {
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = '"' + $script + '"'
    $lnk.WorkingDirectory = $here
    $lnk.Description      = 'Bangla and English voice typing for any Windows app'
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
    Write-Host "created  $Path"
}

Write-Host ''
Write-Host "using  $pythonw"
Write-Host ''
New-VoiceBridgeShortcut $startMenuLink
New-VoiceBridgeShortcut $desktopLink

Write-Host ''
Write-Host 'To pin it to the taskbar: press Start, type "Voice Bridge",'
Write-Host 'right click the result, then choose "Pin to taskbar".'
Write-Host ''

$answer = Read-Host 'Start Voice Bridge automatically when Windows starts? (y/N)'
if ($answer -match '^[Yy]') {
    New-VoiceBridgeShortcut $startupLink
} elseif (Test-Path $startupLink) {
    Remove-Item $startupLink -Force
    Write-Host "removed  $startupLink"
} else {
    Write-Host 'Skipped. Run this installer again if you change your mind.'
}

Write-Host ''
Write-Host 'Done.'
