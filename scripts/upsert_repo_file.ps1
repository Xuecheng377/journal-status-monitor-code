param(
    [Parameter(Mandatory=$true)]
    [string]$Token,
    [Parameter(Mandatory=$true)]
    [string]$LocalFile,
    [Parameter(Mandatory=$true)]
    [string]$RepoPath,
    [Parameter(Mandatory=$true)]
    [string]$Message
)

$ErrorActionPreference = "Stop"
$owner = "Xuecheng377"
$repo = "journal-status-monitor"
$branch = "main"
$apiBase = "https://api.github.com/repos/$owner/$repo"

$headers = @{
    Authorization = "Bearer $Token"
    Accept = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "codex-repo-updater"
}

function Invoke-GitHubJson {
    param([string]$Method, [string]$Uri, [object]$Body = $null)
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers
    }
    $json = $Body | ConvertTo-Json -Depth 20
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers -ContentType "application/json; charset=utf-8" -Body $json
}

$escapedPath = [System.Uri]::EscapeDataString($RepoPath).Replace("%2F", "/")
$sha = $null
try {
    $existing = Invoke-GitHubJson -Method "GET" -Uri ("$apiBase/contents/$escapedPath" + "?ref=$branch")
    $sha = $existing.sha
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) {
        throw
    }
}

$content = Get-Content -Raw -LiteralPath $LocalFile -Encoding UTF8
$encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($content))
$body = @{
    message = $Message
    content = $encoded
    branch = $branch
}
if ($sha) {
    $body.sha = $sha
}

Invoke-GitHubJson -Method "PUT" -Uri "$apiBase/contents/$escapedPath" -Body $body | Out-Null
Write-Host "upserted $RepoPath"
