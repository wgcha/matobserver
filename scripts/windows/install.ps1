<#
.SYNOPSIS
  matobserver Windows 설치 스크립트 (docs/contracts/deploy.md §1).

.DESCRIPTION
  사용자 영역(%LOCALAPPDATA%)에 venv 를 만들고 matobserver 를 설치한 뒤 설정·토큰·폴더·작업 스케줄러를 준비한다.
  관리자 권한은 -AtStartup 또는 보호 경로(InstallRoot)일 때만 필요하다. Python 은 자동 설치하지 않는다.

  powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [옵션]

  종료 코드: 0 성공 / 10 PowerShell·OS / 11 Python / 12 venv / 13 다운로드·해시·pip / 14 설정 / 15 폴더 /
             16 작업 스케줄러 / 17 doctor FAIL / 18 업데이트 마이그레이션 실패(롤백) / 19 제거 일부 실패 /
             20 관리자 권한 필요 / 21 설치 상태 불일치 / 22 버전 규칙 위반 / 23 실행 중 잡 / 1 기타
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [string]$DataRoot = '',
    [string]$Python = '',
    [string]$Tag = '',
    [string]$Repo = '',
    [string]$GitHubTokenFile = '',
    [string]$Wheel = '',
    [string]$WheelDir = '',
    [string]$LsDynaBatchScript = '',
    [int]$Ncpu = 0,
    [string]$MatNexusUrl = '',
    [string]$MatNexusTokenFile = '',
    [int]$Port = 8040,
    [switch]$NonInteractive,
    [switch]$NoSchedule,
    [switch]$AtStartup,
    [switch]$Update,
    [switch]$AllowMajor,
    [switch]$Uninstall,
    [switch]$PurgeData,
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# U-1 미결: 릴리스 저장소(owner/name)가 확정되면 이 값을 바꾼다. -Repo 로 덮어쓸 수 있다.
$script:DefaultRepo = 'OWNER/matobserver'
# 지원 Python 범위. 상한은 의존 패키지(lasso-python -> h5py 등) 휠이 있는 최신 버전. 환경변수 MATOBSERVER_PYTHON_MAX 로 올릴 수 있다.
$script:PythonMin = '3.11'
$script:PythonMax = '3.13'
if ($env:MATOBSERVER_PYTHON_MAX -match '^\d+\.\d+$') { $script:PythonMax = $env:MATOBSERVER_PYTHON_MAX }
$script:TaskPath = '\matobserver\'
$script:TaskNames = @('matobserver-server', 'matobserver-worker')
$script:TranscriptOn = $false

# ---------------------------------------------------------------------------------------------- 공통 보조
function Write-Step([string]$Message) { Write-Host ("==> " + $Message) }
function Write-Warn([string]$Message) { Write-Host ("[경고] " + $Message) }

function Stop-Install([int]$Code, [string]$Message) {
    if ($Message) {
        if ($Code -eq 0) { Write-Host $Message } else { Write-Host ("[오류 " + $Code + "] " + $Message) }
    }
    if ($script:TranscriptOn) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:TranscriptOn = $false
    }
    exit $Code
}

function Invoke-Native {
    # 네이티브 명령 실행. stderr 출력이 오류로 취급되지 않게 하고 종료 코드와 출력을 돌려준다.
    param([string]$Exe, [string[]]$Arguments = @(), [switch]$Quiet)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $Exe @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $code = $LASTEXITCODE
    } catch {
        $lines = @($_.Exception.Message)
        $code = 1
    } finally {
        $ErrorActionPreference = $old
    }
    if (-not $Quiet) { foreach ($l in $lines) { Write-Host $l } }
    return [pscustomobject]@{ Code = $code; Output = ($lines -join "`n") }
}

function ConvertTo-YamlString([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Test-ProtectedPath([string]$Path) {
    foreach ($v in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData, $env:SystemRoot)) {
        if ($v -and $Path.StartsWith($v, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-Sha256Hex([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text))
        return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally { $sha.Dispose() }
}

function New-RandomToken {
    $bytes = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Write-TextFile([string]$Path, [string]$Text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Set-OwnerOnlyAcl([string]$Path, [switch]$Directory) {
    # 현재 사용자만 읽기·쓰기. 상속 제거. 폴더는 하위 항목이 상속하도록 (OI)(CI).
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $grant = $user + ':(F)'
    if ($Directory) { $grant = $user + ':(OI)(CI)(F)' }
    $r = Invoke-Native -Exe 'icacls.exe' -Arguments @($Path, '/inheritance:r', '/grant:r', $grant) -Quiet
    if ($r.Code -ne 0) { throw ("icacls 실패: " + $Path) }
}

function Read-Answer([string]$Prompt, [string]$Default) {
    if ($NonInteractive) { return $Default }
    $suffix = ''
    if ($Default) { $suffix = ' [' + $Default + ']' }
    $a = Read-Host ($Prompt + $suffix)
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    return $a.Trim()
}

function Initialize-Paths {
    $script:Venv = Join-Path $InstallRoot 'venv'
    $script:VenvPython = Join-Path (Join-Path $script:Venv 'Scripts') 'python.exe'
    $script:MoExe = Join-Path (Join-Path $script:Venv 'Scripts') 'matobserver.exe'
    $script:ConfigDir = Join-Path $InstallRoot 'config'
    $script:ConfigFile = Join-Path $script:ConfigDir 'matobserver.yaml'
    $script:SecretsDir = Join-Path $InstallRoot 'secrets'
    $script:LogsDir = Join-Path $InstallRoot 'logs'
    $script:InstallJson = Join-Path $InstallRoot 'install.json'
}

function Read-InstallJson {
    if (-not (Test-Path -LiteralPath $script:InstallJson)) { return $null }
    try { return (Get-Content -LiteralPath $script:InstallJson -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

# ---------------------------------------------------------------------------------------------- 1. 환경
function Test-Environment {
    Write-Step '환경 확인'
    $v = $PSVersionTable.PSVersion
    if ($v.Major -lt 5 -or ($v.Major -eq 5 -and $v.Minor -lt 1)) {
        Stop-Install 10 ('PowerShell 5.1 이상이 필요합니다 (현재 ' + $v + ').')
    }
    $isWin = $true
    if ($PSVersionTable.ContainsKey('Platform') -and $PSVersionTable.Platform -ne 'Win32NT') { $isWin = $false }
    if (-not $isWin) { Stop-Install 10 'Windows 에서만 실행할 수 있습니다.' }
    if ([Environment]::OSVersion.Version.Major -lt 10) { Stop-Install 10 'Windows 10 이상이 필요합니다.' }
    if (-not [Environment]::Is64BitOperatingSystem) { Stop-Install 10 '64비트 Windows 가 필요합니다.' }
    if (-not $Update -and -not $Uninstall -and $NonInteractive -and -not $LsDynaBatchScript) {
        Stop-Install 14 '-NonInteractive 에는 -LsDynaBatchScript 가 필요합니다.'
    }
    if ($PurgeData -and -not $Uninstall) { Stop-Install 1 '-PurgeData 는 -Uninstall 과 함께만 쓸 수 있습니다.' }
    if ($Update -and $Uninstall) { Stop-Install 1 '-Update 와 -Uninstall 은 함께 쓸 수 없습니다.' }
    $needAdmin = $false
    if ($AtStartup -and -not $Uninstall) { $needAdmin = $true }
    if (Test-ProtectedPath $InstallRoot) { $needAdmin = $true }
    if ($needAdmin -and -not (Test-Admin)) {
        Stop-Install 20 '관리자 권한이 필요합니다 (-AtStartup 또는 보호 경로 설치). 관리자 PowerShell 에서 다시 실행하세요.'
    }
}

# ---------------------------------------------------------------------------------------------- 2. Python
function Get-PythonInfo {
    # (실행 파일 + 선행 인자) 후보를 한 번 실행해 {Major, Minor, Bits, Path} 를 얻는다. 실행 실패·형식 불일치는 $null.
    param([string]$Exe, [string[]]$PreArgs = @())
    $probe = "import sys,struct;print('%d.%d %d' % (sys.version_info[0],sys.version_info[1],struct.calcsize('P')*8));print(sys.executable)"
    $r = Invoke-Native -Exe $Exe -Arguments ($PreArgs + @('-c', $probe)) -Quiet
    if ($r.Code -ne 0) { return $null }
    $lines = @($r.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($lines.Count -lt 1 -or $lines[0] -notmatch '^(\d+)\.(\d+) (\d+)$') { return $null }
    $real = $Exe
    if ($lines.Count -ge 2 -and (Test-Path -LiteralPath $lines[1])) { $real = $lines[1] }
    return [pscustomobject]@{ Major = [int]$Matches[1]; Minor = [int]$Matches[2]; Bits = [int]$Matches[3]; Path = $real }
}

function Get-PythonCandidates {
    # 설치된 인터프리터 후보 경로. `py -0p` (Python 런처 목록) + PATH 의 python/python3.
    $paths = New-Object System.Collections.Generic.List[string]
    if (Get-Command 'py' -ErrorAction SilentlyContinue) {
        $r = Invoke-Native -Exe 'py' -Arguments @('-0p') -Quiet
        foreach ($line in ($r.Output -split "`n")) {
            if ($line -match '(?i)([A-Za-z]:\\[^\r\n]*?python[w]?\.exe)\s*$') { $paths.Add($Matches[1].Trim()) }
        }
    }
    foreach ($n in @('python', 'python3')) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and $cmd.Source) { $paths.Add([string]$cmd.Source) }
    }
    return @($paths | Select-Object -Unique)
}

function Find-Python {
    # 지원 범위(PythonMin..PythonMax)에서 가장 높은 64비트 버전을 고른다. 범위 밖(예: 의존 휠이 아직 없는 최신 버전)만 있으면 안내 후 종료(11).
    Write-Step 'Python 확인'
    $found = New-Object System.Collections.Generic.List[object]
    if ($Python) {
        $info = Get-PythonInfo -Exe $Python
        if ($info) { $found.Add($info) }
    } else {
        foreach ($p in (Get-PythonCandidates)) {
            $info = Get-PythonInfo -Exe $p
            if ($info) { $found.Add($info) }
        }
    }
    $minV = [version]$script:PythonMin
    $maxV = [version]$script:PythonMax
    $ok = @($found | Where-Object { $_.Bits -eq 64 -and ([version]('{0}.{1}' -f $_.Major, $_.Minor)) -ge $minV -and ([version]('{0}.{1}' -f $_.Major, $_.Minor)) -le $maxV })
    if ($ok.Count -gt 0) {
        $best = $ok | Sort-Object -Property @{ Expression = { [version]('{0}.{1}' -f $_.Major, $_.Minor) } } -Descending | Select-Object -First 1
        Write-Host ("Python {0}.{1} 사용: {2}" -f $best.Major, $best.Minor, $best.Path)
        return $best.Path
    }
    $msg = ('지원하는 Python 은 {0} ~ {1} (64비트) 입니다.' -f $script:PythonMin, $script:PythonMax)
    if ($found.Count -gt 0) {
        $seen = ($found | ForEach-Object { '{0}.{1} ({2}비트)' -f $_.Major, $_.Minor, $_.Bits }) -join ', '
        $msg += ' 발견된 버전: ' + $seen + ' — 범위 밖입니다.'
    }
    $msg += " https://www.python.org/downloads/windows/ 에서 지원 범위의 Python 을 설치하거나 -Python 으로 경로를 지정하세요 (자동 설치하지 않습니다)."
    Stop-Install 11 $msg
}

# ---------------------------------------------------------------------------------------------- 5. 패키지 원본
function Get-GitHubHeaders {
    $h = @{ 'User-Agent' = 'matobserver-install'; 'Accept' = 'application/vnd.github+json' }
    if ($GitHubTokenFile) {
        if (-not (Test-Path -LiteralPath $GitHubTokenFile)) { Stop-Install 13 '-GitHubTokenFile 을 읽을 수 없습니다.' }
        $tok = (Get-Content -LiteralPath $GitHubTokenFile -TotalCount 1 -Encoding UTF8).Trim()
        $h['Authorization'] = 'Bearer ' + $tok   # 값은 로그에 쓰지 않는다
    }
    return $h
}

function Get-VersionFromWheelName([string]$Path) {
    $n = [System.IO.Path]::GetFileName($Path)
    if ($n -match '^matobserver-(\d+\.\d+\.\d+)-') { return $Matches[1] }
    return $null
}

function Resolve-Package {
    # 설치할 wheel 경로와 버전을 돌려준다 (우선순위 -Wheel > -Tag > 최신 정식 릴리스).
    param([string]$Workdir)
    if ($Wheel) {
        if (-not (Test-Path -LiteralPath $Wheel)) { Stop-Install 13 ('-Wheel 파일이 없습니다: ' + $Wheel) }
        $full = (Resolve-Path -LiteralPath $Wheel).Path
        return [pscustomobject]@{ Path = $full; Version = (Get-VersionFromWheelName $full); Tag = $null }
    }
    $repo = $Repo
    if (-not $repo) { $repo = $script:DefaultRepo }
    if ($repo -like 'OWNER/*') { Stop-Install 13 '릴리스 저장소가 정해지지 않았습니다. -Repo owner/name 을 주거나 -Wheel 로 로컬 wheel 을 지정하세요.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $headers = Get-GitHubHeaders
    if ($Tag) {
        if ($Tag -notmatch '^v\d+\.\d+\.\d+(-rc\.\d+)?$') { Stop-Install 13 ('태그 형식이 올바르지 않습니다: ' + $Tag) }
        $url = "https://api.github.com/repos/$repo/releases/tags/$Tag"
    } else {
        $url = "https://api.github.com/repos/$repo/releases/latest"   # 사전 릴리스 제외
    }
    try { $rel = Invoke-RestMethod -Uri $url -Headers $headers -UseBasicParsing }
    catch { Stop-Install 13 ('릴리스 조회 실패: ' + $_.Exception.Message) }
    $tagName = [string]$rel.tag_name
    $whl = @($rel.assets | Where-Object { $_.name -like 'matobserver-*-py3-none-any.whl' } | Select-Object -First 1)
    $sums = @($rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1)
    if ($whl.Count -eq 0 -or $sums.Count -eq 0) { Stop-Install 13 ('릴리스 ' + $tagName + ' 에 wheel 또는 SHA256SUMS 자산이 없습니다.') }
    $dl = @{ 'User-Agent' = 'matobserver-install'; 'Accept' = 'application/octet-stream' }
    if ($headers.ContainsKey('Authorization')) { $dl['Authorization'] = $headers['Authorization'] }
    $whlPath = Join-Path $Workdir ([string]$whl[0].name)
    $sumsPath = Join-Path $Workdir 'SHA256SUMS'
    try {
        Invoke-WebRequest -Uri ([string]$whl[0].url) -Headers $dl -OutFile $whlPath -UseBasicParsing
        Invoke-WebRequest -Uri ([string]$sums[0].url) -Headers $dl -OutFile $sumsPath -UseBasicParsing
    } catch { Stop-Install 13 ('다운로드 실패: ' + $_.Exception.Message) }
    $expected = $null
    foreach ($line in (Get-Content -LiteralPath $sumsPath -Encoding UTF8)) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$' -and $Matches[2].Trim() -eq [string]$whl[0].name) { $expected = $Matches[1].ToLowerInvariant() }
    }
    $actual = (Get-FileHash -LiteralPath $whlPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $expected -or $expected -ne $actual) { Stop-Install 13 'wheel SHA-256 이 SHA256SUMS 와 일치하지 않습니다. 설치를 중단합니다.' }
    Write-Host ("릴리스 {0} 의 {1} 해시 검증 통과" -f $tagName, $whl[0].name)
    return [pscustomobject]@{ Path = $whlPath; Version = (Get-VersionFromWheelName $whlPath); Tag = $tagName }
}

function New-VenvAndInstall([string]$PythonExe, [object]$Pkg) {
    Write-Step 'venv 생성'
    $r = Invoke-Native -Exe $PythonExe -Arguments @('-m', 'venv', $script:Venv)
    if ($r.Code -ne 0 -or -not (Test-Path -LiteralPath $script:VenvPython)) { return 12 }
    $r = Invoke-Native -Exe $script:VenvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip') -Quiet
    if ($r.Code -ne 0) { Write-Warn 'pip 최신화에 실패했습니다 (계속 진행).' }
    Write-Step '패키지 설치'
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check')
    if ($WheelDir) { $pipArgs += @('--no-index', '--find-links', $WheelDir) }
    $pipArgs += $Pkg.Path
    $r = Invoke-Native -Exe $script:VenvPython -Arguments $pipArgs
    if ($r.Code -ne 0 -or -not (Test-Path -LiteralPath $script:MoExe)) { return 13 }
    return 0
}

function Get-InstalledVersions {
    $r = Invoke-Native -Exe $script:VenvPython -Arguments @('-c', 'import matobserver;print(matobserver.__version__);print(matobserver.CONTRACT_SCHEMA_VERSION)') -Quiet
    $l = @($r.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($r.Code -ne 0 -or $l.Count -lt 2) { return $null }
    return [pscustomobject]@{ Package = $l[0]; Contract = $l[1] }
}

function Copy-PluginRegistry([string]$Destination, [switch]$KeepOld) {
    $src = Join-Path (Join-Path (Join-Path $script:Venv 'share') 'matobserver') 'plugins.d'
    if (-not (Test-Path -LiteralPath $src)) { return $false }
    if ((Test-Path -LiteralPath $Destination) -and $KeepOld) {
        $old = $Destination + '.bak'
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
        Move-Item -LiteralPath $Destination -Destination $old
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $src '*.yaml') -Destination $Destination -Force
    return $true
}

# ---------------------------------------------------------------------------------------------- 6. 설정
function New-ConfigAndSecrets {
    Write-Step '설정 생성'
    $script_ = $LsDynaBatchScript
    if (-not $script_) {
        if ($NonInteractive) { Stop-Install 14 '-NonInteractive 에는 -LsDynaBatchScript 가 필요합니다.' }
        $script_ = Read-Answer 'LS-DYNA 사내 배치 스크립트 경로' ''
        if (-not $script_) { Stop-Install 14 'LS-DYNA 배치 스크립트 경로가 필요합니다.' }
    }
    if (-not (Test-Path -LiteralPath $script_)) { Write-Warn ('배치 스크립트가 아직 없습니다: ' + $script_ + ' (doctor 가 FAIL 로 알립니다)') }
    $cpu = $Ncpu
    if ($cpu -le 0) { $cpu = [int](Read-Answer 'LS-DYNA ncpu' '4') }
    $mnUrl = $MatNexusUrl
    if (-not $script:MnUrlGiven) { $mnUrl = Read-Answer 'MatNexus URL (비워도 됨)' '' }
    $mnTok = $MatNexusTokenFile
    if (-not $script:MnTokGiven) { $mnTok = Read-Answer 'MatNexus 토큰 파일 경로 (비워도 됨)' '' }

    try {
        New-Item -ItemType Directory -Force -Path $script:ConfigDir, $script:SecretsDir | Out-Null
        Set-OwnerOnlyAcl $script:SecretsDir -Directory
        $svcTok = New-RandomToken
        $admTok = New-RandomToken
        $svcFile = Join-Path $script:SecretsDir 'service.token'
        $admFile = Join-Path $script:SecretsDir 'admin.token'
        Write-TextFile $svcFile $svcTok
        Write-TextFile $admFile $admTok
        Set-OwnerOnlyAcl $svcFile
        Set-OwnerOnlyAcl $admFile
        $svcSha = Get-Sha256Hex $svcTok
        $admSha = Get-Sha256Hex $admTok
        $svcTok = $null; $admTok = $null

        $y = New-Object System.Collections.Generic.List[string]
        $y.Add('# matobserver 설정 — install.ps1 이 생성. 지정하지 않은 키는 코드 기본값 (docs/contracts/module-boundaries.md §6)')
        $y.Add('paths:')
        $y.Add('  data_dir: ' + (ConvertTo-YamlString $DataRoot))
        $y.Add('  work_dir: ' + (ConvertTo-YamlString (Join-Path $DataRoot 'work')))
        $y.Add('recipes:')
        $y.Add('  store_dir: ' + (ConvertTo-YamlString (Join-Path $DataRoot 'recipes')))
        $y.Add('jobstore:')
        $y.Add('  sqlite_path: ' + (ConvertTo-YamlString (Join-Path (Join-Path $DataRoot 'db') 'jobstore.sqlite')))
        $y.Add('logging:')
        $y.Add('  dir: ' + (ConvertTo-YamlString $script:LogsDir))
        $y.Add('runtime:')
        $y.Add('  pid_dir: ' + (ConvertTo-YamlString (Join-Path $DataRoot 'run')))
        $y.Add('api:')
        $y.Add('  host: 127.0.0.1')
        $y.Add('  port: ' + $Port)
        $y.Add('cli:')
        $y.Add('  server_url: ' + (ConvertTo-YamlString ('http://127.0.0.1:' + $Port)))
        $y.Add('registry:')
        $y.Add('  paths:')
        $y.Add('    - ' + (ConvertTo-YamlString (Join-Path $script:ConfigDir 'plugins.d')))
        $y.Add('solver:')
        $y.Add('  lsdyna:')
        $y.Add('    script: ' + (ConvertTo-YamlString $script_))
        $y.Add('    ncpu: ' + $cpu)
        if ($mnUrl -or $mnTok) {
            $y.Add('matnexus:')
            if ($mnUrl) { $y.Add('  base_url: ' + (ConvertTo-YamlString $mnUrl)) }
            if ($mnTok) { $y.Add('  token: ' + (ConvertTo-YamlString ('file:' + $mnTok))) }
        }
        $y.Add('auth:')
        $y.Add('  service_tokens:')
        $y.Add('    - {name: service, sha256: ' + $svcSha + '}')
        $y.Add('  admin_tokens:')
        $y.Add('    - {name: admin, sha256: ' + $admSha + '}')
        Write-TextFile $script:ConfigFile (($y -join "`r`n") + "`r`n")
    } catch {
        Stop-Install 14 ('설정 생성 실패: ' + $_.Exception.Message)
    }
    if (-not (Copy-PluginRegistry (Join-Path $script:ConfigDir 'plugins.d'))) {
        Stop-Install 14 '패키지에서 플러그인 레지스트리(share\matobserver\plugins.d)를 찾지 못했습니다.'
    }
    [Environment]::SetEnvironmentVariable('MATOBSERVER_CONFIG', $script:ConfigFile, 'User')
    $env:MATOBSERVER_CONFIG = $script:ConfigFile
    Write-Host ('설정: ' + $script:ConfigFile)
    Write-Host ('토큰: ' + $script:SecretsDir + ' (현재 사용자만 읽기)')
}

# ---------------------------------------------------------------------------------------------- 7. 폴더
function New-DataFolders {
    Write-Step '폴더 생성'
    $dirs = @((Join-Path $DataRoot 'work'), (Join-Path $DataRoot 'recipes'), (Join-Path $DataRoot 'db'),
              $script:LogsDir, (Join-Path $DataRoot 'run'))
    foreach ($d in $dirs) {
        try {
            New-Item -ItemType Directory -Force -Path $d | Out-Null
            $probe = Join-Path $d ('.write-test-' + [guid]::NewGuid().ToString('N'))
            [System.IO.File]::WriteAllText($probe, 'x')
            Remove-Item -LiteralPath $probe -Force
        } catch { Stop-Install 15 ('폴더를 만들거나 쓸 수 없습니다: ' + $d + ' (' + $_.Exception.Message + ')') }
    }
    $r = Invoke-Native -Exe $script:MoExe -Arguments @('db', 'migrate', '--config', $script:ConfigFile)
    if ($r.Code -ne 0) { Stop-Install 15 'DB 초기화(matobserver db migrate)에 실패했습니다.' }
}

# ---------------------------------------------------------------------------------------------- 8. 작업 스케줄러
function Register-MatobserverTasks {
    Write-Step '작업 스케줄러 등록'
    try {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -Hidden
        if ($AtStartup) {
            $trigger = New-ScheduledTaskTrigger -AtStartup
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
        } else {
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
            $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        }
        foreach ($pair in @(@('matobserver-server', 'serve'), @('matobserver-worker', 'worker'))) {
            $arg = $pair[1] + ' --config "' + $script:ConfigFile + '" --run-mode scheduler'
            $action = New-ScheduledTaskAction -Execute $script:MoExe -Argument $arg -WorkingDirectory $InstallRoot
            Register-ScheduledTask -TaskName $pair[0] -TaskPath $script:TaskPath -Action $action -Trigger $trigger `
                -Settings $settings -Principal $principal -Force | Out-Null
        }
        foreach ($n in $script:TaskNames) { Start-ScheduledTask -TaskPath $script:TaskPath -TaskName $n }
        Wait-ServerHealthy 30
    } catch {
        Stop-Install 16 ('작업 스케줄러 등록 실패: ' + $_.Exception.Message + ' (-NoSchedule 로 건너뛰고 `matobserver start` 를 쓸 수 있습니다)')
    }
}

function Wait-ServerHealthy([int]$TimeoutSeconds) {
    # 방금 시작한 서버가 응답할 때까지 기다린다 (doctor D10 이 기동 도중을 FAIL 로 보지 않게). 시간이 지나도 계속 진행한다.
    $url = 'http://127.0.0.1:' + $Port + '/v1/health'
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    Write-Warn ('서버가 ' + $TimeoutSeconds + '초 안에 응답하지 않았습니다.')
}

function Unregister-MatobserverTasks {
    $failed = $false
    foreach ($n in $script:TaskNames) {
        try {
            $t = Get-ScheduledTask -TaskPath $script:TaskPath -TaskName $n -ErrorAction SilentlyContinue
            if ($t) {
                Stop-ScheduledTask -TaskPath $script:TaskPath -TaskName $n -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskPath $script:TaskPath -TaskName $n -Confirm:$false
            }
        } catch { $failed = $true; Write-Warn ('작업 해제 실패: ' + $n + ' (' + $_.Exception.Message + ')') }
    }
    return (-not $failed)
}

function Set-TasksEnabled([bool]$Enabled) {
    foreach ($n in $script:TaskNames) {
        try {
            $t = Get-ScheduledTask -TaskPath $script:TaskPath -TaskName $n -ErrorAction SilentlyContinue
            if (-not $t) { continue }
            if ($Enabled) { Enable-ScheduledTask -TaskPath $script:TaskPath -TaskName $n | Out-Null }
            else { Disable-ScheduledTask -TaskPath $script:TaskPath -TaskName $n | Out-Null }
        } catch { Write-Warn ('작업 상태 변경 실패: ' + $n) }
    }
}

# ---------------------------------------------------------------------------------------------- 9·10. 점검·기록
function Invoke-Doctor {
    Write-Step '점검 (matobserver doctor)'
    $r = Invoke-Native -Exe $script:MoExe -Arguments @('doctor', '--config', $script:ConfigFile)
    return $r.Code
}

function Write-InstallJson([string]$Tag_, [bool]$Scheduled) {
    $v = Get-InstalledVersions
    $obj = [ordered]@{
        package_version         = $(if ($v) { $v.Package } else { $null })
        contract_schema_version = $(if ($v) { $v.Contract } else { $null })
        tag                     = $Tag_
        installed_at            = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        install_root            = $InstallRoot
        data_root               = $DataRoot
        python                  = $script:VenvPython
        scheduled               = $Scheduled
        at_startup              = [bool]$AtStartup
    }
    Write-TextFile $script:InstallJson (($obj | ConvertTo-Json) + "`r`n")
}

# ---------------------------------------------------------------------------------------------- 신규 설치
function Invoke-FreshInstall {
    if (Test-Path -LiteralPath $script:InstallJson) {
        Stop-Install 21 ('이미 설치되어 있습니다 (' + $script:InstallJson + '). 갱신은 -Update 를 쓰세요.')
    }
    $py = Find-Python
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    $work = Join-Path $InstallRoot 'download'
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $pkg = Resolve-Package -Workdir $work
    $code = New-VenvAndInstall $py $pkg
    if ($code -eq 12) { Stop-Install 12 'venv 생성에 실패했습니다.' }
    if ($code -ne 0) { Stop-Install 13 '패키지 설치(pip)에 실패했습니다.' }
    New-ConfigAndSecrets
    New-DataFolders
    $scheduled = $false
    if (-not $NoSchedule) { Register-MatobserverTasks; $scheduled = $true }
    $dc = Invoke-Doctor
    Write-InstallJson $pkg.Tag $scheduled
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    if ($dc -ge 2) { Stop-Install 17 'doctor 에 FAIL 이 있습니다. 설치는 유지됩니다 — 위 표를 확인하세요.' }
    Write-Host ''
    Write-Host '설치 완료.'
    Write-Host ('  설정   : ' + $script:ConfigFile)
    Write-Host ('  토큰   : ' + $script:SecretsDir)
    Write-Host ('  시작   : ' + $script:MoExe + ' start --config "' + $script:ConfigFile + '"')
    Stop-Install 0 ''
}

# ---------------------------------------------------------------------------------------------- 업데이트
function Invoke-UpdateInstall {
    $info = Read-InstallJson
    if (-not $info) { Stop-Install 21 ('설치 정보(install.json)가 없습니다: ' + $InstallRoot + ' — 신규 설치를 먼저 하세요.') }
    if ($info.data_root -and -not $script:DataRootGiven) { $script:DataRoot = [string]$info.data_root }
    $py = Find-Python
    $work = Join-Path $InstallRoot 'download'
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $pkg = Resolve-Package -Workdir $work

    # 2. 버전 규칙 (deploy.md §3.3)
    $old = [version]([string]$info.package_version)
    if ($pkg.Version) {
        $new = [version]$pkg.Version
        if ($new -lt $old) { Stop-Install 22 ("다운그레이드는 지원하지 않습니다 ({0} -> {1})." -f $old, $new) }
        if ($new.Major -gt $old.Major -and -not $AllowMajor) { Stop-Install 22 ("MAJOR 상향({0} -> {1})은 -AllowMajor 가 필요합니다." -f $old, $new) }
    }

    # 3. 정지 (실행 중 잡이 있으면 -Force 없이는 중단)
    Write-Step '정지'
    if (Test-Path -LiteralPath $script:MoExe) {
        Set-TasksEnabled $false
        $stopArgs = @('stop', '--config', $script:ConfigFile, '--timeout', '30')
        if ($Force) { $stopArgs += '--force' }
        $r = Invoke-Native -Exe $script:MoExe -Arguments $stopArgs
        if ($r.Code -eq 4) {
            Set-TasksEnabled $true
            Stop-Install 23 '실행 중인 잡이 있어 업데이트를 중단합니다 (-Force 로 강제 정지 가능).'
        }
    }

    # 4. venv 교체
    $bak = $script:Venv + '.bak'
    if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Recurse -Force }
    Rename-Item -LiteralPath $script:Venv -NewName 'venv.bak'
    $code = New-VenvAndInstall $py $pkg
    if ($code -ne 0) {
        if (Test-Path -LiteralPath $script:Venv) { Remove-Item -LiteralPath $script:Venv -Recurse -Force -ErrorAction SilentlyContinue }
        Rename-Item -LiteralPath $bak -NewName 'venv'
        Set-TasksEnabled $true
        Stop-Install $code '새 venv 설치에 실패해 이전 venv 로 되돌렸습니다.'
    }

    # 5. DB 백업 -> 마이그레이션 (실패 시 복원)
    Write-Step 'DB 백업·마이그레이션'
    $db = Join-Path (Join-Path $DataRoot 'db') 'jobstore.sqlite'
    $dbBak = $db + '.bak-' + [string]$info.package_version
    $hadDb = Test-Path -LiteralPath $db
    if ($hadDb) { Copy-Item -LiteralPath $db -Destination $dbBak -Force }
    foreach ($ext in @('-wal', '-shm')) { if (Test-Path -LiteralPath ($db + $ext)) { Copy-Item -LiteralPath ($db + $ext) -Destination ($dbBak + $ext) -Force } }
    $r = Invoke-Native -Exe $script:MoExe -Arguments @('db', 'migrate', '--no-backup', '--config', $script:ConfigFile)
    if ($r.Code -ne 0) {
        Write-Warn '마이그레이션 실패 — DB 와 venv 를 복원합니다.'
        if ($hadDb) {
            foreach ($ext in @('', '-wal', '-shm')) {
                if (Test-Path -LiteralPath ($db + $ext)) { Remove-Item -LiteralPath ($db + $ext) -Force -ErrorAction SilentlyContinue }
                if (Test-Path -LiteralPath ($dbBak + $ext)) { Copy-Item -LiteralPath ($dbBak + $ext) -Destination ($db + $ext) -Force }
            }
        }
        Remove-Item -LiteralPath $script:Venv -Recurse -Force -ErrorAction SilentlyContinue
        Rename-Item -LiteralPath $bak -NewName 'venv'
        Set-TasksEnabled $true
        Stop-Install 18 '업데이트 중 마이그레이션이 실패했습니다 (롤백 완료).'
    }

    # 6. 보존: 설정·토큰·레시피·DB·작업 폴더. 플러그인 레지스트리는 코드 버전에 묶여 있어 새 것으로 바꾸고 이전 것은 plugins.d.bak 에 남긴다.
    [void](Copy-PluginRegistry (Join-Path $script:ConfigDir 'plugins.d') -KeepOld)

    # 7. 점검 -> 재시작 -> 기록 -> venv.bak 삭제
    $dc = Invoke-Doctor
    $scheduled = [bool]$info.scheduled
    Set-TasksEnabled $true
    if ($scheduled) {
        foreach ($n in $script:TaskNames) { try { Start-ScheduledTask -TaskPath $script:TaskPath -TaskName $n } catch { Write-Warn ('작업 시작 실패: ' + $n) } }
    }
    $tagOut = $pkg.Tag
    if (-not $tagOut) { $tagOut = [string]$info.tag }
    Write-InstallJson $tagOut $scheduled
    Remove-Item -LiteralPath $bak -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    if ($dc -ge 2) { Stop-Install 17 'doctor 에 FAIL 이 있습니다. 업데이트는 적용되었습니다.' }
    Stop-Install 0 '업데이트 완료.'
}

# ---------------------------------------------------------------------------------------------- 제거
function Invoke-UninstallInstall {
    $info = Read-InstallJson
    if (-not $info) { Stop-Install 21 ('설치 정보(install.json)가 없습니다: ' + $InstallRoot) }
    if ($info.data_root -and -not $script:DataRootGiven) { $script:DataRoot = [string]$info.data_root }
    if ($PurgeData -and -not $Force) {
        if ($NonInteractive) { Stop-Install 1 '-PurgeData 를 -NonInteractive 로 쓰려면 -Force 가 필요합니다.' }
        $a = Read-Host ('데이터와 설정·토큰·로그를 모두 삭제합니다 (' + $InstallRoot + ', ' + $DataRoot + '). 계속하려면 yes 입력')
        if ($a -ne 'yes') { Stop-Install 1 '취소했습니다.' }
    }
    $remaining = New-Object System.Collections.Generic.List[string]
    Write-Step '작업 스케줄러 해제'
    if (-not (Unregister-MatobserverTasks)) { $remaining.Add('작업 스케줄러 작업') }
    Write-Step '프로세스 정지'
    if (Test-Path -LiteralPath $script:MoExe) {
        $r = Invoke-Native -Exe $script:MoExe -Arguments @('stop', '--config', $script:ConfigFile, '--timeout', '15', '--force')
        if ($r.Code -ne 0) { Write-Warn '정지 명령이 0 이 아닌 코드를 돌려줬습니다.' }
    }
    Write-Step 'venv·install.json·환경변수 제거'
    foreach ($p in @($script:Venv, ($script:Venv + '.bak'))) {
        if (Test-Path -LiteralPath $p) {
            try { Remove-Item -LiteralPath $p -Recurse -Force } catch { $remaining.Add($p) }
        }
    }
    try { Remove-Item -LiteralPath $script:InstallJson -Force } catch { $remaining.Add($script:InstallJson) }
    try { [Environment]::SetEnvironmentVariable('MATOBSERVER_CONFIG', $null, 'User') } catch { $remaining.Add('환경변수 MATOBSERVER_CONFIG') }
    if ($PurgeData) {
        if ($script:TranscriptOn) { try { Stop-Transcript | Out-Null } catch { }; $script:TranscriptOn = $false }
        foreach ($p in @($DataRoot, $InstallRoot)) {
            if (Test-Path -LiteralPath $p) {
                try { Remove-Item -LiteralPath $p -Recurse -Force } catch { $remaining.Add($p) }
            }
        }
    } else {
        Write-Host '데이터·설정·토큰·로그는 보존했습니다:'
        Write-Host ('  ' + $DataRoot)
        Write-Host ('  ' + $script:ConfigDir)
        Write-Host ('  ' + $script:SecretsDir)
        Write-Host ('  ' + $script:LogsDir)
    }
    if ($remaining.Count -gt 0) {
        Write-Host '제거하지 못한 항목:'
        foreach ($x in $remaining) { Write-Host ('  ' + $x) }
        Stop-Install 19 '제거 중 일부가 실패했습니다.'
    }
    Stop-Install 0 '제거 완료.'
}

# ---------------------------------------------------------------------------------------------- 진입
if (-not $InstallRoot) {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = [Environment]::GetFolderPath('LocalApplicationData') }
    $InstallRoot = Join-Path $base 'matobserver'
}
$script:DataRootGiven = $PSBoundParameters.ContainsKey('DataRoot')
$script:MnUrlGiven = $PSBoundParameters.ContainsKey('MatNexusUrl')
$script:MnTokGiven = $PSBoundParameters.ContainsKey('MatNexusTokenFile')
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
if (-not $DataRoot) { $DataRoot = Join-Path $InstallRoot 'data' }
$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
Initialize-Paths

try {
    Test-Environment
    try {
        New-Item -ItemType Directory -Force -Path $script:LogsDir | Out-Null
        $logPath = Join-Path $script:LogsDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
        Start-Transcript -Path $logPath | Out-Null
        $script:TranscriptOn = $true
    } catch {
        Write-Warn ('설치 로그를 시작하지 못했습니다: ' + $_.Exception.Message)
    }
    if ($Uninstall) { Invoke-UninstallInstall }
    elseif ($Update) { Invoke-UpdateInstall }
    else { Invoke-FreshInstall }
} catch {
    Stop-Install 1 ('예기치 않은 오류: ' + $_.Exception.Message)
}
