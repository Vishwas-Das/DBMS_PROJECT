<#
.SYNOPSIS
  All quality gates: ruff, ruff format, mypy, lint-imports, pytest unit, pytest integration/e2e
  per directory (each on its OWN test database), frontend typecheck/lint/test/build.

.DESCRIPTION
  Every gate runs even if an earlier one fails; a summary table is printed and the exit code is
  1 if any gate failed. Integration directories run in separate pytest processes because their
  fixtures DROP and recreate schemas `lae` and `app`. Each directory uses the database its own
  conftest accepts (discovery and verification refuse any other name):
      db -> lae_test, discovery -> lae_test_disc, services -> lae_test_lead,
      verification -> lae_test_verif, e2e -> lae_test_qa
  Do not run the DB gates while another workstream is using those databases.
  -Only db,e2e limits the DB gates to the listed directories.
  -SingleTestDb lae_test_qa runs the listed directories sequentially on that one database
  (directories that pin their own database will refuse it).
#>
[CmdletBinding()]
param(
    [string]$SingleTestDb = "",
    [string]$Only = "",
    [string]$ServerUrl = "postgresql+psycopg://lae_owner:lae_owner_dev@127.0.0.1:5433",
    [switch]$SkipFrontend,
    [switch]$SkipDb
)
$ErrorActionPreference = "Continue"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
$Bin = Join-Path $Repo ".venv\Scripts"
$results = New-Object System.Collections.Generic.List[object]

function Gate {
    param([string]$Name, [scriptblock]$Body)
    Write-Host "==> $Name" -ForegroundColor Cyan
    $t0 = Get-Date
    & $Body
    $code = $LASTEXITCODE
    $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    $results.Add([pscustomobject]@{ Gate = $Name; Exit = $code; Seconds = $secs })
}

Gate "ruff check" { & "$Bin\ruff.exe" check backend }
Gate "ruff format --check" { & "$Bin\ruff.exe" format --check backend }
# benchmarks/ and scripts/ have no pyproject of their own: lint them with the backend rules
Gate "ruff (benchmarks, scripts)" {
    & "$Bin\ruff.exe" check --config backend\pyproject.toml benchmarks scripts
    if ($LASTEXITCODE -eq 0) { & "$Bin\ruff.exe" format --check --config backend\pyproject.toml benchmarks scripts }
}
Gate "mypy (src/lae)" { Push-Location backend; & "$Bin\mypy.exe" src\lae; Pop-Location }  # from backend/ so [tool.mypy] applies
Gate "lint-imports" { Push-Location backend; & "$Bin\lint-imports.exe" --config pyproject.toml; Pop-Location }
Gate "pytest unit" { & $Py -m pytest backend\tests -m unit -q -p no:cacheprovider }

if (-not $SkipDb) {
    $dbFor = @{ db = "lae_test"; discovery = "lae_test_disc"; services = "lae_test_lead";
                verification = "lae_test_verif"; e2e = "lae_test_qa" }
    $dirs = @()
    $dirs += Get-ChildItem backend\tests\integration -Directory | Where-Object { $_.Name -ne "__pycache__" } |
        ForEach-Object { @{ Name = $_.Name; Path = "backend\tests\integration\$($_.Name)" } }
    if (Test-Path backend\tests\e2e) { $dirs += @{ Name = "e2e"; Path = "backend\tests\e2e" } }
    if ($Only) {
        $keep = $Only.Split(",") | ForEach-Object { $_.Trim() }
        $dirs = @($dirs | Where-Object { $keep -contains $_.Name })
    }
    $suffixes = ($dirs | ForEach-Object {
        $n = if ($SingleTestDb) { $SingleTestDb } elseif ($dbFor.ContainsKey($_.Name)) { $dbFor[$_.Name] } else { "lae_test_$($_.Name)" }
        $n -replace '^lae_test_?', '' } | Where-Object { $_ } | Sort-Object -Unique) -join ","
    if ($suffixes) { Gate "create test databases" { & $Py scripts\create_test_dbs.py --names $suffixes } }
    foreach ($d in $dirs) {
        $db = if ($SingleTestDb) { $SingleTestDb } elseif ($dbFor.ContainsKey($d.Name)) { $dbFor[$d.Name] } else { "lae_test_$($d.Name)" }
        $env:LAE_TEST_DATABASE_URL = "$ServerUrl/$db"
        # a developer .env sets LAE_READER_URL to the DEV database; process env wins over .env,
        # so pin every role to the test DB (CR qa-devops-engineer-1 D3)
        $env:LAE_READER_URL = "$ServerUrl/$db"
        $env:LAE_EXECUTOR_URL = "$ServerUrl/$db"
        $p = $d.Path
        Gate "pytest $($d.Name) [$db]" { & $Py -m pytest $p -m "integration or e2e" -q -p no:cacheprovider }
    }
    foreach ($v in "LAE_TEST_DATABASE_URL", "LAE_READER_URL", "LAE_EXECUTOR_URL") {
        Remove-Item "Env:\$v" -ErrorAction SilentlyContinue
    }
}

if (-not $SkipFrontend) {
    Push-Location frontend
    Gate "frontend typecheck" { npm run typecheck }
    Gate "frontend lint" { npm run lint }
    Gate "frontend test" { npx vitest run }
    Gate "frontend build" { npm run build }
    Pop-Location
}

Write-Host ""
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Exit -ne 0 })
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) gate(s) FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "all gates passed" -ForegroundColor Green
exit 0
