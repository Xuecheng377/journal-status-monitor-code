#Requires -Version 5.0
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI,
# so any non-ASCII text here would arrive as mojibake in dialogs.
$ErrorActionPreference = 'Stop'

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
$url = 'http://127.0.0.1:8976/'
$log = Join-Path $env:TEMP 'journal-status-ui.log'

function Write-LaunchLog([string] $message) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message
    try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 } catch { }
}

function Test-UiUp {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Show-Problem([string] $message) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($message, 0, 'Journal Status UI', 16) | Out-Null
    } catch {
        Write-Output $message
    }
}

function Resolve-Node {
    $candidates = @(
        'D:\Program Files\nodejs\node.exe',
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    $fromPath = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($fromPath) {
        return $fromPath.Source
    }
    return $null
}

function Open-Browser {
    try {
        Start-Process $url
    } catch {
        # Last resort: explorer understands http URLs on every Windows build.
        Start-Process explorer.exe $url
    }
}

Write-LaunchLog 'launcher started'

if (Test-UiUp) {
    Write-LaunchLog 'server already running, opening browser'
    Open-Browser
    exit 0
}

$node = Resolve-Node
if (-not $node) {
    Write-LaunchLog 'node.exe not found'
    Show-Problem "Node.js was not found.`n`nInstall Node.js, then run this shortcut again."
    exit 1
}

$dependency = Join-Path $dir 'node_modules\libsodium-wrappers'
if (-not (Test-Path -LiteralPath $dependency)) {
    $npmCli = Join-Path (Split-Path -Parent $node) 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $npmCli)) {
        Write-LaunchLog "npm-cli.js missing at $npmCli"
        Show-Problem "Dependencies are missing and npm could not be located.`n`nOpen PowerShell in:`n$dir`nand run: npm install"
        exit 1
    }
    Write-LaunchLog 'installing npm dependencies'
    Start-Process -FilePath $node -ArgumentList @($npmCli, 'install', '--no-audit', '--no-fund') `
        -WorkingDirectory $dir -WindowStyle Minimized -Wait
    if (-not (Test-Path -LiteralPath $dependency)) {
        Write-LaunchLog 'npm install did not create node_modules'
        Show-Problem "Dependency install failed.`n`nOpen PowerShell in:`n$dir`nand run: npm install`n`nLog: $log"
        exit 1
    }
}

Write-LaunchLog "starting server with $node"
try {
    Start-Process -FilePath $node -ArgumentList 'server.js' -WorkingDirectory $dir -WindowStyle Hidden | Out-Null
} catch {
    Write-LaunchLog ("start failed: {0}" -f $_.Exception.Message)
    Show-Problem "Could not start the local server.`n`nLog: $log"
    exit 1
}

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    if (Test-UiUp) {
        $ready = $true
        break
    }
}

if ($ready) {
    Write-LaunchLog 'server is up, opening browser'
    Open-Browser
    exit 0
}

Write-LaunchLog 'server did not answer on 8976'
Show-Problem "The local server did not start within 20 seconds.`n`nCheck the log:`n$log"
exit 1