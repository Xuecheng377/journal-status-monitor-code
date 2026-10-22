# Stops the background config UI started by launch.ps1.
# ASCII only: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI.
$ErrorActionPreference = 'SilentlyContinue'

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$stopped = 0

Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" | ForEach-Object {
    $commandLine = [string] $_.CommandLine
    if ($commandLine -like '*server.js*' -and $commandLine -like '*local-config-ui*') {
        Stop-Process -Id $_.ProcessId -Force
        $stopped++
    }
}

$shell = New-Object -ComObject WScript.Shell
if ($stopped -gt 0) {
    $shell.Popup("Stopped $stopped local UI process(es).", 4, 'Journal Status UI', 64) | Out-Null
} else {
    $shell.Popup("The local UI is not running.", 4, 'Journal Status UI', 64) | Out-Null
}