<#
.SYNOPSIS
  Start the Vite dev server for the LAE UI on http://localhost:5173 (expects the API on :8000).
#>
[CmdletBinding()]
param([int]$Port = 5173)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $Repo "frontend")
if (-not (Test-Path "node_modules")) { throw "node_modules missing: run scripts\setup.ps1 (npm ci) first" }
& npm run dev -- --port $Port --strictPort
exit $LASTEXITCODE
