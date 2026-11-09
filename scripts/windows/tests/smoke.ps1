<#
  install.ps1 Windows smoke test (module-boundaries.md B-19, deploy.md section 1).
  Runs on windows-latest CI. Usage: smoke.ps1 -Wheel <built wheel> [-Root <temp folder>]
  Every step asserts; the first failure stops the script with a non-zero exit code.
#>
param(
    [Parameter(Mandatory = $true)][string]$Wheel,
    [string]$Root = '',
    [string]$Python = ''   # CI 의 setup-python 경로. 지원 범위(3.11-3.13) 밖 기본 Python 을 피한다
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

if (-not $Root) {
    $tmp = $env:RUNNER_TEMP
    if (-not $tmp) { $tmp = [System.IO.Path]::GetTempPath() }
    $Root = Join-Path $tmp 'mo-smoke'
}
$Wheel = (Resolve-Path -LiteralPath $Wheel).Path
$installer = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\install.ps1')).Path
$psExe = (Get-Command powershell.exe).Source     # Windows PowerShell 5.1
if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Root | Out-Null

function Assert-That([bool]$Condition, [string]$Message) {
    if (-not $Condition) { Write-Host ("SMOKE FAIL: " + $Message); exit 1 }
    Write-Host ("ok: " + $Message)
}

function Invoke-Installer([string[]]$ScriptArgs) {
    if ($Python -and ($ScriptArgs -notcontains '-Python')) { $ScriptArgs += @('-Python', $Python) }
    & $psExe -NoProfile -ExecutionPolicy Bypass -File $installer @ScriptArgs | Out-Host
    return $LASTEXITCODE
}

function Invoke-Tool([string]$Exe, [string[]]$ToolArgs) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lines = @(& $Exe @ToolArgs 2>&1 | ForEach-Object { $_.ToString() }); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    return [pscustomobject]@{ Code = $code; Text = ($lines -join "`n") }
}

function Get-PythonCount([string]$VenvPython, [string]$Db) {
    $code = "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);print(c.execute('select count(*) from jobs where job_id=?',('cal_smoke',)).fetchone()[0])"
    $r = Invoke-Tool $VenvPython @('-c', $code, $Db)
    return [int]($r.Text.Trim())
}

$bat = Join-Path $Root 'run_dyna.bat'
Set-Content -LiteralPath $bat -Value "@echo off`r`necho LS-DYNA dummy`r`n" -Encoding ASCII
$port = 18041

# ---- exit code 14: required value missing (non-interactive) ------------------------------------------
$code = Invoke-Installer @('-NonInteractive', '-NoSchedule', '-Wheel', $Wheel, '-InstallRoot', (Join-Path $Root 'e14'))
Assert-That ($code -eq 14) "missing -LsDynaBatchScript -> exit 14 (got $code)"

# ---- exit code 11: Python too old (fake interpreter) ------------------------------------------------
$fake = Join-Path $Root 'fakepy.cmd'
Set-Content -LiteralPath $fake -Value "@echo off`r`necho 3.8 64`r`necho C:\fake\python.exe`r`n" -Encoding ASCII
$code = Invoke-Installer @('-NonInteractive', '-NoSchedule', '-Python', $fake, '-Wheel', $Wheel, '-LsDynaBatchScript', $bat, '-InstallRoot', (Join-Path $Root 'e11'))
Assert-That ($code -eq 11) "Python 3.8 -> exit 11 (got $code)"

# ---- exit code 11: Python newer than the supported range (3.15 has no dependency wheels) ----------------
$fake2 = Join-Path $Root 'fakepy315.cmd'
Set-Content -LiteralPath $fake2 -Value "@echo off`r`necho 3.15 64`r`necho C:\fake\python.exe`r`n" -Encoding ASCII
$code = Invoke-Installer @('-NonInteractive', '-NoSchedule', '-Python', $fake2, '-Wheel', $Wheel, '-LsDynaBatchScript', $bat, '-InstallRoot', (Join-Path $Root 'e11b'))
Assert-That ($code -eq 11) "Python 3.15 (above supported range) -> exit 11 (got $code)"

# ---- fresh install ----------------------------------------------------------------------------------
$r1 = Join-Path $Root 'a'
$code = Invoke-Installer @('-NonInteractive', '-NoSchedule', '-Wheel', $Wheel, '-LsDynaBatchScript', $bat, '-InstallRoot', $r1, '-Port', "$port")
Assert-That ($code -eq 0) "fresh install -> exit 0 (got $code)"
$mo = Join-Path $r1 'venv\Scripts\matobserver.exe'
$cfg = Join-Path $r1 'config\matobserver.yaml'
$venvPy = Join-Path $r1 'venv\Scripts\python.exe'
$db = Join-Path $r1 'data\db\jobstore.sqlite'
Assert-That (Test-Path $mo) 'matobserver.exe installed in venv'
Assert-That (Test-Path $cfg) 'config file written'
Assert-That ((Test-Path (Join-Path $r1 'secrets\service.token')) -and (Test-Path (Join-Path $r1 'secrets\admin.token'))) 'token files written'
Assert-That (Test-Path $db) 'jobstore.sqlite created by db migrate'
$info = Get-Content -LiteralPath (Join-Path $r1 'install.json') -Raw | ConvertFrom-Json
Assert-That ($info.contract_schema_version -eq '1.3' -and $info.scheduled -eq $false) 'install.json content'
$cfgText = Get-Content -LiteralPath $cfg -Raw
Assert-That (($cfgText -notmatch 'service\.token') -and ($cfgText -match 'sha256')) 'config holds token hashes only'

$ver = Invoke-Tool $mo @('--version')
Assert-That ($ver.Code -eq 0 -and $ver.Text -match '1\.3') "matobserver --version: $($ver.Text)"

# ---- doctor (B-19: exit code <= 1; LS-DYNA / MatNexus items may be WARN or SKIP) -----------------------
$d = Invoke-Tool $mo @('doctor', '--json', '--config', $cfg)
Write-Host $d.Text
Assert-That ($d.Code -le 1) "doctor exit code <= 1 (got $($d.Code))"
$rep = $d.Text | ConvertFrom-Json
Assert-That ($rep.checks.Count -eq 11) 'doctor reports D01..D11'
foreach ($c in $rep.checks) { Assert-That ($c.status -ne 'FAIL') ("doctor " + $c.id + " " + $c.name + " = " + $c.status) }

$st = Invoke-Tool $mo @('status', '--json', '--config', $cfg)
Assert-That ($st.Code -eq 4) "status with nothing running -> exit 4 (got $($st.Code))"

# ---- process mode lifecycle: start -> health -> status -> stop ---------------------------------------
$s = Invoke-Tool $mo @('start', '--mode', 'process', '--config', $cfg)
Assert-That ($s.Code -eq 0) "start --mode process (got $($s.Code))"
$healthy = $false
for ($i = 0; $i -lt 90 -and -not $healthy; $i++) {
    try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/health" -TimeoutSec 2; $healthy = ($h.status -eq 'ok') } catch { Start-Sleep -Seconds 1 }
}
Assert-That $healthy 'server answers /v1/health'
$beat = $false
for ($i = 0; $i -lt 60 -and -not $beat; $i++) {
    $st = Invoke-Tool $mo @('status', '--json', '--config', $cfg)
    $beat = ($st.Code -eq 0) -and (($st.Text | ConvertFrom-Json).worker.last_heartbeat_age_s -ne $null)
    if (-not $beat) { Start-Sleep -Seconds 1 }
}
Assert-That $beat 'status exit 0 with worker heartbeat'
$stop = Invoke-Tool $mo @('stop', '--timeout', '30', '--config', $cfg)
Assert-That ($stop.Code -eq 0) "stop (got $($stop.Code))"
$st = Invoke-Tool $mo @('status', '--json', '--config', $cfg)
Assert-That ($st.Code -eq 4) "status after stop -> exit 4 (got $($st.Code))"

# ---- update (same wheel): config and DB are preserved --------------------------------------------------
Add-Content -LiteralPath $cfg -Value '# smoke-sentinel'
$ins = "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('insert into jobs (job_id,status,created_at,schema_version) values (?,?,?,?)',('cal_smoke','succeeded','2026-01-01T00:00:00Z','1.3'));c.commit();c.close()"
$null = Invoke-Tool $venvPy @('-c', $ins, $db)
Assert-That ((Get-PythonCount $venvPy $db) -eq 1) 'sentinel job row inserted'
$code = Invoke-Installer @('-Update', '-NonInteractive', '-Wheel', $Wheel, '-InstallRoot', $r1)
Assert-That ($code -eq 0) "update -> exit 0 (got $code)"
Assert-That ((Get-Content -LiteralPath $cfg -Raw) -match 'smoke-sentinel') 'config preserved by update'
Assert-That ((Get-PythonCount $venvPy $db) -eq 1) 'DB row preserved by update'
Assert-That (-not (Test-Path (Join-Path $r1 'venv.bak'))) 'venv.bak removed after update'
Assert-That (Test-Path $mo) 'matobserver.exe present after update'

# ---- uninstall: data is kept --------------------------------------------------------------------------
$code = Invoke-Installer @('-Uninstall', '-NonInteractive', '-InstallRoot', $r1)
Assert-That ($code -eq 0) "uninstall -> exit 0 (got $code)"
Assert-That (-not (Test-Path (Join-Path $r1 'venv'))) 'venv removed'
Assert-That (-not (Test-Path (Join-Path $r1 'install.json'))) 'install.json removed'
Assert-That ((Test-Path $cfg) -and (Test-Path $db)) 'config and data preserved by uninstall'

# ---- scheduled tasks: register and remove (needs permission; otherwise record the reason) ---------------
$r2 = Join-Path $Root 'b'
$code = Invoke-Installer @('-NonInteractive', '-Wheel', $Wheel, '-LsDynaBatchScript', $bat, '-InstallRoot', $r2, '-Port', '18042')
if ($code -eq 16) {
    Write-Host "::warning::Task Scheduler registration is not possible on this runner (exit 16); covered by unit tests for start/stop scheduler mode."
} else {
    Assert-That ($code -eq 0) "install with scheduled tasks -> exit 0 (got $code)"
    $tasks = @(Get-ScheduledTask -TaskPath '\matobserver\' -ErrorAction SilentlyContinue)
    Assert-That ($tasks.Count -eq 2) "two scheduled tasks registered (got $($tasks.Count))"
    $code = Invoke-Installer @('-Uninstall', '-NonInteractive', '-InstallRoot', $r2)
    Assert-That ($code -eq 0) "uninstall with tasks -> exit 0 (got $code)"
    $tasks = @(Get-ScheduledTask -TaskPath '\matobserver\' -ErrorAction SilentlyContinue)
    Assert-That ($tasks.Count -eq 0) 'scheduled tasks removed'
}

# ---- purge ---------------------------------------------------------------------------------------------
$r3 = Join-Path $Root 'c'
$code = Invoke-Installer @('-NonInteractive', '-NoSchedule', '-Wheel', $Wheel, '-LsDynaBatchScript', $bat, '-InstallRoot', $r3, '-Port', '18043')
Assert-That ($code -eq 0) "third install -> exit 0 (got $code)"
$code = Invoke-Installer @('-Uninstall', '-PurgeData', '-Force', '-NonInteractive', '-InstallRoot', $r3)
Assert-That ($code -eq 0) "uninstall -PurgeData -> exit 0 (got $code)"
Assert-That (-not (Test-Path $r3)) 'install root purged'

Write-Host 'SMOKE PASSED'
exit 0
