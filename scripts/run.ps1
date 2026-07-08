<#
  Garmin Coach launcher (Windows / PowerShell).
    .\scripts\run.ps1            # real data (needs one-time garmin-mcp-auth)
    .\scripts\run.ps1 -Demo      # synthetic demo data, no Garmin account
    .\scripts\run.ps1 -Auth      # run the one-time Garmin login (email/pw/MFA)
#>
param(
  [switch]$Demo,
  [switch]$Auth,
  [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Auth) {
  Write-Host "Launching one-time Garmin authentication (saves token to ~/.garminconnect)..." -ForegroundColor Cyan
  uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth
  exit $LASTEXITCODE
}

if ($Demo) {
  $env:GARMIN_COACH_DEMO = "1"
} else {
  # Clear any demo flag left over from a previous -Demo run in this same shell.
  Remove-Item Env:GARMIN_COACH_DEMO -ErrorAction SilentlyContinue
}
$env:GARMIN_COACH_PORT = "$Port"

Write-Host "Starting Garmin Coach on http://127.0.0.1:$Port  (demo=$($Demo.IsPresent))" -ForegroundColor Green
uv run python -m server.app
