schema_version: 1.3 · 상태: 승인(G1, MINOR 1.3) · 2026-11-09

# 배포·운영 계약 (Windows)

근거: 사용자 결정(2026-11-09) — Windows 배포는 PowerShell 설치 스크립트 방식. plan.md 「해석 실행 전략」(로컬 PC, API·워커 분리, 워커 1개), decisions.md(SQLite JobStore, 사내 배치 스크립트 래핑). 소유는 전부 Impl-Backend([module-boundaries.md §1](module-boundaries.md)).

## 1. 설치 스크립트 `scripts/windows/install.ps1`

### 1.1 대상과 권한

| 항목 | 규칙 |
| --- | --- |
| OS | Windows 10(21H2+) / Windows 11, x64 |
| PowerShell | 5.1 이상(Windows PowerShell 또는 PowerShell 7). 미만이면 종료 코드 10 |
| 실행 | `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [옵션]` (실행 정책은 프로세스 범위만 우회, 시스템 정책 변경 안 함) |
| 관리자 권한 | **기본 불필요**: 설치 루트가 사용자 영역(`%LOCALAPPDATA%`), 작업 스케줄러 작업은 현재 사용자 「로그온 시」 트리거. 관리자 필요한 경우만: `-AtStartup`(부팅 시 실행·사용자 미로그온 실행), 설치 루트가 `Program Files` 등 보호 경로. 필요한데 권한이 없으면 종료 코드 20 |
| 네트워크 | GitHub 릴리스 다운로드 또는 PyPI 의존성 설치 시 필요. 오프라인은 `-Wheel` + `-WheelDir`(의존성 wheel 폴더) |

### 1.2 매개변수

| 매개변수 | 타입 | 기본 | 설명 |
| --- | --- | --- | --- |
| `-InstallRoot` | string | `%LOCALAPPDATA%\matobserver` | 설치 루트 |
| `-DataRoot` | string | `<InstallRoot>\data` | 작업 폴더·레시피 저장소·DB 상위 |
| `-Python` | string | 자동 탐색(`py -3`, `python`) | 사용할 python.exe 경로 |
| `-Tag` | string | 최신 정식 릴리스 | 설치할 릴리스 태그 `vX.Y.Z` (§3) |
| `-Repo` | string | 스크립트에 기록된 릴리스 저장소(`owner/name`) | GitHub 저장소 |
| `-GitHubTokenFile` | string | — | 비공개 저장소 릴리스 다운로드용 토큰 파일(평문을 명령행에 두지 않음) |
| `-Wheel` | string | — | 로컬 wheel 경로(지정 시 다운로드 안 함, `-Tag` 무시) |
| `-WheelDir` | string | — | 오프라인 의존성 wheel 폴더(`pip --no-index --find-links`) |
| `-LsDynaBatchScript` | string | 대화형 질문 | 사내 배치 스크립트 경로 → `solver.lsdyna.script` |
| `-Ncpu` | int | 대화형 질문(기본 4) | → `solver.lsdyna.ncpu` |
| `-MatNexusUrl` | string | 대화형 질문(빈 값 허용) | → `matnexus.base_url` |
| `-MatNexusTokenFile` | string | 대화형 질문(빈 값 허용) | → `matnexus.token: "file:<경로>"` |
| `-Port` | int | 8040 | → `api.port` (호스트는 `127.0.0.1` 고정 기본) |
| `-NonInteractive` | switch | off | 질문 없이 매개변수·기본값만 사용. 필수값(`-LsDynaBatchScript`) 없으면 종료 코드 14 |
| `-NoSchedule` | switch | off | 작업 스케줄러 등록 생략(수동 `matobserver start`) |
| `-AtStartup` | switch | off | 부팅 시 트리거(관리자 필요) |
| `-Update` | switch | off | §1.4 |
| `-AllowMajor` | switch | off | `-Update`에서 MAJOR 변경 허용 |
| `-Uninstall` | switch | off | §1.5 |
| `-PurgeData` | switch | off | `-Uninstall`과 함께만. 데이터 삭제(확인 질문, `-NonInteractive`면 `-Force` 필요) |
| `-Force` | switch | off | 확인 질문 생략 |

### 1.3 설치 단계 (신규)

| # | 단계 | 규칙 | 실패 시 종료 코드 |
| --- | --- | --- | --- |
| 1 | 환경 확인 | PowerShell ≥5.1, OS, 권한 요구 판정 | 10 / 20 |
| 2 | Python 확인 | `-Python` 또는 자동 탐색, `sys.version_info ≥ (3, 11)`, 64비트. **없거나 낮으면 설치 안내(python.org 링크·필요 버전) 출력 후 중단, 자동 설치 안 함** | 11 |
| 3 | 기존 설치 확인 | `<InstallRoot>\install.json`이 있으면 신규 설치 거부(`-Update` 안내) | 21 |
| 4 | venv 생성 | `<InstallRoot>\venv` 전용 venv, pip 최신화 | 12 |
| 5 | 패키지 설치 | 우선순위 `-Wheel` > `-Tag` > 최신 릴리스. 릴리스 자산 `matobserver-X.Y.Z-py3-none-any.whl`과 `SHA256SUMS`를 받아 해시 검증 후 `pip install`. 해시 불일치는 중단 | 13 |
| 6 | 설정 생성 | `<InstallRoot>\config\matobserver.yaml`: 대화형 질문은 4개뿐(배치 스크립트 경로, ncpu, MatNexus URL, MatNexus 토큰 파일). 나머지는 코드 기본값. 서비스·관리자 토큰을 생성해 `<InstallRoot>\secrets\{service,admin}.token`(현재 사용자만 읽기 ACL)에 쓰고 설정에는 SHA-256만(`auth.*`). 경로 키(`paths.*`, `recipes.store_dir`, `jobstore.sqlite_path`, `logging.dir`, `runtime.pid_dir`)는 절대 경로로 기록 | 14 |
| 7 | 폴더 생성 | `<DataRoot>\work`(`paths.work_dir`), `<DataRoot>\recipes`(`recipes.store_dir`), `<DataRoot>\db\jobstore.sqlite`(`jobstore.sqlite_path`, 상위 폴더만 생성, DB는 첫 실행이 만듦 → 이어서 `matobserver db migrate`), `<InstallRoot>\logs`, `<DataRoot>\run`(PID). 쓰기 시험 | 15 |
| 8 | 작업 등록 | `-NoSchedule`가 아니면 §1.6 두 작업 등록 후 시작 | 16 |
| 9 | 점검 | `matobserver doctor --config <설정>` 실행, 표 출력. FAIL 있으면 설치는 유지하고 종료 코드 17 | 17 |
| 10 | 기록 | `<InstallRoot>\install.json`: `{package_version, contract_schema_version, tag, installed_at, install_root, data_root, python, scheduled: bool, at_startup: bool}` | — |

환경변수 `MATOBSERVER_CONFIG`를 사용자 범위에 설정한다(CLI가 기본으로 찾게).

### 1.4 `-Update`

| # | 단계 |
| --- | --- |
| 1 | `install.json` 읽기(없으면 종료 코드 21) |
| 2 | 대상 태그 결정 → 버전 비교(§3.3). 하향이면 중단(코드 22), MAJOR 상향이면 `-AllowMajor` 없을 때 중단(코드 22) |
| 3 | `matobserver stop` (실행 중 잡이 있으면 경고하고 `-Force` 없으면 중단, 코드 23) |
| 4 | `venv`를 `venv.bak`으로 이름 변경 후 새 venv에 설치 |
| 5 | DB 백업 `jobstore.sqlite.bak-<이전버전>` → `matobserver db migrate` (§3.4). 실패 시 DB 백업 복원 + `venv.bak` 복원, 코드 18 |
| 6 | **보존**: 설정 파일, 토큰, 레시피 저장소, DB(마이그레이션만), 작업 폴더·artifact. 설정은 덮어쓰지 않는다. 새 버전에서 추가된 키는 코드 기본값이 적용되며 doctor가 `INFO`로 알린다 |
| 7 | doctor → 작업 재시작 → `install.json` 갱신 → `venv.bak` 삭제 |

### 1.5 `-Uninstall`

작업 스케줄러 작업 해제 → 프로세스 정지 → `venv`·`install.json`·사용자 환경변수 제거. **데이터(`DataRoot`)·설정·토큰·로그는 기본 보존**하고 경로를 출력한다. `-PurgeData`면 확인 후 `InstallRoot`·`DataRoot` 전체 삭제. 일부 실패 시 남은 항목을 출력하고 코드 19.

### 1.6 작업 스케줄러 등록

| 작업 이름 | 명령 | 트리거 | 설정 |
| --- | --- | --- | --- |
| `matobserver-server` | `<venv>\Scripts\matobserver.exe serve --config <설정>` | 현재 사용자 로그온 시(기본) / 부팅 시(`-AtStartup`, SYSTEM이 아닌 설치 사용자 계정, 관리자) | 재시작: 실패 시 1분 간격 최대 3회, 실행 시간 제한 없음, 배터리 조건 해제, 중복 실행 금지(`IgnoreNew`) |
| `matobserver-worker` | `<venv>\Scripts\matobserver.exe worker --config <설정>` | 같음 | 같음 |

작업 폴더(Task path)는 `\matobserver\`. 콘솔 창 없이 실행(`pythonw` 대신 작업 「숨김」 설정 + 로그 파일 출력).

### 1.7 종료 코드

| 코드 | 의미 |
| --- | --- |
| 0 | 성공(doctor FAIL 없음; WARN은 성공) |
| 10 | PowerShell·OS 요구 미충족 |
| 11 | Python 없음, 3.11 미만, 또는 지원 상한(기본 3.13, `MATOBSERVER_PYTHON_MAX`) 초과 |
| 12 | venv 생성 실패 |
| 13 | 다운로드·해시 검증·pip 설치 실패 |
| 14 | 설정 생성 실패, 비대화형 필수값 누락 |
| 15 | 폴더 생성·쓰기 권한 실패 |
| 16 | 작업 스케줄러 등록 실패 |
| 17 | 설치 완료, doctor FAIL 있음 |
| 18 | 업데이트 중 마이그레이션 실패(롤백 완료) |
| 19 | 제거 일부 실패 |
| 20 | 관리자 권한 필요 |
| 21 | 설치 상태 불일치(신규인데 기존 설치 있음 / 업데이트인데 설치 없음) |
| 22 | 버전 규칙 위반(하향, MAJOR 미허용) |
| 23 | 실행 중 잡 때문에 중단 |
| 1 | 기타 오류 |

### 1.8 로그

설치 로그: `<InstallRoot>\logs\install-<yyyyMMdd-HHmmss>.log`(`Start-Transcript`, 토큰 값 기록 금지). 서버·워커 로그: `logging.dir`(기본 `<InstallRoot>\logs`)의 `server.log`, `worker.log`, 크기 회전 `logging.max_bytes`(10 MiB)·`logging.backups`(5).

## 2. CLI 운영 명령

공통: `--config <경로>`(기본 `MATOBSERVER_CONFIG` → `./config/matobserver.yaml`), `--json`(기계용 출력, 표 대신 JSON 하나). 기존 `serve`, `worker`, `recipe ...`(recipe.md §6)는 그대로.

### 2.1 `matobserver start|stop|status`

| 명령 | 옵션 | 동작 |
| --- | --- | --- |
| `start` | `--service server\|worker\|all`(기본 all), `--mode auto\|scheduler\|process`(기본 auto) | `auto`: 작업 스케줄러 작업(`\matobserver\matobserver-*`)이 있으면 실행 요청, 없으면 분리 프로세스로 실행(stdout/stderr → 로그). 이미 실행 중이면 그대로 두고 0 |
| `stop` | `--service`, `--timeout <s>`(기본 30), `--force` | 스케줄러 작업 종료 또는 PID 프로세스 종료(먼저 정상 종료 요청, 시간 초과 시 강제). 워커가 잡 실행 중이면 경고하고 `--force` 없으면 중단(종료 코드 4). 강제 정지된 잡은 다음 워커 기동 시 `failed`(`WORKER_LOST`, api.md §5) |
| `status` | — | 아래 항목 출력 |

PID 파일: `runtime.pid_dir`(기본 `<paths.data_dir>/run`)의 `server.pid`, `worker.pid` — 내용 `{"pid", "started_at", "version", "mode"}`(JSON). 존재하지만 프로세스가 없으면 stale로 보고하고 다음 `start`에서 지운다.

`status` 출력 항목:

| 항목 | 출처 |
| --- | --- |
| `version`, `contract_schema_version` | 패키지 |
| `config_path` | 해석된 설정 경로 |
| `server.url` | `http://{api.host}:{api.port}` |
| `server.state` | `running` \| `stopped` \| `stale_pid` \| `unknown`, `pid`, `mode`(scheduler\|process) |
| `server.health` | `GET /v1/health` 결과(`ok` \| `unreachable` \| HTTP 코드), 응답의 `version`·`schema_version` |
| `worker.state` | `running` \| `stopped` \| `stale_pid`, `pid`, `last_heartbeat_age_s`(JobStore 하트비트, `worker.heartbeat_s` 주기) |
| `queue.queued`, `queue.running_job_id` | JobStore |
| `recipes.active` | active 레시피 수 |

`status` 종료 코드: 0 서버·워커 모두 정상, 3 일부만 정상, 4 모두 정지, 1 설정 오류.

### 2.2 `matobserver doctor`

항목별 상태 `OK` \| `WARN` \| `FAIL` \| `SKIP` \| `INFO`. 사람용 표(ID, 항목, 상태, 상세) + `--json`:

```json
{"version": "0.4.0", "contract_schema_version": "1.3", "config_path": "C:\\...\\matobserver.yaml",
 "summary": {"ok": 10, "warn": 1, "fail": 0, "skip": 1},
 "checks": [{"id": "D03", "name": "lsdyna_version", "status": "WARN", "detail": "버전 문자열 미확인", "data": {}}]}
```

| ID | 항목 | OK | WARN | FAIL |
| --- | --- | --- | --- | --- |
| D01 | 설정 로드·필수 키 | 로드 성공, 필수 키(`paths.work_dir`, `recipes.store_dir`, `jobstore.sqlite_path`, `solver.lsdyna.script`, `auth.service_tokens`, `auth.admin_tokens`) 존재·타입 일치 | 알 수 없는 키, 1.0 별칭 키 사용, 새 버전 추가 키가 기본값(INFO) | 로드 실패, 필수 키 누락 |
| D02 | Python·패키지 | Python ≥3.11, 패키지 import, 버전 일치(`install.json`) | 버전 불일치 | Python 미달 |
| D03 | 배치 스크립트 | `solver.lsdyna.script` 존재·실행 가능(확장자 .bat/.cmd/.ps1/.exe, 읽기 권한) | — | 없음·실행 불가 |
| D04 | LS-DYNA 버전 | **해석 실행 없이** `solver.lsdyna.executable`(선택)의 파일 버전 정보 또는 `solver.lsdyna.version_command`(선택, 예: 배치 스크립트의 버전 출력 모드, 시간 상한 `doctor.command_timeout_s`)에서 `solver.lsdyna.version_pattern` 추출 | 둘 다 미설정 또는 추출 실패(정보 「첫 해석 d3hsp에서 기록」) | — |
| D05 | 폴더 권한·공간 | `paths.work_dir`, `recipes.store_dir`, `jobstore` 폴더, `logging.dir`, `runtime.pid_dir` 쓰기·삭제 시험 성공, 여유 ≥ `doctor.min_free_bytes`(10 GiB) | 여유 부족 | 쓰기 불가 |
| D06 | DB | 열기·`PRAGMA integrity_check` ok, `user_version` = 코드 기대값 | 마이그레이션 필요(`matobserver db migrate` 안내) | 열기 실패, DB가 코드보다 새 버전 |
| D07 | 레지스트리 | `registry.paths` 로드, 모든 enabled 플러그인 import·인터페이스 검사 | disabled 항목 존재 | 로드 실패 |
| D08 | gmsh | `import gmsh` 성공, 버전 | — | import 실패 |
| D09 | MatNexus | `matnexus.base_url` 설정 시 `GET {base_url}{matnexus.probe_path}` 200(토큰 유효) | 응답 지연 > `matnexus.timeout_s`의 절반 | 연결 실패, 401/403 (미설정이면 SKIP) |
| D10 | 포트 | 서버 미실행이면 `api.host:api.port` 바인드 가능, 실행 중이면 그 포트를 matobserver 서버가 점유(헬스 응답) | — | 다른 프로세스가 점유 |
| D11 | 토큰 파일 | `secrets` 토큰 파일 ACL이 현재 사용자 전용 | 다른 사용자 읽기 가능 | — |

`doctor` 종료 코드: 0 FAIL·WARN 없음, 1 WARN 있음(FAIL 없음), 2 FAIL 있음, 3 설정 자체 로드 불가.

### 2.3 `matobserver db migrate`

JobStore 스키마 버전은 SQLite `PRAGMA user_version`(정수). 마이그레이션은 앞으로만, 트랜잭션 단위, 시작 전 백업 파일 생성(`--no-backup`로 생략 가능). DB가 코드보다 새 버전이면 거부(종료 코드 2). 종료 코드 0 성공(변경 없음 포함), 1 실패(롤백).

### 2.4 비밀값 참조

설정 값에 `env:VAR`(1.0) 외에 `file:<경로>`(1.3)를 허용한다: 파일 첫 줄(앞뒤 공백 제거)을 값으로 쓴다. 파일이 없으면 설정 로드 실패(D01 FAIL).

## 3. 버전·릴리스

### 3.1 패키지 버전과 계약 버전

| 항목 | 규칙 |
| --- | --- |
| 패키지 버전 | SemVer `X.Y.Z` (`matobserver.__version__`, `pyproject.toml`) |
| 계약 버전 | `matobserver.CONTRACT_SCHEMA_VERSION = "1.3"` (이 문서 묶음의 schema_version). `/v1/health`의 `schema_version`, `matobserver --version`, 결과 `schema_version`이 이 값 |
| 관계 | 계약 MINOR 상승 → 패키지 MINOR 이상 상승. 계약 MAJOR 상승 → 패키지 MAJOR 상승. 패키지 PATCH는 계약을 바꾸지 않는다. 0.x 동안에도 이 규칙을 따른다 |
| 표기 | 릴리스 노트 머리에 「contract schema_version 1.3」 명시 |

### 3.2 릴리스 태그

- 정식: `vX.Y.Z`. 사전 릴리스: `vX.Y.Z-rc.N`(설치 스크립트 「최신」 선택에서 제외, `-Tag`로만).
- 릴리스 자산: `matobserver-X.Y.Z-py3-none-any.whl`, `install.ps1`, `SHA256SUMS`(두 파일 해시). 태그는 불변(재태깅 금지).

### 3.3 업데이트 호환 규칙

| 변경 | 허용 | 처리 |
| --- | --- | --- |
| PATCH·MINOR 상향 | 예 | 설정·토큰·레시피·DB·작업 폴더 보존, DB 전진 마이그레이션 |
| MAJOR 상향 | `-AllowMajor`일 때만 | 같음 + 릴리스 노트의 수동 조치 확인 안내 |
| 하향(다운그레이드) | 아니오 | 중단(코드 22). DB `user_version`이 높으면 이전 코드가 못 연다(D06 FAIL) |

- 저장된 결과는 생성 당시 `schema_version` 그대로 반환한다(api.md §9). 레시피는 불변이므로 업데이트 후에도 같은 `content_hash`.
- 설정 키는 MINOR에서 추가만 한다. 이름이 바뀌면 이전 키를 별칭으로 계속 읽는다(예: 1.1 `engine.stages.*`).

## 4. 완료 기준 테스트 (Impl-Backend)

[module-boundaries.md §5.4](module-boundaries.md) B-15~B-20.

## 5. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| D-1 | 기본 트리거는 사용자 로그온 시(관리자 불필요). 부팅 시 실행은 옵션 | 가정 |
| D-2 | 서비스·관리자 토큰은 설치 시 생성해 파일로 보관, 설정에는 해시만 | 가정 |
| D-3 | 기본 포트 8040, 바인드 `127.0.0.1`(MatNexus가 다른 호스트면 설정 변경 필요) | 가정 |
| D-4 | LS-DYNA 버전은 실행 없이 확인 가능한 경우만 doctor에서 추출, 아니면 첫 해석 d3hsp 기록에 맡김 | 가정 |
| U-1 | 릴리스 저장소(`owner/name`)와 공개 여부, 사내 프록시 하에서 GitHub 접근 가능 여부 | 미결 |
| U-2 | 사내 PC 실행 정책·백신이 작업 스케줄러·venv 실행을 막는지(배포 파일럿에서 확인) | 미결 |

## 변경 이력

| 버전 | 날짜 | 변경 |
| --- | --- | --- |
| 1.3 | 2026-11-09 | 신규: Windows PowerShell 설치 스크립트, 운영 CLI(start/stop/status/doctor/db migrate), `file:` 비밀값 참조, 버전·릴리스 규칙 |
