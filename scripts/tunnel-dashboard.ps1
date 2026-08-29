# SSH tunnel: open the VM dashboard at http://127.0.0.1:8000 (reads live logs on the server).
#
# Setup: same scripts\.deploy.local as sync-logs.ps1
# Usage: .\scripts\tunnel-dashboard.ps1
param(
    [int]$LocalPort = 8000,
    [int]$RemotePort = 8000
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

if (-not $env:GCP_SSH_HOST) { throw "Set GCP_SSH_HOST in scripts\.deploy.local" }
if (-not $env:GCP_SSH_USER) { throw "Set GCP_SSH_USER in scripts\.deploy.local" }

if ($env:GCP_LOCAL_DASHBOARD_PORT) { $LocalPort = [int]$env:GCP_LOCAL_DASHBOARD_PORT }
if ($env:GCP_DASHBOARD_PORT) { $RemotePort = [int]$env:GCP_DASHBOARD_PORT }

$SshArgs = @()
if ($env:GCP_SSH_IDENTITY_FILE) {
    $identity = $env:GCP_SSH_IDENTITY_FILE
    if (-not [System.IO.Path]::IsPathRooted($identity)) {
        $identity = Join-Path $Root $identity
    }
    if (-not (Test-Path $identity)) {
        throw "GCP_SSH_IDENTITY_FILE not found: $identity"
    }
    $SshArgs += "-i", $identity
}

Write-Host "Tunneling localhost:${LocalPort} -> ${env:GCP_SSH_HOST}:${RemotePort}"
Write-Host "Open http://127.0.0.1:${LocalPort} (Ctrl+C to close)"
ssh @SshArgs -N -L "${LocalPort}:127.0.0.1:${RemotePort}" "${env:GCP_SSH_USER}@${env:GCP_SSH_HOST}"
