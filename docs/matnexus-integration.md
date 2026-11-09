# matobserver ↔ MatNexus 연동: 공유·요청·협의 사항

작성 2026-11-09 · matobserver 계약 schema 1.2 기준 · MatNexus 참조 v1.302.0

## 1. 요약

matobserver는 MatNexus의 직접 환산(F-δ → σ-ε)을 보완하는 재료 역공학(FEMU) 엔진이다. 응력장이 불균일해 직접 환산할 수 없는 시험(1차 3점굽힘, 2차 압축)에 LS-DYNA 해석을 맞춰 탄성계수와 진응력–진소성변형률 테이블을 식별한다.

- matobserver는 MatNexus를 수정하지 않는다. 현재는 MatNexus API를 **읽기만** 한다.
- 결과를 MatNexus에 되돌려 쓰는 경로와 MatNexus에서 matobserver를 호출하는 경로가 없다. 이 문서의 협의 대상이다.
- matobserver는 LS-DYNA가 설치된 Windows PC에서 독립 서비스(REST API)로 동작한다.

| 구분 | 항목 수 | 우선순위 |
| --- | --- | --- |
| 요청(MatNexus 측 제공·확인) | 5 | R1·R2는 1차 실시험(G4) 전 필요 |
| 협의(양측 결정) | 7 | D1·D3은 결과 반입 설계 전 필요 |

## 2. 현재 연동 상태 (MatNexus 무수정)

| 방향 | 내용 | 사용 API |
| --- | --- | --- |
| matobserver → MatNexus | 시험 레코드·조건·치수 조회 | `GET /api/test-runs`, `GET /api/test-runs/{id}` |
| matobserver → MatNexus | 곡선 조회 | `GET /api/test-runs/{id}/curve`(최대 5000점) |
| 인증 | 개인 토큰(PAT) | `/api/auth/tokens` |
| MatNexus → matobserver | 없음 | — |
| 결과 반입 | 없음 | — |

matobserver가 그대로 쓰는 MatNexus 어휘:

| 항목 | MatNexus 정의 |
| --- | --- |
| 시험 종류 | `flexural`(FLX), `compression`(CMP) — `backend/app/modules/tests/definitions.py` |
| 채널(SI) | force(N), displacement(m), deflection(m), strain(1), time(s) |
| 굽힘 치수 | width, thickness, span, load_span, total_length — `vocabulary/definitions.py` |
| 압축 치수 | height, diameter, width, thickness |
| 조건 | temperature(K), test_speed(m/s) |
| 카드 블록 | elastic, table(plastic_strain / true_stress), rate_table(values, rows) — `matcore/export/dyna.py` |

## 3. MatNexus에 공유할 내용 (matobserver 제공)

### 3.1 API (비동기 잡)

| 메서드 | 경로 | 용도 |
| --- | --- | --- |
| POST | `/v1/files` | STP 등 입력 파일 업로드 |
| POST | `/v1/calibrations` | 잡 생성(`test_run_ids`, 레시피, 재료 슬롯, 옵션) |
| GET | `/v1/calibrations` | 잡 목록 |
| GET | `/v1/calibrations/{id}` | 상태·진행(단계: elastic → plastic → joint → finalize) |
| GET | `/v1/calibrations/{id}/result` | 최종 결과 |
| GET | `/v1/calibrations/{id}/history` | 반복 이력 |
| POST | `/v1/calibrations/{id}/cancel` | 취소 |
| GET | `/v1/artifacts/{id}` | 파일 다운로드(카드 .k, 곡선, 리포트). 서버 경로는 노출하지 않음 |

- 잡 상태: `queued → running → succeeded / failed / cancelled`. 콜백·웹훅은 없고 폴링 방식이다.
- 스키마 버전: 응답 1.2, 요청 1.0~1.2 수용. 추가만 하는 MINOR 개정으로 하위 호환을 유지한다.
- 상세 계약: `docs/contracts/api.md`, `result.md`

### 3.2 결과 (솔버 중립)

| 필드 | 내용 |
| --- | --- |
| 슬롯별 물성 | E, ν(입력값 기록), 진응력–진소성변형률 테이블 — MatNexus 카드 블록 어휘와 같은 형식 |
| 유효 범위 | 시편 내 도달 최대 소성변형률 εp_valid, 범위 밖 외삽 방식 |
| 기준 변형률속도 | ε̇_ref(굽힘 6hV/L²), 속도 의존 미식별 플래그 `RATE_DEPENDENCE_NOT_IDENTIFIED` |
| 불확도 | 시편별 피팅 통계(하한 추정으로 표기) |
| 식별성 진단 | 비식별·고상관 경고, 고정 권고 |
| 잔차 | 시험 vs 해석 곡선, J |
| 추적성 | input_hash, LS-DYNA 버전, 플러그인 버전, 레시피 `recipe_id@version` |
| 산출물 | LS-DYNA 카드(.k, 결과 테이블에서 재렌더링), 곡선 비교 데이터 |

### 3.3 ADR 0026(제안) 대응표

| ADR 0026 | matobserver 결과 |
| --- | --- |
| `InverseRun.input_run_ids` | 잡 요청 `test_run_ids` |
| `InverseRun.free_params` / `fixed` | 슬롯별 식별·고정 파라미터 |
| `InverseRun.fit` | J, 단계별 결과(`final_stage`, `e_by_stage`) |
| `InverseRun.residual_curves` | 곡선 비교 artifact |
| `EstimatedProperty.value_si` | E, 테이블 값(SI) |
| `EstimatedProperty.uncertainty` | 불확도(하한 추정) |
| `EstimatedProperty.valid_range` | εp_valid, `rate`(ε̇_ref) |

## 4. 요청 사항 (MatNexus 측)

| # | 요청 | 이유 | 필요 시점 |
| --- | --- | --- | --- |
| R1 | **서비스 계정 토큰** 발급(읽기: test-runs, curve, source, specimen) | 개인 토큰으로 자동 서비스를 돌리지 않기 위해 | G4(1차 실시험) 전 |
| R2 | **원본 해상도 곡선 접근**: `/source` 파일 제공 형식 확인 또는 다운샘플 없는 곡선 API | `/curve`는 5000점 상한이고 채널별 축약 결과가 달라, 채널을 맞추지 못하면 버려짐 | G4 전 |
| R3 | **장비·지그 식별 정보**를 시험 조건에 기록(장비 ID, 지그 ID 또는 레시피 참조) | 크로스헤드 변위 보정용 컴플라이언스 C를 장비+지그 조합별로 연결하기 위해 | G4 전 |
| R4 | **test_speed 필수 기록** | 기준 변형률속도 ε̇_ref 계산. 없으면 시간 채널로 추정 | G4 전 |
| R5 | **압축 시편 치수 키 추가**: 원뿔대(윗면·밑면 지름), STP 형상 참조 | 2차(압축) 형상 지원 | 2차 착수 전 |

## 5. 협의 사항

| # | 주제 | 선택지 | matobserver 제안 |
| --- | --- | --- | --- |
| D1 | **결과 반입 경로** | ① ADR 0026 `inverse` 모듈 구현 ② `parameter-sets`에 직접 값 입력 API 추가 ③ 반입 없이 artifact 링크만 보관 | ① — 구조가 이미 맞고, 불확도·유효 범위·잔차를 잃지 않음 |
| D2 | **반입 결과 상태** | 바로 채택 / draft 등록 후 사람이 채택 | draft — MCP 계획 D3(카드는 draft로만)과 일치 |
| D3 | **호출 방향** | ① MatNexus가 잡 생성·폴링 ② 사용자가 matobserver에 직접 제출 후 반입만 | 단기 ②, 중기 ① |
| D4 | **네트워크·인증** | matobserver 호스트(Windows PC)의 주소·포트·방화벽, MatNexus → matobserver 호출 시 토큰 | D3 ① 선택 시 정함 |
| D5 | **반복 시편 묶음 단위** | Specimen 단위 / 묶음(ADR 0020) 재사용 | 묶음 재사용 — 다중 시편 동시 피팅과 불확도에 필요 |
| D6 | **표시 방식** | 직접 환산 결과(`flexural.engineering`)와 나란히 "역공학 기반"으로 표시 | 나란히 표시 — 두 방법의 차이가 사용자에게 드러남 |
| D7 | **변경 공지** | 어휘·API 변경 시 상호 공지 방식 | MatNexus 어휘 변경 시 사전 공지, matobserver는 schema_version 정책 공유 |

## 6. 확인 질문 (MatNexus 담당자)

1. `/source`가 돌려주는 원본 파일 형식은 장비별로 다른가? 파서 프로파일을 matobserver가 재사용할 수 있는가?
2. ADR 0026의 진행 계획(일정, 담당)이 있는가? 없다면 D1 ②의 최소 API 추가가 가능한가?
3. 시험 조건에 장비·지그 ID를 넣을 필드가 있는가? 없다면 `conditions` 확장으로 가능한가?
4. 서비스 계정에 권한 범위(읽기 전용, 특정 부서)를 줄 수 있는가?
5. `testing_group`이나 묶음(ADR 0020)으로 "같은 재료, 같은 조건의 시편 n개"를 조회하는 표준 방법은?

## 7. 참조

- matobserver: `docs/plan.md`, `docs/decisions.md`, `docs/contracts/`(api, result, testspec, recipe, plugins, module-boundaries)
- MatNexus: `backend/openapi.json`, `backend/app/modules/tests/definitions.py`, `backend/app/modules/vocabulary/definitions.py`, `backend/matcore/export/dyna.py`, `docs/adr/0026-복합-물성-구성체와-시험법을-둔다.md`, ADR 0006·0007·0008·0020·0023·0038, `docs/[계획] MCP 서버.md`
