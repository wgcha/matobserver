schema_version: 1.0 · 상태: 승인(G1) · 2026-11-08

# API 계약 (REST)

matobserver의 외부 계약. 소비자는 MatNexus(서비스 토큰)와 관리자(관리자 토큰·CLI)다. 근거: `docs/plan.md` 「API 계약 초안」, 「레시피 구조」, 「시험 데이터 입력」, `docs/decisions.md`.

관련 계약: 재료·플러그인 이름은 [plugins.md](plugins.md), 응답 정의·정렬은 [testspec.md](testspec.md), 레시피 매니페스트는 [recipe.md](recipe.md), 결과 본문은 [result.md](result.md), 구현 소유는 [module-boundaries.md](module-boundaries.md).

## 1. 공통 규칙

| 항목 | 규칙 |
| --- | --- |
| 기본 경로 | `/v1` (메이저 버전). 호스트·포트는 설정 `api.host`, `api.port` |
| 형식 | 요청·응답 `application/json; charset=utf-8`. 파일 업로드만 `multipart/form-data`, 다운로드는 파일 스트림 |
| 단위 | **API 경계의 모든 수치는 SI**(m, N, Pa, s, kg/m³, 무차원 1). MatNexus 어휘와 같다. 해석 단위계(mm·N·s·tonne, MPa) 변환은 내부에서 한다([module-boundaries.md §3](module-boundaries.md)) |
| 시각 | ISO 8601 UTC(`2026-11-08T03:12:00Z`) |
| 필드 이름 | snake_case. 응답에 모르는 필드가 있으면 클라이언트는 무시해야 한다 |
| 요청 필드 | 엄격: 정의되지 않은 필드는 400 `SCHEMA_INVALID` |
| `schema_version` | 요청 본문(JSON)에 필수, 모든 응답 본문에 포함. 형식 `"MAJOR.MINOR"` (현재 `"1.0"`) |
| 페이지 | 목록은 `limit`(기본 `api.page_default_limit`=50, 최대 `api.page_max_limit`=500), `offset`(0부터). 응답 `{items, total, limit, offset}` (MatNexus `Page` 형과 같은 모양) |
| 경로 비노출 | 어떤 응답에도 서버 파일 경로를 넣지 않는다. 파일은 `file_id`(입력)·`artifact_id`(출력)로만 다룬다 |

### 1.1 식별자 규칙

| 대상 | 형식 | 예 | 비고 |
| --- | --- | --- | --- |
| 잡 | `cal_` + ULID(26자, Crockford base32 대문자) | `cal_01JC8Z6Q4M2V7R3T9X0A1B2C3D` | 생성 시각 정렬 가능 |
| 입력 파일 | `fil_` + ULID | `fil_01JC...` | `POST /v1/files` |
| 산출 파일 | `art_` + ULID | `art_01JC...` | §6 |
| 레시피 검증 | `ver_` + ULID | `ver_01JC...` | 레시피 등록 시 |
| 레시피 참조 | `recipe_id@version` | `flex3p-d790-utm01@3` | `recipe_id`: `^[a-z0-9][a-z0-9-]{2,63}$`, `version`: 양의 정수. [recipe.md §5](recipe.md) |
| MatNexus 시험 | UUID 문자열 | `test_run_ids` 원소 | MatNexus `TestRun.id` 그대로 |

ID는 불투명 문자열이다. 클라이언트는 접두사 외 구조를 해석하지 않는다.

## 2. 인증

plan.md 범위상 사용자 인증은 MatNexus 담당이다. matobserver는 **호출 주체의 권한만** 구분한다(결정 2026-11-08: 주체 구분 없이 권한으로만 구분).

| 토큰 | 헤더 | 허용 범위 | 설정 키 |
| --- | --- | --- | --- |
| 서비스 토큰 | `Authorization: Bearer <token>` | §3의 모든 비관리 엔드포인트 | `auth.service_tokens[]` (`{name, sha256}`; 평문 저장 금지) |
| 관리자 토큰 | 같음 | 서비스 범위 전체 + `/v1/admin/*` | `auth.admin_tokens[]` (같은 형식) |
| 없음 | — | `GET /v1/health` 만 | — |

- 토큰 비교는 SHA-256 해시 상수 시간 비교. 실패 시 401 `AUTH_REQUIRED`(헤더 없음) / 401 `AUTH_INVALID`(불일치), 권한 부족 403 `FORBIDDEN`.
- 잡에는 생성한 토큰의 `name`을 `created_by`로 기록한다(추적용, 접근 제어에는 쓰지 않음).
- matobserver → MatNexus 호출은 MatNexus 서비스 계정 PAT를 쓴다: `matnexus.base_url`, `matnexus.token`(환경변수 참조 `env:MATNEXUS_TOKEN` 형식 허용), `matnexus.timeout_s`.

## 3. 엔드포인트 목록

| # | 메서드 | 경로 | 권한 | 용도 | 성공 응답 |
| --- | --- | --- | --- | --- | --- |
| E1 | GET | `/v1/health` | 없음 | 생존·버전 | 200 `{status, version, schema_version}` |
| E2 | POST | `/v1/files` | 서비스 | STP 등 입력 파일 업로드 | 201 `FileOut` |
| E3 | POST | `/v1/calibrations` | 서비스 | 잡 생성 | 201 `JobCreated` |
| E4 | GET | `/v1/calibrations` | 서비스 | 잡 목록(필터·페이지) | 200 `Page[JobSummary]` |
| E5 | GET | `/v1/calibrations/{job_id}` | 서비스 | 상태·진행률 | 200 `JobStatus` |
| E6 | GET | `/v1/calibrations/{job_id}/result` | 서비스 | 최종 결과 | 200 `Result` ([result.md](result.md)) |
| E7 | GET | `/v1/calibrations/{job_id}/history` | 서비스 | 반복 이력(페이지) | 200 `Page[HistoryItem]` |
| E8 | POST | `/v1/calibrations/{job_id}/cancel` | 서비스 | 중단 | 202 `JobStatus` |
| E9 | GET | `/v1/artifacts/{artifact_id}` | 서비스 | 파일 다운로드 | 200 스트림 |
| E10 | GET | `/v1/recipes` | 서비스 | 사용 가능(`active`) 레시피 목록 | 200 `Page[RecipeSummary]` |
| E11 | GET | `/v1/recipes/{recipe_id}/versions/{version}` | 서비스 | 레시피 공개 정보(파라미터·슬롯·응답) | 200 `RecipePublic` |
| E12 | GET | `/v1/registry` | 서비스 | 등록 플러그인 목록·버전·capabilities | 200 `Registry` |
| A1 | POST | `/v1/admin/recipes` | 관리자 | 레시피 등록(번들 업로드 → 검증 시작) | 202 `RecipeRegistered` (같은 해시 재등록은 200) |
| A2 | GET | `/v1/admin/recipes` | 관리자 | 전체 상태 레시피 목록 | 200 `Page[RecipeAdminSummary]` |
| A3 | GET | `/v1/admin/recipes/{recipe_id}/versions/{version}` | 관리자 | 상세(매니페스트·검증 보고서) | 200 `RecipeAdminDetail` |
| A4 | POST | `/v1/admin/recipes/{recipe_id}/versions/{version}/retire` | 관리자 | 폐기 | 200 `RecipeAdminSummary` |

E1–E9는 plan.md API 표 그대로, E10–E12와 A1–A4는 레시피·레지스트리 조회를 위해 추가했다(§10 가정 A-1).

## 4. 잡 API

### 4.1 E2 `POST /v1/files`

multipart 필드:

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `file` | 파일 | 예 | 크기 ≤ `files.max_bytes`(기본 200 MiB). 초과 413 `FILE_TOO_LARGE` |
| `purpose` | string enum | 예 | `geometry_stp` (1차 유일값, 확장 가능 enum) |

업로드 시 검사: 확장자 `.stp`/`.step`, 첫 줄 `ISO-10303-21;`. 실패 400 `FILE_TYPE_UNSUPPORTED`. 형상 읽기 가능 여부(전체 임포트)는 잡 생성 시 검사한다(`STP_UNREADABLE`).

`FileOut`:

```json
{"schema_version": "1.0", "file_id": "fil_01JC...", "purpose": "geometry_stp",
 "filename": "pkg.stp", "bytes": 182733, "sha256": "e3b0...", "created_at": "2026-11-08T03:12:00Z"}
```

### 4.2 E3 `POST /v1/calibrations` — 요청 `CalibrationRequest`

헤더 `Idempotency-Key`(선택, ≤128자): 같은 키+같은 본문 해시는 기존 `job_id`를 200으로 돌려준다. 같은 키+다른 본문은 409 `IDEMPOTENCY_KEY_REUSED`. 키 보존 `api.idempotency_ttl_s`(기본 86400).

최상위 필드:

| 필드 | 타입 | 필수 | 단위 | 설명 |
| --- | --- | --- | --- | --- |
| `schema_version` | string | 예 | — | `"1.0"` |
| `name` | string ≤200 | 아니오 | — | 표시용 이름 |
| `material_ref` | object | 아니오 | — | `{matnexus_material_id: uuid}`. 목록 필터용. 없으면 첫 MatNexus run의 `material_id` |
| `tests` | `TestInput[]` (1..`api.max_tests_per_job`=8) | 예* | — | 시험 목록. 다중 시험 동시 피팅 단위 |
| `test_type`, `recipe`, `geometry`, `test_run_ids`, `inline_runs`, `displacement_channel`, `recipe_params` | — | * | — | **단일 시험 약식**: `tests`를 생략하고 최상위에 `TestInput` 필드를 두면 `tests=[{test_key:"t1", ...}]`와 같다. `tests`와 동시 사용 시 400 `SCHEMA_INVALID` |
| `materials` | `MaterialsInput` | 예 | — | 재료 슬롯(§4.3) |
| `plasticity_mode` | `"coeff"` \| `"table"` | 아니오 | — | 슬롯 `material_model`이 별칭 `mat024`일 때의 해석 기본값. 생략 시 설정 `engine.default_plasticity_mode` |
| `optimizer` | string \| `{name, options}` | 아니오 | — | 탄소성 단계 최적화기 레지스트리 이름. 기본 `engine.plastic_stage.optimizer`(=`bo`) |
| `elastic_optimizer` | string \| `{name, options}` | 아니오 | — | 탄성 단계 최적화기. 기본 `engine.elastic_stage.optimizer`(=`lsq`) |
| `solver` | string \| `{name, options}` | 아니오 | — | 솔버 어댑터 이름. 기본 `engine.default_solver`(=`lsdyna`) |
| `friction` | `FrictionInput` | 아니오 | — | 지그–시편 마찰. 생략 시 레시피 파라미터 `fric` 기본값 |
| `budget` | `BudgetInput` | 아니오 | — | 해석 예산 |
| `seed` | int ≥0 | 아니오 | — | 난수 시드. 생략 시 `engine.default_seed`. 재현성 근거 |
| `explicit_check` | `ExplicitCheckInput` | 아니오 | — | 식별 후 explicit 재현 검사(1차 옵션, §4.6.1). 생략 시 설정 `verify.explicit_check.enabled`(기본 false) |

`TestInput`:

| 필드 | 타입 | 필수 | 단위 | 설명 |
| --- | --- | --- | --- | --- |
| `test_key` | string `^[a-z0-9_]{1,16}$` | 예(약식은 자동 `t1`) | — | 잡 내 고유 |
| `test_type` | string | 예 | — | TestSpec 레지스트리 키. 1차 `bending`. 2차 `compression` (확장 가능 enum) |
| `recipe` | string | 예 | — | `recipe_id@version`. 상태 `active`여야 함 |
| `geometry` | `GeometryInput` | 예 | — | §4.4 |
| `test_run_ids` | uuid[] | † | — | MatNexus TestRun ID. SW가 직접 가져옴 |
| `inline_runs` | `InlineRun[]` | † | — | MatNexus 밖 데이터(오프라인·테스트). §4.5 |
| `displacement_channel` | `"auto"` \| `"displacement"` \| `"deflection"` | 아니오 | — | 기본 `auto`. 규칙은 [testspec.md §6](testspec.md) |
| `recipe_params` | object `{name: number}` | 아니오 | 매니페스트 선언 SI 단위 | 레시피 노출 파라미터 덮어쓰기. 우선순위: 이 값 > 시편 치수(MatNexus 키 매핑) > 레시피 기본값 |
| `user_window` | `{x_min, x_max}` | 아니오 | 기준축 단위(m 또는 s) | 사용자 절단점([testspec.md §5](testspec.md) 4단계) |
| `weight` | number >0 | 아니오 | 1 | 다중 시험 결합 가중 `W_t` (기본 1.0) |

† `test_run_ids`와 `inline_runs` 중 정확히 하나, 원소 1..`api.max_runs_per_test`(=20).

### 4.3 `MaterialsInput` — 재료 슬롯

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `mode` | `"A"` \| `"B"` | 예 | A 단일 등가, B 요소별 다중 물성 |
| `slots` | `SlotInput[]` (1..`api.max_slots`=8) | 예 | 각 `slot` 이름은 **모든 시험 레시피의 매니페스트 슬롯에 존재**해야 함 |

`SlotInput`:

| 필드 | 타입 | 필수 | 단위 | 설명 |
| --- | --- | --- | --- | --- |
| `slot` | string `^[a-z][a-z0-9_]{0,15}$` | 예 | — | 매니페스트 `material_slots[].slot` |
| `role` | `"identify"` \| `"fixed"` | 예 | — | 식별 대상 / 고정 카드 |
| `material_model` | string | 예 | — | 레지스트리 이름: `mat001`, `mat024_coeff`, `mat024_table`, 별칭 `mat024`(→ `plasticity_mode`로 해석) |
| `options` | object | 아니오 | — | 모델별 옵션([plugins.md §4.3](plugins.md)). 예: `{hardening_law: "swift"}`, `{table_nodes: 8, max_strain: 0.2}` |
| `reference` | `CardBlocks` | fixed는 예, identify는 아니오 | SI | 기준 카드. [result.md §3](result.md)의 `card_blocks`와 같은 형식(`elastic`, `table`). `elastic.poisson_ratio`·`density`는 **입력값으로 기록**되며 식별하지 않음 |
| `params` | object `{param_name: ParamOverride}` | 아니오 | SI | 모델 `param_space()` 기본 범위 덮어쓰기 |
| `member_position` | int ≥0 | 아니오 | — | MatNexus ADR 0026 `member_position` 대응(모드 B 결과 반입용) |

`ParamOverride`: `{lower?: number, upper?: number, initial?: number, fixed_value?: number}`. `fixed_value`가 있으면 그 변수는 θ에서 빠진다. 이름은 슬롯 내부 이름(예: `E`, `sigma_y`, `K`, `n`). θ 전역 이름은 `"{slot}.{param}"`, 비재료(시험별) 변수는 `"_test.{test_key}.{param}"`([plugins.md §3](plugins.md) R1·R9). 1차 굽힘은 비재료 변수가 없다.

1차 제약(위반 시 400):

| 조건 | 코드 |
| --- | --- |
| `mode=A`인데 슬롯이 2개 이상이거나 형상 영역이 둘 이상의 슬롯에 매핑 | `MODE_A_MULTI_SLOT` |
| `mode=B`에서 `identify` 슬롯이 plastic 단계 변수를 가짐(fixed_value로 모두 고정하지 않음) | `MODE_B_PLASTIC_UNSUPPORTED` (2차에서 해제) |
| `identify` 슬롯 0개 | `SLOT_NO_IDENTIFY` |

`ν` 미입력 시 400 `POISSON_RATIO_REQUIRED`(identify 슬롯도 `reference.elastic.poisson_ratio` 필수). `density` 미입력 시 설정 `material.default_density_kg_m3`(기본 1000)를 쓰고 경고 `DENSITY_DEFAULTED`.

### 4.4 `GeometryInput`

| 필드 | 타입 | 필수 | 단위 | 적용 kind | 설명 |
| --- | --- | --- | --- | --- | --- |
| `kind` | enum | 예 | — | 전부 | 1차 `box`, `layered_box`, `stp`. 2차 `cylinder`, `frustum` 예약 (확장 가능 enum) |
| `dimensions` | object | 아니오 | m | box, layered_box | MatNexus 시편 치수 키 덮어쓰기. 굽힘: `width`, `thickness`, `total_length`. 생략 키는 MatNexus 시편 치수(measured 우선, 없으면 nominal)에서 |
| `layers` | `[{slot, thickness}]` | layered_box 예 | m | layered_box | 아래→위 적층. 두께 합은 `thickness`와 상대오차 ≤ `geometry.layer_sum_rel_tol`(1e-3) |
| `stack_axis` | `"z"` \| `"y"` | 아니오 | — | layered_box | 적층 방향. `z`=하중축(기본), `y`=폭 방향(측면 하중). 방향이 다른 시험 동시 피팅용 |
| `file_id` | string | stp 예 | — | stp | `POST /v1/files` 결과 |
| `placement` | object | 아니오 | m, deg | stp | `{mode: "auto"\|"explicit", translate: [x,y,z], rotate_deg: [rx,ry,rz]}`. 규약은 [recipe.md §4.3](recipe.md) |
| `body_slots` | `[{body: int\|string, slot}]` | stp 다중 바디 예 | — | stp | 바디 인덱스(0부터) 또는 STEP 바디 이름 → 슬롯 |
| `diameter`, `top_diameter`, `bottom_diameter`, `height` | number | — | m | cylinder, frustum | 2차 예약 |

### 4.5 `InlineRun` (MatNexus 어휘 JSON)

```json
{
  "run_key": "spec01",
  "test_type_key": "flexural",
  "channels": {
    "force":        {"unit": "N", "values": [0.0, 1.2, ...]},
    "displacement": {"unit": "m", "values": [0.0, 1.0e-5, ...]},
    "deflection":   {"unit": "m", "values": [...]},
    "time":         {"unit": "s", "values": [...]}
  },
  "specimen_dimensions": {"width": 0.010, "thickness": 0.004, "span": 0.064},
  "conditions": {"temperature": 296.15, "test_speed": 3.3e-5},
  "instrument": "UTM-01",
  "specimen_ref": "S-01"
}
```

- 채널 키·단위는 MatNexus `BUILTIN_TEST_TYPES`의 채널 정의와 같다. 단위는 SI 정본만 허용(`N`, `m`, `s`, `1`). 다르면 400 `CHANNEL_UNIT_INVALID`. 모든 채널 길이 동일, ≥ `alignment.min_raw_points`(20).
- 치수 키는 MatNexus 시편 분류 「굽힘」 칸(`width`, `thickness`, `span`, `load_span`, `total_length`).

### 4.6 `FrictionInput`, `BudgetInput`

| 객체 | 필드 | 타입 | 단위 | 설명 |
| --- | --- | --- | --- | --- |
| Friction | `mode` | `"fixed"` \| `"identify"` | — | 1차(굽힘) `identify`는 400 `FRICTION_IDENTIFY_UNSUPPORTED`. 2차 압축에서 기본 `identify` |
| | `value` | number ≥0 | 1 | fixed일 때 |
| | `lower`, `upper` | number | 1 | identify일 때 |
| Budget | `max_evaluations` | int ≥1 | 회 | 해석 총 횟수 상한. 기본 `budget.default_max_evaluations`, 상한 `budget.max_evaluations_limit` 초과 400 `BUDGET_EXCEEDS_LIMIT` |
| | `max_wall_time_s` | number | s | 잡 전체 시간. 기본 `budget.default_max_wall_time_s` |
| | `run_timeout_s` | number | s | 해석 1회 상한. 기본 `solver.run_timeout_s` |

### 4.6.1 `ExplicitCheckInput` — explicit 재현 검사(1차 옵션)

식별 완료 후 최종 카드를 제품 낙하 모델과 같은 요소 정식화·explicit 해석으로 같은 시험·레시피에 1회 재실행해 F-δ 차이를 결과에 기록한다. 판정은 **경고만**(잡 거절·실패 없음). 절차·지표는 [testspec.md §8.1](testspec.md), 결과는 [result.md §4.12](result.md).

| 필드 | 타입 | 필수 | 단위 | 설명 |
| --- | --- | --- | --- | --- |
| `enabled` | bool | 예 | — | |
| `tests` | string[] | 아니오 | — | 대상 `test_key`. 생략 시 전체 |
| `section` | object | 아니오 | — | 슬롯 공통 덮어쓰기 `{elform: int, hourglass: {ihq: int, qm: number}}`. 슬롯별은 `slots: {slot: {...}}`. 생략 시 설정 `verify.explicit_check.elform`·`verify.explicit_check.hourglass.*` |
| `loading_velocity` | number >0 | 아니오 | m/s | 준정적 압자 속도. 생략 시 `verify.explicit_check.loading_velocity_m_s` |
| `max_ke_ratio` | number | 아니오 | 1 | 운동에너지/내부에너지 비 상한. 생략 시 `verify.explicit_check.max_ke_ratio` |

요청에서 `enabled=true`인데 레시피에 explicit 제어 파일이 없거나 레시피 검증에서 질량 스케일링(`WARN_MASS_SCALING`)이 기록됐으면 400 `EXPLICIT_CHECK_UNAVAILABLE`; elform이 시편 topology와 맞지 않으면 400 `EXPLICIT_CHECK_SECTION_INVALID`. 설정 기본값으로 켜진 경우에는 같은 조건에서 검사를 건너뛰고 경고 `EXPLICIT_CHECK_SKIPPED`.

### 4.7 E3 응답과 검증

성공 201 `JobCreated`:

```json
{"schema_version": "1.0", "job_id": "cal_01JC...", "status": "queued",
 "created_at": "2026-11-08T03:12:00Z", "warnings": [{"code": "COMPLIANCE_MISSING", "message": "...", "test_key": "t1"}]}
```

잡 생성 전에 **동기 검증**한다(plan: 입력 검증 실패는 잡 생성 전 400). MatNexus 조회도 이 단계에서 수행하고 받은 데이터를 잡 입력 스냅샷으로 저장한다(재현성). MatNexus에 닿지 못하면 502 `UPSTREAM_UNAVAILABLE`.

검증 순서: ① 스키마 ② 레지스트리 이름 ③ 레시피 상태·시험 종류 ④ 슬롯 대응 ⑤ 시험 데이터 조회·매핑(TestSpec) ⑥ 곡선 품질 ⑦ 형상(STP 임포트 포함) ⑧ 파라미터 범위 ⑨ 조합 호환(재료–솔버–시험) ⑩ 식별성 사전 점검. 오류는 한꺼번에 모아 `details[]`로 반환한다(⑤ 실패 시 ⑥ 이후는 건너뜀).

### 4.8 E4 `GET /v1/calibrations`

쿼리: `status`(복수 허용, 쉼표), `created_from`, `created_to`(ISO 8601), `material_id`(uuid, `material_ref`), `test_type`, `recipe_id`, `limit`, `offset`. 정렬 `created_at` 내림차순 고정.

`JobSummary`:

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `job_id` | string | |
| `name` | string \| null | |
| `status` | enum | §5 |
| `phase` | enum \| null | §5 |
| `test_types` | string[] | |
| `recipes` | string[] | `recipe_id@version` |
| `material_id` | uuid \| null | |
| `best_J` | number \| null | |
| `created_at`, `finished_at` | string \| null | |

### 4.9 E5 `GET /v1/calibrations/{job_id}` — `JobStatus`

```json
{
  "schema_version": "1.0",
  "job_id": "cal_01JC...",
  "status": "running",
  "phase": "plastic",
  "iter": 37,
  "best_J": 0.0412,
  "progress": {"stage": "plastic", "stage_iter": 21, "max_evaluations": 120,
               "evaluations_failed": 1, "best_J_stage": 0.0412, "elapsed_s": 5130.2},
  "cancel_requested": false,
  "created_at": "...", "started_at": "...", "finished_at": null,
  "created_by": "matnexus-prod",
  "warnings": [],
  "error": null
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `iter` | int | 완료된 해석 평가 수(전 단계 누적, plan의 `iter`) |
| `best_J` | number \| null | 현재 단계까지 최소 목적함수([testspec.md §8](testspec.md)) |
| `progress.stage` | `"preprocess"` \| `"elastic"` \| `"plastic"` \| `"diagnostics"` \| `"finalize"` \| `"explicit_check"` | |
| `error` | `{code, message}` \| null | `failed`일 때만. 코드는 §7.3 |

### 4.10 E6 `GET /v1/calibrations/{job_id}/result`

`succeeded`일 때만 200 `Result`([result.md](result.md) 전체 스키마). 그 외 상태는 409 `JOB_NOT_SUCCEEDED`(`details[0].status`에 현재 상태).

### 4.11 E7 `GET /v1/calibrations/{job_id}/history` — `HistoryItem`

쿼리 `stage`, `limit`, `offset`. 모든 상태에서 조회 가능. 정렬 `eval_index` 오름차순.

| 필드 | 타입 | 단위 | 설명 |
| --- | --- | --- | --- |
| `eval_index` | int | — | 0부터, 잡 내 고유 |
| `stage` | `"elastic"` \| `"plastic"` \| `"diagnostics"` \| `"roundtrip"` \| `"explicit_check"` | — | explicit_check 평가는 예산 밖 |
| `theta` | object `{"{slot}.{param}" \| "_test.{test_key}.{param}": number}` | SI | 이 평가의 전체 θ(고정값 제외). 재료·비재료 이름 공간은 [plugins.md §3](plugins.md) R1·R9 |
| `J` | number \| null | 1 | 결합 목적함수. 실패 시 페널티 값 |
| `J_components` | object | 1 | `{test_key: {response_key: {elastic, plastic, shortfall, scalar}}}` |
| `status` | `"ok"` \| `"penalized"` | — | penalized=해석 실패·시간 초과·범위 부족 페널티 적용 |
| `run_status` | object | — | `{test_key/case_id: "normal"\|"error"\|"timeout"\|"diverged"\|"license_timeout"}` |
| `mode_used` | object | — | `{test_key/case_id: "implicit"\|"explicit"}` |
| `wall_time_s` | number | s | |
| `is_best` | bool | — | 단계 최적 갱신 여부 |
| `started_at` | string | — | |

### 4.12 E8 `POST /v1/calibrations/{job_id}/cancel`

본문 없음. `queued`면 즉시 `cancelled`. `running`이면 `cancel_requested=true`로 202를 돌려주고, 워커가 `worker.cancel_poll_s`(기본 5) 안에 실행 중 해석을 종료한 뒤 `cancelled`로 바꾼다. 종결 상태면 409 `JOB_ALREADY_FINISHED`.

## 5. 잡 상태 머신

```
            ┌───────────── cancel ──────────────┐
            │                                    ▼
 [생성] → queued ──(워커 획득)──→ running ──→ succeeded
                                   │   │
                                   │   └──(cancel 요청 → 해석 종료)──→ cancelled
                                   └──(복구 불가 오류)──→ failed
```

| 상태 | 종결 | 진입 조건 |
| --- | --- | --- |
| `queued` | 아니오 | 검증 통과 후 JobStore 등록 |
| `running` | 아니오 | 워커가 잡을 원자적으로 획득(SQLite 트랜잭션). 워커 1개·잡 1건 동시 실행 |
| `succeeded` | 예 | 결과 확정·결과 artifact 기록 완료 |
| `failed` | 예 | §7.3 실행 오류. 해석 개별 실패는 페널티로 처리하고 잡을 실패시키지 않는다 |
| `cancelled` | 예 | 취소 완료 |

- `phase`(running 하위 표시, 상태 아님): `preprocess` → `elastic` → `plastic`(탄성 전용 잡은 생략) → `diagnostics` → `finalize`(결과 테이블 확정·왕복 검사) → `explicit_check`(옵션, 왕복 검사 이후) → 결과 저장. 상태 enum은 plan 그대로 5개로 고정하고 세부는 `phase`로만 늘린다.
- 워커 재시작 시 `running`으로 남은 잡은 `failed`(`WORKER_LOST`)로 바꾼다. 재개(restore)는 1차 범위 밖이며 최적화기 `state()/restore()` 계약으로 확장 지점만 둔다.

## 6. artifact 규칙

| 규칙 | 내용 |
| --- | --- |
| 저장 | 소유자별 작업 폴더에만: 잡 `paths.work_dir/{job_id}/artifacts/`, 레시피 검증 `paths.work_dir/_recipes/{verification_id}/artifacts/`. JobStore에는 `artifact_id → (owner_kind: "job"\|"recipe_verification", owner_id: job_id\|verification_id, 상대 경로, sha256, bytes, media_type, kind)` |
| 노출 | `GET /v1/artifacts/{artifact_id}`만. 응답 헤더 `Content-Type`, `Content-Disposition: attachment; filename="..."`, `ETag: "sha256:<hex>"`, `Content-Length` |
| 불변 | 한 번 발급된 artifact 내용은 바뀌지 않는다. 재생성은 새 ID |
| 보존 | `artifacts.retention_days`(기본 null=무기한). 만료 후 410 `ARTIFACT_EXPIRED` |
| 안전 | 다운로드 시 해석된 실제 경로가 해당 소유자의 artifact 루트(위 두 경로 중 하나) 안인지 검사. 밖이면 404 |
| 종류 | `kind` enum은 [result.md §6](result.md) |

## 7. 오류

### 7.1 오류 본문

```json
{
  "schema_version": "1.0",
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "요청 검증 실패 3건",
    "details": [
      {"code": "CHANNEL_MISSING", "field": "tests[0].test_run_ids[1]", "message": "force 채널 없음",
       "test_key": "t1", "run_id": "6f1c..."}
    ]
  }
}
```

`details[]` 원소: `code`(필수), `message`(필수, 한국어), `field`(JSON 경로, 선택), `test_key`/`run_id`/`slot`/`param`(해당 시).

### 7.2 HTTP 상태와 최상위 코드

| HTTP | `error.code` | 상황 |
| --- | --- | --- |
| 400 | `VALIDATION_FAILED` | 요청 검증 실패. 사유는 `details[].code`(§7.4) |
| 401 | `AUTH_REQUIRED` / `AUTH_INVALID` | |
| 403 | `FORBIDDEN` | 서비스 토큰으로 `/v1/admin/*` |
| 404 | `NOT_FOUND` | 잡·artifact·레시피·파일 없음 |
| 409 | `JOB_NOT_SUCCEEDED`, `JOB_ALREADY_FINISHED`, `IDEMPOTENCY_KEY_REUSED`, `RECIPE_VERSION_CONFLICT`, `RECIPE_NOT_RETIRABLE` | 상태 충돌 |
| 410 | `ARTIFACT_EXPIRED` | |
| 413 | `FILE_TOO_LARGE` | |
| 502 | `UPSTREAM_UNAVAILABLE` | MatNexus 조회 실패(네트워크·5xx·인증) |
| 500 | `INTERNAL_ERROR` | |

### 7.3 잡 실행 오류(`JobStatus.error.code`)

| 코드 | 의미 |
| --- | --- |
| `MESH_FAILED` | 시편·지그 메시 생성 실패(사전 검증 통과 후) |
| `SOLVER_UNAVAILABLE` | 실행 파일·배치 스크립트 호출 불가 |
| `LICENSE_TIMEOUT` | 라이선스 대기 `solver.license_retry.max_wait_s` 초과 |
| `ALL_INITIAL_RUNS_FAILED` | 초기 설계점 해석이 전부 실패(설정 `engine.abort_if_initial_all_failed`) |
| `BUDGET_EXHAUSTED_NO_VALID` | 예산 소진까지 유효 평가 0건 |
| `WALL_TIME_EXCEEDED_NO_VALID` | 시간 초과까지 유효 평가 0건 (유효 평가가 있으면 그 최적점으로 `succeeded` + 경고) |
| `WORKER_LOST` | 워커 비정상 종료 |
| `INTERNAL_ERROR` | 기타 |

### 7.4 400 검증 사유 코드(`details[].code`)

| 분류 | 코드 | 조건 |
| --- | --- | --- |
| 스키마 | `SCHEMA_INVALID` | 필수 누락·타입·미정의 필드·약식과 `tests` 동시 사용 |
| | `SCHEMA_VERSION_UNSUPPORTED` | MAJOR≠1, 또는 MINOR가 서버보다 큼 |
| 레지스트리 | `TEST_TYPE_UNKNOWN` | TestSpec 미등록 |
| | `OPTIMIZER_UNKNOWN`, `SOLVER_UNKNOWN`, `MATERIAL_MODEL_UNKNOWN` | 레지스트리 미등록·비활성 |
| 시험 종류 | `TEST_TYPE_UNMAPPED` | MatNexus `test_type_key`가 어떤 TestSpec에도 매핑 안 됨 |
| | `RUN_TEST_TYPE_MISMATCH` | run의 `test_type_key`가 요청 `test_type`의 매핑과 다름 |
| | `TEST_VARIANT_UNSUPPORTED` | 굽힘에 `load_span` 값 있음(4점, 1차 미지원) |
| 레시피 | `RECIPE_NOT_FOUND`, `RECIPE_NOT_ACTIVE`, `RECIPE_RETIRED` | |
| | `RECIPE_TEST_TYPE_MISMATCH` | 매니페스트 `test_type` ≠ 요청 |
| | `RECIPE_PARAM_UNKNOWN`, `RECIPE_PARAM_OUT_OF_RANGE` | `recipe_params` 또는 MatNexus 치수가 매니페스트 범위 밖 |
| 슬롯 | `SLOT_UNKNOWN` | 요청 슬롯이 레시피 매니페스트에 없음 |
| | `SLOT_UNASSIGNED` | 형상 영역이 어느 슬롯에도 매핑 안 됨 |
| | `SLOT_NO_IDENTIFY`, `MODE_A_MULTI_SLOT`, `MODE_B_PLASTIC_UNSUPPORTED` | §4.3 |
| | `FIXED_SLOT_REFERENCE_MISSING`, `POISSON_RATIO_REQUIRED` | |
| 시험 데이터 | `RUN_NOT_FOUND` | MatNexus 404 |
| | `RUN_UNREADABLE` | MatNexus run 상태가 파싱 실패 |
| | `CHANNEL_MISSING` | 필수 채널 없음(굽힘: force + displacement/deflection 중 하나) |
| | `DISPLACEMENT_CHANNEL_UNAVAILABLE` | 지정 채널 없음 |
| | `CHANNEL_UNIT_INVALID`, `INLINE_RUN_INVALID` | |
| | `SPECIMEN_DIMENSION_MISSING` | TestSpec 필수 치수(굽힘: width, thickness, span) 없음 |
| 곡선 | `CURVE_TOO_FEW_POINTS` | 정규화 후 점 수 < `alignment.min_raw_points` |
| | `CURVE_NOT_MONOTONIC` | 단조 하중 구간 길이가 전체 x 범위의 `alignment.min_loading_fraction`(0.3) 미만 |
| | `CURVE_NO_CONTACT` | 접촉 시작 판정 실패 |
| | `CURVE_NO_ELASTIC_REGION` | 토우 맞춤 점 < `alignment.toe_fit_min_points`(5) 또는 탄성 구간 원본 점 < `alignment.elastic_min_raw_points`(5) |
| 형상 | `GEOMETRY_KIND_NOT_ALLOWED` | TestSpec 허용 형상 또는 매니페스트 `specimen.geometry_kinds` 밖 |
| | `GEOMETRY_DIMENSION_MISSING`, `GEOMETRY_DIMENSION_INVALID` | ≤0, 레이어 합 불일치 등 |
| | `FILE_NOT_FOUND`, `STP_UNREADABLE`, `STP_BODY_SLOT_MISMATCH`, `STP_CHECK_FAILED` | 임포트 실패·바디 수 불일치·형상 검사(얇은 부위 등) 실패 |
| 파라미터 | `PARAM_UNKNOWN` | 모델 `param_space()`에 없는 이름 |
| | `PARAM_RANGE_INVALID` | lower ≥ upper, initial 범위 밖, 물리 한계 위반(E≤0 등) |
| 조합 | `MATERIAL_SOLVER_INCOMPATIBLE` | MaterialModel `capabilities().solvers`에 솔버 없음 |
| | `SOLVER_ANALYSIS_UNSUPPORTED` | 레시피 해석 모드를 솔버가 지원 안 함 |
| | `FRICTION_IDENTIFY_UNSUPPORTED` | |
| 예산 | `BUDGET_EXCEEDS_LIMIT` | |
| explicit 검사 | `EXPLICIT_CHECK_UNAVAILABLE`, `EXPLICIT_CHECK_SECTION_INVALID` | §4.6.1 |
| 식별성 | `IDENTIFIABILITY_UNDERDETERMINED` | 사전 점검 실패(`identifiability.precheck_mode=reject`일 때). [testspec.md §9](testspec.md) |

새 코드는 MINOR 버전에서 추가될 수 있다. 클라이언트는 모르는 코드를 일반 검증 실패로 처리해야 한다.

## 8. 레시피 API

### 8.1 A1 `POST /v1/admin/recipes`

multipart: `bundle`(zip; 최상위에 `manifest.yaml`이 있는 레시피 폴더 압축, ≤ `recipes.max_bundle_bytes` 기본 50 MiB, 해제 총크기 ≤ `recipes.max_unpacked_bytes`·파일 수 ≤ `recipes.max_files` — [recipe.md §2](recipe.md)), `strict`(bool, 기본 false: 경고도 거절).

처리: 정적 검사(동기, 실패 400 `VALIDATION_FAILED` + `RECIPE_*` 사유) → 저장(불변) → 검증 잡을 큐에 넣음 → 202.

```json
{"schema_version": "1.0", "recipe_ref": "flex3p-d790-utm01@3", "recipe_id": "flex3p-d790-utm01",
 "version": 3, "content_hash": "sha256:9a1f...", "status": "verifying", "verification_id": "ver_01JC..."}
```

- 같은 `recipe_id@version` + 같은 `content_hash`가 `verifying`/`active`/`retired`로 있으면 200 기존 레코드(멱등).
- 같은 `recipe_id@version` + 다른 해시가 `verifying`/`active`/`retired`로 있으면 409 `RECIPE_VERSION_CONFLICT`. `rejected` 기록만 있으면 새 업로드를 받는다(시도 이력 보존).
- 정적 검사 사유 코드: [recipe.md §7.1](recipe.md).

### 8.2 레시피 상태

```
verifying ──(검증 PASS)──→ active ──(retire)──→ retired
    └──(검증 FAIL)──→ rejected
```

검증 잡은 일반 잡과 같은 워커·큐에서 순차 실행한다(잡 1건 동시 실행 규칙 유지). 상세 판정은 [recipe.md §7](recipe.md).

### 8.3 A2, A3, A4, E10, E11

| 엔드포인트 | 쿼리/본문 | 응답 핵심 필드 |
| --- | --- | --- |
| A2 | `status`, `test_type`, `recipe_id`, `limit`, `offset` | `{recipe_ref, recipe_id, version, kind, test_type, status, content_hash, created_at, created_by, retired_at, retire_reason, warnings_count}` |
| A3 | — | A2 필드 + `manifest`(파싱된 YAML 전체, JSON), `files[]`(`{path, bytes, sha256}`), `verification`(`VerificationReport`, [recipe.md §7.3](recipe.md)), `attempts[]`(rejected 이력) |
| A4 | 본문 `{reason: string(필수, ≤500)}` | 갱신된 A2 형. `verifying`이면 409 `RECIPE_NOT_RETIRABLE`, 이미 retired면 200 그대로 |
| E10 | `test_type`, `limit`, `offset` | `active`만: `{recipe_ref, recipe_id, version, kind, test_type, title, standards, compliance_available}` |
| E11 | — | `RecipePublic`: `{recipe_ref, kind, test_type, title, standards, unit_system, parameters[] (name, dimension, unit_si, default, range, matnexus_key), material_slots[] (slot, region), specimen.geometry_kinds, responses[] (response_key, test_channel, axis), compliance: {available, value_m_per_n}, status}` — 덱 본문·파일은 노출 안 함 |

폐기된 레시피는 새 잡에 쓸 수 없고(400 `RECIPE_RETIRED`), 그것을 참조한 기존 잡·결과는 그대로 조회된다. 파일은 삭제하지 않는다.

## 9. 하위 호환·버전 정책

| 변경 | 허용 위치 | 예 |
| --- | --- | --- |
| 응답 필드 추가 | MINOR | `metadata`에 새 키 |
| 선택 요청 필드 추가(기본값이 기존 동작과 같음) | MINOR | 2차 `friction.mode=identify` 활성 |
| 「확장 가능 enum」 값 추가 | MINOR | `test_type: compression`, `geometry.kind: cylinder`, `artifact.kind`, 오류 코드 |
| 고정 enum 값 추가(`status` 등) | MAJOR | — |
| 필드 삭제·이름 변경·단위 변경·의미 변경 | MAJOR(`/v2`) | — |

- 확장 가능 enum으로 지정된 것: `test_type`, `geometry.kind`, `material_model`, `optimizer`, `solver`, `phase`, `progress.stage`, `history.stage`, `artifact.kind`, `run_status` 값, 모든 오류·경고 코드.
- 서버는 `schema_version` MINOR ≤ 자신의 MINOR인 요청을 받는다. 결과는 생성 당시 `schema_version`으로 저장·반환한다(이전 결과를 재작성하지 않음).
- 2차·n차 추가는 이 표의 MINOR 범위로만 한다(결정 2026-10-24).

## 10. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| A-1 | E10–E12(레시피 공개 조회, 레지스트리 조회), E1(health)을 plan 표에 추가. MatNexus가 레시피·플러그인 목록을 화면 없이 고를 수 있게 하기 위함 | 가정 |
| A-2 | 다중 시험 동시 피팅을 위해 시험 단위 필드를 `tests[]`로 묶고, plan의 최상위 필드(`test_type`, `geometry`, `test_run_ids`)는 단일 시험 약식으로 유지 | 가정 |
| A-3 | 레시피 참조는 시험마다 `tests[].recipe`(필수). plan의 요청 핵심 필드 목록에 없지만 「일반 잡은 recipe_id@version과 치수만 넘긴다」 결정에 따름 | 가정 |
| A-4 | 인증은 정적 Bearer 토큰 2등급(서비스·관리자), 해시로 설정 파일에 보관. 토큰 발급·회전 API는 없음(설정 변경+재시작) | 가정 |
| A-5 | MatNexus 곡선은 `/curve?x=&y=&max_points=`(≤5000)로 가져온다. `/source` 원본 파싱은 1차 범위 밖(장비 파서가 MatNexus에 있음) | 가정 |
| A-6 | 레시피 등록 검증은 비동기(202). LS-DYNA dry-run이 수 분 이상 걸리기 때문 | 가정 |
| A-7 | `rejected` 레시피 버전 번호는 재사용 허용 | 가정 |
| U-1 | 결과 반입 경로(ADR 0026 구현 vs parameter-set 직접 입력)는 MatNexus 협의 대기. 이 계약은 결과를 조회 API로만 제공 | 미결 |
| U-2 | 잡 삭제·artifact 정리 API 필요 여부 | 미결 |
