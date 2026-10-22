# Creates Desktop shortcuts for the local config UI (start + stop).
# Re-run it after moving this folder. ASCII only: PowerShell 5.1 reads
# BOM-less UTF-8 as ANSI.
$ErrorActionPreference = 'Stop'

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launch = Join-Path $dir 'launch.ps1'
$stop = Join-Path $dir 'stop.ps1'
if (-not (Test-Path -LiteralPath $launch)) { throw "launch.ps1 not found next to this script" }

$node = 'D:\Program Files\nodejs\node.exe'
if (-not (Test-Path -LiteralPath $node)) { $node = $null }
$localIcon = Join-Path $dir 'icon.ico'
$icon = if (Test-Path -LiteralPath $localIcon) {
    "$localIcon,0"
} elseif ($node) {
    "$node,0"
} else {
    "$env:SystemRoot\System32\imageres.dll,68"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell

function New-UiShortcut([string] $path, [string] $script, [string] $description) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"'
    $shortcut.WorkingDirectory = $dir
    $shortcut.Description = $description
    $shortcut.IconLocation = $icon
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Output "created: $path"
}

New-UiShortcut (Join-Path $desktop ([char]0x671F + [char]0x520A + [char]0x76D1 + [char]0x63A7 + [char]0x914D + [char]0x7F6E + '.lnk')) $launch 'Journal status monitor - local configuration UI'
New-UiShortcut (Join-Path $desktop ([char]0x505C + [char]0x6B62 + [char]0x76D1 + [char]0x63A7 + [char]0x540E + [char]0x53F0 + '.lnk')) $stop 'Stop the local configuration UI'