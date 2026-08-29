# Pull live bot logs from the GCP VM into .\logs for local inspection.
#
# Setup (once):
#   Copy-Item scripts\deploy.local.example scripts\.deploy.local
#   # edit scripts\.deploy.local with your VM IP and username
#
# Usage:
#   .\scripts\sync-logs.ps1
#   .\scripts\sync-logs.ps1 -Tail
param(
    [switch]$Tail
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Config = Join-Path $Root "scripts\.deploy.local"

if (Test-Path $Config) {
    Get-Content $Config | ForEach-Object {
        if ($_ -match '^\s*([^#=]+?)\s*=\s*(.+?)\s*$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

if (-not $env:GCP_SSH_HOST) { throw "Set GCP_SSH_HOST in scripts\.deploy.local or the environment" }
if (-not $env:GCP_SSH_USER) { throw "Set GCP_SSH_USER in scripts\.deploy.local or the environment" }

$DeployPath = if ($env:GCP_DEPLOY_PATH) { $env:GCP_DEPLOY_PATH } else { "~/crypto-trade-bot" }
$RemoteLogs = if ($env:GCP_REMOTE_LOGS) { $env:GCP_REMOTE_LOGS } else { "logs" }
$LocalLogs = if ($env:GCP_LOCAL_LOGS) { $env:GCP_LOCAL_LOGS } else { Join-Path $Root "logs" }

New-Item -ItemType Directory -Force -Path $LocalLogs | Out-Null
$Remote = "${env:GCP_SSH_USER}@${env:GCP_SSH_HOST}:${DeployPath}/${RemoteLogs}/"

Write-Host "Syncing $Remote -> $LocalLogs\"
$patterns = @("events.jsonl", "trades.jsonl", "briefings.jsonl", "control.json", "briefing_state.json")
foreach ($name in $patterns) {
    $src = "${env:GCP_SSH_USER}@${env:GCP_SSH_HOST}:${DeployPath}/${RemoteLogs}/${name}"
    scp $src $LocalLogs 2>$null
}

Write-Host "Done. Local logs:"
Get-ChildItem $LocalLogs

if ($Tail) {
    foreach ($name in @("events.jsonl", "trades.jsonl", "briefings.jsonl")) {
        $path = Join-Path $LocalLogs $name
        if (Test-Path $path) {
            Write-Host ""
            Write-Host "--- tail $name ---"
            Get-Content $path -Tail 5
        }
    }
}
