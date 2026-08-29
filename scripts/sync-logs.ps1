# Pull live bot logs from the GCP VM into .\logs for local inspection.
#
# Setup (once):
#   Copy-Item scripts\deploy.local.example scripts\.deploy.local
#   # edit scripts\.deploy.local with your VM IP and username
#
# Usage:
#   .\scripts\sync-logs.ps1
#   .\scripts\sync-logs.ps1 -Tail
#   .\scripts\sync-logs.ps1 -Force   # overwrite local even if scp skips unchanged files
param(
    [switch]$Tail,
    [switch]$Force
)

$LogFiles = @("events.jsonl", "trades.jsonl", "briefings.jsonl", "control.json", "briefing_state.json")

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

$ScpArgs = @()
if ($env:GCP_SSH_IDENTITY_FILE) {
    $identity = $env:GCP_SSH_IDENTITY_FILE
    if (-not [System.IO.Path]::IsPathRooted($identity)) {
        $identity = Join-Path $Root $identity
    }
    if (-not (Test-Path $identity)) {
        throw "GCP_SSH_IDENTITY_FILE not found: $identity"
    }
    $ScpArgs += "-i", $identity
}

New-Item -ItemType Directory -Force -Path $LocalLogs | Out-Null
$Remote = "${env:GCP_SSH_USER}@${env:GCP_SSH_HOST}:${DeployPath}/${RemoteLogs}/"

Write-Host "Syncing $Remote -> $LocalLogs\"
$copied = 0
foreach ($name in $LogFiles) {
    $src = "${env:GCP_SSH_USER}@${env:GCP_SSH_HOST}:${DeployPath}/${RemoteLogs}/${name}"
    $dest = Join-Path $LocalLogs $name
    if ($Force -and (Test-Path $dest)) {
        Remove-Item -Force $dest
    }
    scp @ScpArgs $src $LocalLogs 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $copied++
    } else {
        Write-Warning "Skipped ${name} (missing on VM or scp failed)"
    }
}
if ($copied -eq 0) {
    throw "No log files copied — check SSH access and GCP_SSH_IDENTITY_FILE in scripts\.deploy.local"
}
Write-Host "Copied $copied file(s)."

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
