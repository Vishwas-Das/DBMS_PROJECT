<#
.SYNOPSIS
  Start the LAE API (uvicorn lae.api.main:app) on http://127.0.0.1:8000.
  Configuration comes from .env (LAE_*). -Reload for auto-reload during development.
#>
[CmdletBinding()]
param([int]$Port = 8000, [string]$BindHost = "127.0.0.1", [switch]$Reload)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw ".venv missing: run scripts\setup.ps1 first" }
if (-not (Test-Path (Join-Path $Repo ".env"))) { throw ".env missing: run scripts\setup.ps1 first" }
$uvArgs = @("-m", "uvicorn", "lae.api.main:app", "--host", $BindHost, "--port", "$Port")
if ($Reload) { $uvArgs += @("--reload", "--reload-dir", "backend\src") }
& $Py @uvArgs
exit $LASTEXITCODE
