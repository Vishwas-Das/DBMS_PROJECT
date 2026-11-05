<#
.SYNOPSIS
  One-time local setup for LAE on Windows PowerShell 5.1+ (also works in PowerShell 7).

.DESCRIPTION
  1. .venv (Python 3.12) + `pip install -e backend[dev]`        (skip: -SkipInstall)
  2. frontend `npm ci`                                         (skip: -SkipFrontend)
  3. .env from .env.example if missing, with RANDOM secrets (LAE_HMAC_KEY, LAE_API_TOKEN).
     Secrets are written to .env only, never printed.
  4. docker compose up -d db, wait until the healthcheck reports healthy
  5. scripts/create_test_dbs.py (idempotent: lae_test + lae_test_* on existing volumes)
  6. alembic upgrade head on the target database
  7. seed the synthetic dataset (default scale, seed 42)       (skip: -SkipSeed)
  8. Ed25519 signing key in secrets\ if missing

  -DatabaseUrl lets you point steps 6-7 at another database (for example a lae_test_* DB).
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipFrontend,
    [switch]$SkipSeed,
    [switch]$SkipDocker,
    [string]$Scale = "default",
    [int]$Seed = 42,
    [string]$DatabaseUrl = ""
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$Py = Join-Path $Repo ".venv\Scripts\python.exe"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}
function Find-Docker {
    $cmd = Get-Command docker -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    if (Test-Path $default) { return $default }
    throw "docker not found (install Docker Desktop or add docker to PATH)"
}
function New-Secret { & $Py -c "import secrets; print(secrets.token_hex(32))" }

# ---------------------------------------------------------------- 1. python
if (-not (Test-Path $Py)) {
    Step "creating .venv (Python 3.12)"
    Invoke-Checked "py" @("-3.12", "-m", "venv", ".venv")
}
if (-not $SkipInstall) {
    Step "pip install -e backend[dev]"
    Invoke-Checked $Py @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $Py @("-m", "pip", "install", "-e", ".\backend[dev]")
}

# ---------------------------------------------------------------- 2. frontend
if (-not $SkipFrontend) {
    Step "npm ci (frontend)"
    Push-Location frontend
    try { Invoke-Checked "npm" @("ci") } finally { Pop-Location }
}

# ---------------------------------------------------------------- 3. .env
$EnvFile = Join-Path $Repo ".env"
if (-not (Test-Path $EnvFile)) {
    Step "creating .env from .env.example with random secrets (not printed)"
    $lines = Get-Content (Join-Path $Repo ".env.example")
    $hmac = New-Secret
    $token = New-Secret
    $out = foreach ($l in $lines) {
        if ($l -match '^LAE_HMAC_KEY=\s*$') { "LAE_HMAC_KEY=$hmac" }
        elseif ($l -match '^LAE_API_TOKEN=\s*$') { "LAE_API_TOKEN=$token" }
        else { $l }
    }
    # UTF-8 without BOM so pydantic-settings / docker compose read it cleanly
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]$out, (New-Object System.Text.UTF8Encoding($false)))
} else {
    # fill secrets that are present but EMPTY (e.g. "LAE_API_TOKEN="); never overwrite real values
    $lines = Get-Content $EnvFile
    $changed = $false
    $out = foreach ($l in $lines) {
        if ($l -match '^LAE_HMAC_KEY=\s*$') { $changed = $true; "LAE_HMAC_KEY=$(New-Secret)" }
        elseif ($l -match '^LAE_API_TOKEN=\s*$') { $changed = $true; "LAE_API_TOKEN=$(New-Secret)" }
        else { $l }
    }
    if ($changed) {
        Step ".env exists; filled EMPTY secrets with random values (not printed)"
        [System.IO.File]::WriteAllLines($EnvFile, [string[]]$out, (New-Object System.Text.UTF8Encoding($false)))
    } else {
        Step ".env exists (left unchanged)"
    }
}
function Get-EnvValue([string]$Name) {
    $line = Get-Content $EnvFile | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
    if ($line) { return $line.Substring($Name.Length + 1) } else { return "" }
}
# migrations + seeding need the OWNER role; the app itself runs as the restricted lae_app role
$AdminUrl = if ($DatabaseUrl) { $DatabaseUrl } else { Get-EnvValue "LAE_ADMIN_URL" }
if (-not $AdminUrl) { $AdminUrl = Get-EnvValue "LAE_DATABASE_URL" }

# ---------------------------------------------------------------- 4. database container
if (-not $SkipDocker) {
    $Docker = Find-Docker
    Step "docker compose up -d db"
    Invoke-Checked $Docker @("compose", "up", "-d", "db")
    Step "waiting for the db healthcheck"
    $cid = (& $Docker compose ps -q db).Trim()
    $deadline = (Get-Date).AddSeconds(120)
    do {
        $state = (& $Docker inspect -f "{{.State.Health.Status}}" $cid).Trim()
        if ($state -eq "healthy") { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($state -ne "healthy") { throw "database did not become healthy (state: $state)" }
    Write-Host "    db is healthy"
}

# ---------------------------------------------------------------- 5-7. schema + data
Step "creating test databases (idempotent)"
Invoke-Checked $Py @("scripts\create_test_dbs.py")

Step "alembic upgrade head (admin role)"
Invoke-Checked $Py @("-m", "alembic", "-c", "backend\alembic.ini", "-x", "url=$AdminUrl", "upgrade", "head")

if (-not $SkipSeed) {
    Step "seeding synthetic dataset (scale=$Scale seed=$Seed) + lae_app grants"
    # `lae db seed` runs the generator as the admin role, then SELECT lae.grant_app_role()
    Invoke-Checked (Join-Path $Repo ".venv\Scripts\lae.exe") @("db", "seed", "--scale", $Scale, "--seed", "$Seed", "--url", $AdminUrl)
}

# ---------------------------------------------------------------- 8. signing key
$Key = Join-Path $Repo "secrets\signing_key.pem"
if (-not (Test-Path $Key)) {
    Step "generating Ed25519 signing key"
    Invoke-Checked (Join-Path $Repo ".venv\Scripts\lae.exe") @("keys", "generate", "--out", $Key)
} else {
    Step "signing key exists (left unchanged)"
}
Step "setup complete. Next: scripts\start-backend.ps1 and scripts\start-frontend.ps1"
