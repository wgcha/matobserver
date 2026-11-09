schema_version: 1.3 · 상태: 승인(G1, MINOR 1.3) · 2026-11-09

# 결과 스키마 계약

근거: plan.md 「개요·목표」(솔버 중립 산출물·메타데이터), 「재료 슬롯과 멀티스케일 등가화」, 「방법론」(Verifier 왕복·외삽 표시), 「시험 데이터 입력: MatNexus 어휘 채택」(카드 블록 어휘·ADR 0026), decisions.md(1차 산출물 솔버 중립, LS-DYNA 카드 2차 산출물, artifact_id).

`GET /v1/calibrations/{job_id}/result`(api.md E6)의 본문이다. 결과는 `succeeded` 시점에 JobStore에 확정 저장되고 이후 바뀌지 않는다. 수치는 모두 SI.

## 1. 최상위

```json
{
  "schema_version": "1.0",
  "job_id": "cal_01JC...",
  "status": "succeeded",
  "created_at": "...", "finished_at": "...",
  "units": {"length": "m", "force": "N", "stress": "Pa", "strain": "1", "density": "kg/m^3", "time": "s", "compliance": "m/N"},
  "slots": [ /* §2 SlotResult */ ],
  "metadata": { /* §4 */ },
  "artifacts": [ /* §6 ArtifactRef */ ]
}
```

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `schema_version` | string | 예 | 결과 생성 당시 버전(재작성 안 함) |
| `job_id` | string | 예 | |
| `status` | `"succeeded"` | 예 | 결과는 성공 잡만 |
| `units` | object | 예 | 고정값(SI). 소비자 확인용 |
| `slots` | `SlotResult[]` | 예 | 요청 슬롯 순서 그대로, fixed 슬롯 포함 |
| `metadata` | object | 예 | §4 |
| `artifacts` | `ArtifactRef[]` | 예 | §6 |

## 2. `SlotResult`

```json
{
  "slot": "body",
  "role": "identify",
  "member_position": null,
  "material_model": {"name": "mat024_table", "version": "1.0.0", "options": {"table_nodes": 8, "max_strain": 0.083, "max_strain_auto": true, "first_node_strain": 0.002, "smoothing_weight": 0.01}},
  "regularization": {"kind": "adjacent_slope", "weight": 0.01, "J_reg": 0.0007},
  "card_blocks": {
    "elastic": {"youngs_modulus": 2.31e9, "poisson_ratio": 0.38, "density": 1200.0},
    "table": [{"plastic_strain": 0.0, "true_stress": 4.10e7}, {"plastic_strain": 0.005, "true_stress": 4.52e7}],
    "rate_table": null
  },
  "identified_params": {
    "E":       {"value": 2.31e9, "unit": "Pa", "stage": "elastic", "lower": 2.0e8, "upper": 2.0e10, "initial": 2.05e9, "at_bound": false},
    "sigma_y": {"value": 4.10e7, "unit": "Pa", "stage": "plastic", "lower": 1.2e7, "upper": 1.2e8, "initial": 3.9e7, "at_bound": false}
  },
  "fixed_params": {"poisson_ratio": {"value": 0.38, "source": "request.reference"}, "density": {"value": 1200.0, "source": "request.reference"}},
  "valid_range": {"max_plastic_strain": 0.083, "source": "field_max", "test_keys": ["t1"]},
  "extrapolation": {"method": "last_slope", "beyond_plastic_strain": 0.083, "rows_extrapolated_from_index": 9},
  "hardening": {"law": null, "coefficients": null},
  "derived": {"lsdyna_keyword": "MAT_024"}
}
```

| 필드 | 타입 | 단위 | 설명 |
| --- | --- | --- | --- |
| `slot` | string | — | 매니페스트·요청 슬롯 이름 |
| `role` | `identify` \| `fixed` | — | |
| `member_position` | int \| null | — | 요청값(ADR 0026 대응) |
| `material_model` | `{name, version, options}` | — | 레지스트리 이름·버전·**해석된 옵션**(1.2: `resolved_options()` — 자동 결정 max_strain·smoothing_weight 등 실제 적용값). `input_hash` 계산에 포함 |
| `regularization` | `{kind, weight, J_reg}` \| null | — | (1.2) 정칙화 종류(`adjacent_slope`)·적용 가중·최종 θ의 J_reg. 정칙화 없는 모델이면 null. `residuals.J`에는 포함되지 않음 |
| `card_blocks` | object | SI | **솔버 중립 1차 산출물**. MatNexus 카드 블록 어휘(§3) |
| `identified_params` | `{param: ParamResult}` | SI | identify 슬롯의 식별 변수. fixed 슬롯은 `{}` |
| `fixed_params` | `{name: {value, source}}` | SI | 식별하지 않은 입력값(ν, ρ, `fixed_value`로 고정한 변수). `source`: `request.reference` \| `request.params` \| `config.default` |
| `valid_range` | object \| null | 1 | 유효 범위 = 최적점 해석에서 이 슬롯 요소가 도달한 최대 유효소성변형률(추출기 `field_max`). 탄성 전용이면 null |
| `extrapolation` | object \| null | — | §3.2 |
| `hardening` | `{law, coefficients}` | SI | 계수 모드면 `law`(`swift`\|`voce`\|`swift_voce`)와 계수, 테이블 모드면 null. (1.1) 계수는 강상관으로 비유일할 수 있어 참고값이며, **판정·불확도·MatNexus 반입은 `card_blocks.table` 기준** |
| `derived.lsdyna_keyword` | string | — | 2차 산출물 카드 키워드(`MAT_001`\|`MAT_024`) |

`ParamResult`: `{value, unit, stage, lower, upper, initial, at_bound}`. `at_bound` = 값이 경계에서 정규화 거리 `result.at_bound_tol`(0.01) 이내 → 경고 `PARAM_AT_BOUND`.

## 3. `card_blocks` — MatNexus 카드 블록 어휘

`NeutralMaterial.to_card_blocks()`([plugins.md §1](plugins.md)) 출력과 동일.

| 블록 | 키 | 단위 | 1차 | MatNexus 대응 |
| --- | --- | --- | --- | --- |
| `elastic` | `youngs_modulus` | Pa | 예 | `Need("elastic", values=("youngs_modulus", ...))` (`matcore/export/dyna.py` *MAT_001/*MAT_024) |
| | `poisson_ratio` | 1 | 예(입력 기록) | 같음 |
| | `density` | kg/m³ | 예(입력 기록, 없으면 기본값+경고) | 같음(dyna 렌더러는 RO 필수) |
| `table` | 행 `{plastic_strain, true_stress}` | 1, Pa | 탄소성 슬롯 | `deck.pairs("table", "plastic_strain", "true_stress")` |
| `rate_table` | `{values: {model, cs_d, cs_p, rate_count, ...}, rows: [{strain_rate, plastic_strain, true_stress}]}` | 1/s, 1, Pa | null(n차) | MatNexus `rate_table` 블록: `deck.values("rate_table")`(model·cs_d·cs_p·rate_count) + `deck.rows("rate_table")`(`rate_curves()`) |

MatNexus `elastic` 블록은 온도별 행을 가질 수 있으나 이 SW는 온도 무관이므로 단일 값 객체로 낸다(MatNexus의 1행과 같은 뜻).

### 3.1 테이블 생성 규칙

| 모드 | 규칙 |
| --- | --- |
| `mat024_table` | 노드 그대로(εp=0 포함 m+1행) |
| `mat024_coeff` | 경화식을 εp ∈ [0, εp_valid]에서 `result.table_points`(50)점 샘플(첫 점 0, 간격 `result.table_spacing`=`geometric`, 첫 간격 `result.table_first_step`=1e-4) |
| 공통 | NeutralMaterial 불변식 만족(첫 행 εp=0, 변형률 엄격 증가, 응력 비감소, ≥2행) |
| 탄성 전용(`mat001`) | `table: null` |

### 3.2 유효 범위와 외삽

| 항목 | 규칙 |
| --- | --- |
| 유효 범위 | εp_valid = `valid_range.max_plastic_strain`. 여러 시험이면 시험별 최대값 중 최대(`test_keys`에 기여 시험) |
| 범위 밖 행 | `result.extrapolate_to_strain`(기본 null)이 εp_valid보다 크면 그 변형률까지 행을 추가하고 `extrapolation.rows_extrapolated_from_index`에 첫 외삽 행 인덱스를 적는다. null이면 행 추가 없음 |
| 외삽 방식 | `extrapolation.method`: `last_slope`(마지막 기울기 유지, 행 추가 없을 때는 솔버 기본 동작과 같음) \| `hardening_law`(계수 모드에서 경화식 연장). 설정 `result.extrapolation_method`(기본 `last_slope`, 계수 모드면 `hardening_law` 허용) |
| 테이블 모드 노드가 εp_valid 밖 | 그 노드는 외삽으로 표시(식별 근거 약함), 경고 `TABLE_NODES_BEYOND_VALID_RANGE` |

## 4. `metadata`

| 키 | 타입 | 설명 |
| --- | --- | --- |
| `test_types` | string[] | 식별에 쓴 TestSpec 키(`["bending"]`) |
| `matnexus_test_type_keys` | string[] | `["flexural"]` |
| `tests` | `TestSummary[]` | §4.1 |
| `assumptions` | `Assumption[]` | §4.2 |
| `residuals` | object | §4.3 |
| `uncertainty` | object | §4.4 |
| `identifiability` | object | §4.5 |
| `compliance` | `ComplianceInfo[]` | §4.6 (시험별) |
| `equivalence` | object | §4.7 |
| `termination` | object | §4.8 |
| `roundtrip` | object | §4.9 |
| `warnings` | `Warning[]` | §4.10 |
| `traceability` | object | §4.11 (`input_hash`는 슬롯 해석된 옵션을 포함, 1.2) |
| `explicit_check` | object | §4.12 (옵션 검사, 꺼져 있으면 `{"enabled": false}`) |
| `rate` | object | §4.13 기준 변형률속도·속도 의존 여부 |

### 4.1 `TestSummary`

```json
{"test_key": "t1", "test_type": "bending", "recipe_ref": "flex3p-d790-utm01@3",
 "recipe_params": {"span": 0.064, "r_ind": 0.005, "r_sup": 0.005, "fric": 0.1},
 "geometry": {"kind": "box", "dimensions": {"width": 0.0101, "thickness": 0.0040, "total_length": 0.080}},
 "displacement_channel": "displacement",
 "runs": [{"run_id": "6f1c...", "specimen_id": "a2b...", "case_id": "t1-c1", "source_sha256": "...", "conditions": {"temperature": 296.15, "test_speed": 3.3e-5}}],
 "cases": [{"case_id": "t1-c1", "run_ids": ["6f1c..."], "element_count": 12800, "symmetry": "quarter"}],
 "window": {"axis": "displacement", "x_min": 0.0, "x_max": 0.0061, "x_elastic_end": 0.0012},
 "identified_params": {},
 "weight": 1.0}
```

`identified_params`: 비재료 식별 변수([plugins.md §3 R9](plugins.md), 키는 `_test.{test_key}.` 뒤 이름, 값은 §2 `ParamResult`). 1차 bending은 항상 `{}`. 2차 압축의 `mu` 등이 여기에 온다.

### 4.2 `Assumption`

`{code, text}`. 항상 포함: `J2_ISOTROPIC_HARDENING`(J2 등방경화), `RATE_INDEPENDENT`(속도 무관 — 근거 값은 `metadata.rate.reference_strain_rate_1_s`, §4.13), `TEMPERATURE_INDEPENDENT`, `POISSON_RATIO_INPUT`(ν 입력값), `NO_DAMAGE`(파단·손상 미포함). 조건부: `ISOTROPIC_EQUIVALENT`(모드 A, 등가 등방성), `RIGID_JIG`(지그 강체), `COMPLIANCE_FROM_RECIPE`, `ELASTIC_ONLY`(취성 재료 E만), `FRICTION_FIXED`.

### 4.3 `residuals`

```json
{
  "J": 0.0412,
  "J_stage": {"elastic": 0.0081, "plastic": 0.0412},
  "per_test": [{"test_key": "t1", "J_t": 0.0412,
                "per_response": [{"response_key": "force_disp", "J": 0.0412, "elastic": 0.0081, "plastic": 0.0331, "shortfall": 0.0}]}],
  "per_run": [{"run_id": "6f1c...", "test_key": "t1", "J": 0.0398, "rmse_N": 3.21, "max_abs_err_N": 9.8,
               "nrmse": 0.021, "shortfall": 0.0}]
}
```

`J`·구간 오차 정의는 [testspec.md §8](testspec.md). `nrmse` = RMSE / max|F_exp| (전 구간). 잔차 곡선 자체는 artifact `curve_compare`.

### 4.4 `uncertainty` — 시편별 피팅 통계

```json
{
  "method": "per_specimen_scaling",
  "per_run": [{"run_id": "6f1c...", "specimen_id": "a2b...", "J": 0.0398, "E_scaled": 2.28e9, "sigma_y_scaled": null}],
  "statistics": {"body.E": {"mean": 2.30e9, "stdev": 4.1e7, "cv": 0.018, "n": 5}},
  "notes": []
}
```

| 항목 | 규칙 |
| --- | --- |
| (1.1) 기준 | 탄소성 불확도·비교는 테이블 응력(εp 노드별) 기준. 계수 모드의 계수별 통계는 내지 않는다 |
| `method` | `per_specimen_scaling`(기본) \| `per_specimen_refit`(설정 `uncertainty.per_specimen_refit=true`, run별 재식별 — 예산 `uncertainty.refit_max_evaluations` 별도) \| `none`(run 1개) |
| `E_scaled` | 모드 A: E* · k₀,exp,r / k₀,sim* (run r 시험 기울기 / 최적 해석 기울기, 정렬 3단계 기울기 정의). 모드 B: null + `notes`에 `MODE_B_SCALING_NA` |
| `statistics` | run이 2개 이상일 때 `E_scaled` 표본 평균·표준편차(n−1)·변동계수. 키는 전역 θ 이름 |
| ADR 0026 대응 | `EstimatedProperty.uncertainty = {kind: "stdev", value: stdev, n, method}` (§7) |

### 4.5 `identifiability`

```json
{
  "precheck": {"n_params": {"elastic": 1, "plastic": 9}, "n_configs": 1, "n_points_plastic": 170, "verdict": "ok", "codes": []},
  "post_fit": [
    {"stage": "plastic", "params": ["body.sigma_y", "body.dsig_1"],
     "normalized_sensitivity": {"body.sigma_y": 1.84, "body.dsig_1": 0.42},
     "correlation": [[1.0, -0.71], [-0.71, 1.0]],
     "relative_stdev": {"body.sigma_y": 0.006, "body.dsig_1": 0.08},
     "relative_stdev_is_lower_bound": true,
     "condition_number": 3.2e3,
     "evaluations": 9}
  ],
  "flags": [{"code": "HIGH_CORRELATION", "stage": "plastic", "params": ["body.dsig_7", "body.dsig_8"], "value": 0.97}],
  "recommendations": [{"action": "fix_param", "param": "body.dsig_8", "reason": "HIGH_CORRELATION"}]
}
```

`relative_stdev`는 격자점 잔차 독립 가정에 따른 **하한 추정**이다(항상 `relative_stdev_is_lower_bound: true`, [testspec.md §9.3](testspec.md)). `verdict`: `ok` | `warn` | `underdetermined`(이 값이면 잡은 생성되지 않으므로 결과에는 `warn` 모드에서만 나타남). 규칙은 [testspec.md §9](testspec.md). `post_fit` 생략 시 `[]`와 경고 `DIAGNOSTICS_SKIPPED`.

### 4.6 `ComplianceInfo` (시험별)

```json
{"test_key": "t1", "displacement_channel": "displacement", "applied": true,
 "value_m_per_n": 1.8e-7, "source": "recipe", "recipe_ref": "flex3p-d790-utm01@3",
 "method": "rigid_block", "calibrated_at": "2026-10-01", "reason": null, "warnings": []}
```

`applied=false`일 때 `reason`: `missing`(C 없음 → 경고 `COMPLIANCE_MISSING` "E 신뢰도 낮음") | `deflectometer`. `warnings`: `COMPLIANCE_INSTRUMENT_MISMATCH`, `COMPLIANCE_NONMONOTONIC`.

### 4.7 `equivalence` — 등가화 구성·하중 모드

```json
{
  "mode": "A",
  "load_mode": "three_point_bending",
  "configurations": [
    {"test_key": "t1", "geometry_kind": "box", "stack_axis": null,
     "regions": [{"region": "specimen", "slot": "body", "part_id": 1, "volume_m3": 3.23e-6}],
     "span_m": 0.064, "orientation": "load_along_thickness"}
  ],
  "note": "등가 물성은 하중 모드·형상에 의존한다"
}
```

`load_mode` 값은 TestSpec 클래스 속성 `load_mode`(1.1, plugins.md §4.5)에서 온다. 시험마다 다르면 목록. enum(확장 가능): `three_point_bending`(1차), `four_point_bending`, `uniaxial_compression`(2차), `dynamic_bending`(n차).

### 4.8 `termination`

`{reason, evaluations, evaluations_failed, wall_time_s, per_stage: {elastic: {reason, evaluations, best_eval_index}, plastic: {...}, joint: {..., accepted: bool}}, final_stage, e_by_stage}`. (1.2) `final_stage`: `elastic`\|`plastic`\|`joint`. `e_by_stage`: `[{slot, elastic: E₁, plastic: E(=E₁), joint: E₃\|null, rel_change_joint: (E₃−E₁)/E₁}]` (Pa, 1). `reason`: `budget` | `stagnation` | `converged` | `wall_time` (wall_time이면 경고 `WALL_TIME_REACHED`).

### 4.9 `roundtrip` — 솔버 중립 테이블 왕복 검사

Verifier 기능을 결과 확정 단계에서 자동 실행: 슬롯별 `card_blocks`로 1요소 단축 덱을 만들어 돌리고 응력–소성변형률이 입력 테이블을 재현하는지 본다.

```json
{"enabled": true, "per_slot": [{"slot": "body", "passed": true, "max_rel_err": 0.0021, "tolerance": 0.01, "artifact_id": "art_..."}]}
```

설정: `verify.roundtrip_enabled`(true), `verify.roundtrip_tol`(0.01), `verify.roundtrip_strain`(εp_valid). 실패해도 잡은 `succeeded`, 경고 `ROUNDTRIP_FAILED`.

### 4.10 `Warning`

`{code, message, test_key?, slot?, run_id?, param?}`. 1차 경고 코드: `COMPLIANCE_MISSING`, `COMPLIANCE_INSTRUMENT_MISMATCH`, `COMPLIANCE_NONMONOTONIC`, `DENSITY_DEFAULTED`, `PARAM_AT_BOUND`, `IDENTIFIABILITY_LOW_DATA`, `HIGH_CORRELATION`, `INSENSITIVE_PARAM`, `ILL_CONDITIONED`, `DIAGNOSTICS_SKIPPED`, `TABLE_NODES_BEYOND_VALID_RANGE`, `ROUNDTRIP_FAILED`, `WALL_TIME_REACHED`, `FALLBACK_EXPLICIT_USED`, `KE_RATIO_HIGH`, `BEAM_THEORY_APPROXIMATE`, `MESH_COARSENED`, `SYMMETRY_IGNORED_STP`, `PARAM_OVERRIDES_SPECIMEN`, `RECIPE_WARNINGS`(레시피 검증 경고 승계), `MESH_QUALITY_LOW`(1.1: 메시 품질 임계 미달, 경고 — plugins.md G12), `EXPLICIT_CHECK_DEVIATION`, `EXPLICIT_CHECK_KE_HIGH`, `EXPLICIT_CHECK_FAILED`, `EXPLICIT_CHECK_SKIPPED`(§4.12), `RATE_DEPENDENCE_NOT_IDENTIFIED`, `REFERENCE_RATE_UNKNOWN`(§4.13). 새 코드는 MINOR에서 추가.

### 4.11 `traceability`

```json
{
  "input_hash": "sha256:4be1...",
  "request_hash": "sha256:...",
  "run_data_hashes": {"6f1c...": "sha256:..."},
  "recipes": [{"recipe_ref": "flex3p-d790-utm01@3", "content_hash": "sha256:9a1f..."}],
  "solver": {"name": "lsdyna", "adapter_version": "1.0.0", "solver_version": "R13.1.1", "precision": "double", "parallel": "smp", "ncpu": 4},
  "plugins": {"optimizer": {"bo": "1.0.0", "lsq": "1.0.0"}, "material_model": {"mat024_table": "1.0.0"},
              "geometry_mesher": {"box": "1.0.0", "jig.bending3p_rollers": "1.0.0"}, "test_spec": {"bending": "1.0.0"}},
  "matobserver_version": "0.1.0",
  "config_hash": "sha256:...",
  "seed": 20261108,
  "matnexus": {"source_sha256": {"6f1c...": "..."}, "curve_max_points": 5000}
}
```

| 키 | 정의 |
| --- | --- |
| `request_hash` | 정규화 요청 JSON(키 정렬, 공백 없음, 기본값 채운 후) sha256 |
| `run_data_hashes` | `RunRecord.fetched_hash`(정규화 채널·치수 sha256) |
| `input_hash` | sha256(`request_hash` + 정렬된 `run_data_hashes` + 정렬된 recipe `content_hash` + STP 파일 sha256 + `config_hash`). **같은 input_hash + 같은 솔버 버전 → 같은 결과**가 성공 기준(재현)의 판정 키 |
| `config_hash` | 식별 결과에 영향을 주는 설정 하위집합(`alignment.*`, `objective.*`, `engine.*`, `identifiability.*`, `material.*`, `result.*`, `cases.*`, `testspec.*`, `solver.implicit.*`, `solver.explicit.*`, `solver.d3plot_frames`) 정규화 JSON sha256. 경로·토큰·코어 수 제외 |
| `solver.solver_version` | 최적점 해석 d3hsp에서 읽은 문자열 |

### 4.12 `explicit_check` — explicit 재현 검사 (1차 옵션)

왕복 검사(§4.9) 이후 최종 카드를 낙하 모델과 같은 요소 정식화·explicit(질량 스케일링 금지, 준정적 속도)로 1회 재실행한 결과. 규칙은 [testspec.md §8.1](testspec.md). 임계값 초과는 경고만.

```json
{
  "enabled": true,
  "settings": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}, "loading_velocity_m_s": 0.5,
               "max_ke_ratio": 0.05, "max_region_error": 0.05, "max_peak_rel_diff": 0.05, "mass_scaling": false},
  "per_case": [
    {"test_key": "t1", "case_id": "t1-c1", "status": "ok",
     "run_state": "normal", "ke_ie_ratio_max": 0.012,
     "vs_implicit":   {"elastic": 0.018, "plastic": 0.026, "peak_force_rel_diff": -0.011, "shortfall": 0.0},
     "vs_experiment": {"elastic": 0.021, "plastic": 0.041, "peak_force_rel_diff": -0.017, "shortfall": 0.0},
     "within_tolerance": true,
     "artifact_ids": {"explicit_check": "art_...", "explicit_check_curves": "art_..."}}
  ],
  "warnings": []
}
```

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `per_case[].status` | `ok` \| `deviation` \| `failed` \| `skipped` | `deviation` = 임계값 초과(경고 `EXPLICIT_CHECK_DEVIATION`) |
| `vs_implicit`, `vs_experiment` | object | 구간별 정규화 오차 e_k(1), 피크 하중 상대차(1), 범위 부족 비율(1). (1.1) `vs_experiment`는 케이스 내 run별 지표의 산술평균 |
| `ke_ie_ratio_max` | number | GLSTAT 운동/내부 에너지 비 최대 |
| `skip_reason` | string \| null | `skipped`일 때: `no_explicit_controls` \| `mass_scaling` \| `adapter_unsupported` |

### 4.13 `rate` — 기준 변형률속도와 속도 의존

```json
{
  "reference_strain_rate_1_s": {"per_run": [{"run_id": "6f1c...", "value": 1.9e-3, "velocity_source": "condition.test_speed",
                                           "formula": "6*h*V/L^2", "inputs": {"h": 0.004, "V": 3.3e-5, "L": 0.064}}],
                                "per_test": [{"test_key": "t1", "median": 1.9e-3, "min": 1.8e-3, "max": 2.0e-3}],
                                "overall_range": [1.8e-3, 2.0e-3]},
  "rate_dependence_identified": false,
  "rate_model": null,
  "rate_coefficients": null,
  "identified_rate_range_1_s": null,
  "advisory": {"code": "RATE_DEPENDENCE_NOT_IDENTIFIED",
               "message": "속도 의존을 식별하지 않았다. 기준 변형률속도보다 크게 빠른 동적(낙하·충격) 해석에 쓰면 응력을 과소평가할 수 있다"}
}
```

| 필드 | 규칙 |
| --- | --- |
| `reference_strain_rate_1_s` | TestSpec `reference_strain_rate()`(bending: 6hV/L², [testspec.md §10.6](testspec.md)) run별 값과 시험별 요약. 계산 불가 run은 `value: null` + 경고 `REFERENCE_RATE_UNKNOWN` |
| `rate_dependence_identified` | 1차·2차 항상 false → 경고 `RATE_DEPENDENCE_NOT_IDENTIFIED`를 `warnings`와 `advisory`에 함께 기록(동적 해석 적용 시 주의) |
| `rate_model`, `rate_coefficients`, `identified_rate_range_1_s` | 1차 null. **n차 확장 지점**: 고속 TestSpec이 같은 필드를 채운다(`rate_model: "cowper_symonds"`, `rate_coefficients: {cs_d, cs_p}` — `card_blocks.rate_table.values`와 같은 키, 또는 `"table"` + `rate_table.rows`) |
| ADR 0026 | `EstimatedProperty.valid_range.rate = overall_range` |

## 5. 2차 산출물

LS-DYNA 카드(.k)는 `card_blocks`에서 파생한 2차 산출물이다(artifact `card_lsdyna`, 슬롯별). (1.1) 최적점 실행 폴더의 카드를 복사하지 않고, 엔진이 결과 조립 시 **결과 NeutralMaterial**(테이블은 §3.2 유효 범위 절단·외삽 행 반영 후)을 `SolverAdapter.render_card(neutral, slot, options)`(plugins.md §4.2)로 다시 렌더링한다. 어댑터 `card_format`이 None이면 `card_lsdyna`를 내지 않는다. 키워드는 SolverAdapter의 재료 writer가 쓰며 *MAT_001(탄성 전용) 또는 *MAT_024 + *DEFINE_CURVE(LCSS)이고 단위계는 mm·N·s·tonne(덱 머리 주석에 명시). 타 솔버 카드는 MatNexus 렌더러가 `card_blocks`로 만든다.

## 6. artifact 목록 `ArtifactRef`

```json
{"artifact_id": "art_01JC...", "kind": "card_lsdyna", "media_type": "text/plain", "filename": "body_mat024.k",
 "bytes": 2210, "sha256": "...", "slot": "body", "test_key": null, "created_at": "..."}
```

| `kind` (확장 가능 enum) | 형식 | 범위 | 내용 |
| --- | --- | --- | --- |
| `result_json` | application/json | 잡 | 이 결과 본문 사본 |
| `card_lsdyna` | text/plain (.k) | 슬롯 | 2차 산출물 카드 — (1.1) 결과 NeutralMaterial에서 `render_card`로 재렌더링(§5) |
| `card_blocks_json` | application/json | 슬롯 | `card_blocks` 단독(MatNexus 반입용) |
| `curve_compare` | text/csv | 케이스·run | 열: `x_m, f_exp_N, f_sim_N, residual_N, region`(격자 위) |
| `curve_raw` | text/csv | 케이스 | 최적점 해석 원곡선 `x_m, f_N` |
| `history` | text/csv | 잡 | `eval_index, stage, J, status, <θ 열...>` |
| `convergence_plot_data` | application/json | 잡 | 단계별 best_J 이력 |
| `deck_best` | application/zip | 케이스 | 최적점 실행 폴더(덱·include·binout·d3hsp·messag, d3plot은 `solver.keep_d3plot`에 따라) |
| `mesh` | text/plain (.k) | 케이스 | `mo_specimen.k` |
| `roundtrip` | application/zip | 슬롯 | 1요소 왕복 덱·결과 |
| `explicit_check` | application/zip | 케이스 | explicit 재현 검사 실행 폴더(덱·binout·d3hsp·glstat, d3plot 제외) |
| `explicit_check_curves` | text/csv | 케이스 | 열: `x_m, f_exp_N, f_implicit_N, f_explicit_N, region` (같은 격자) |
| `identifiability_json` | application/json | 잡 | 진단 전체(S, 상관행렬 원값) |
| `verification_bundle` | application/zip | 레시피 | 레시피 dry-run 결과(레시피 API 전용) |

## 7. MatNexus 어휘 매핑표

### 7.1 입력 채널·치수 키

| matobserver 사용처 | MatNexus 원천 | 키 | 단위 | 비고 |
| --- | --- | --- | --- | --- |
| `tests[].test_type=bending` | `TestType.key` | `flexural` (abbr FLX) | — | `compression`(CMP) ↔ 2차 `compression` |
| y 채널 | flexural 채널 | `force` | N | 필수 |
| x 채널(크로스헤드) | flexural 채널 | `displacement` | m | 컴플라이언스 보정 |
| x 채널(처짐계) | flexural 채널 | `deflection` | m | 보정 생략 |
| 기록 | flexural 채널 | `time` | s | n차 기준축 |
| 형상 폭 | 시편 분류 「굽힘」 | `width` | m | `GET /api/specimens/{id}/dimensions` → `fields[].measured` ?? `nominal` |
| 형상 두께 | 같음 | `thickness` | m | |
| 레시피 `span` | 같음 | `span` | m | 매니페스트 `matnexus_key` |
| 4점 판별 | 같음 | `load_span` | m | 값 있으면 1차 거절 |
| 형상 길이 | 같음 | `total_length` | m | |
| 압축(2차) | 시편 분류 「압축」 | `height`, `diameter`, `width`, `thickness` | m | 원뿔대 `top_diameter`·`bottom_diameter`는 SW 정의 |
| 조건 기록 | flexural 조건 | `temperature`, `test_speed` | K, m/s | |
| 컴플라이언스 장비 대조 | `TestRun` | `instrument` | — | |
| 목록 재료 필터 | `TestRun` | `material_id` | — | |
| 추적성 | `TestRun` | `source_sha256` | — | |
| 곡선 | `GET /api/test-runs/{id}/curve?x=&y=&max_points=` | `CurvePointsOut.points` [[x,y]] | SI | max_points ≤ 5000 |

### 7.2 출력 카드 블록

| matobserver | MatNexus 블록·키 | 렌더러 |
| --- | --- | --- |
| `card_blocks.elastic.youngs_modulus` | `elastic.youngs_modulus` | dyna_elastic(*MAT_001), dyna(*MAT_024) |
| `card_blocks.elastic.poisson_ratio` | `elastic.poisson_ratio` | 같음 |
| `card_blocks.elastic.density` | `elastic.density` | 같음(RO) |
| `card_blocks.table[].plastic_strain` / `true_stress` | `table` 행 `plastic_strain` / `true_stress` | dyna(*MAT_024 + *DEFINE_CURVE), abaqus_def(*PLASTIC) |
| `card_blocks.rate_table.values` (n차) | `rate_table` 값 `model`, `cs_d`, `cs_p`, `rate_count` | dyna(C·P 요약), dyna_rate |
| `card_blocks.rate_table.rows[]` (n차) | `rate_table` 행 `strain_rate`, `plastic_strain`, `true_stress` | dyna_rate |

### 7.3 ADR 0026(제안) `InverseRun`·`EstimatedProperty` 대응

| ADR 0026 필드 | matobserver 출처 | 비고 |
| --- | --- | --- |
| `InverseRun.id` | `job_id` | MatNexus가 자체 ID 발급 시 외부 참조로 보관 |
| `InverseRun.assembly_id` | — | MatNexus 측 구성체. 요청에 없음(협의) |
| `InverseRun.recipe_version_id` | `metadata.tests[].recipe_ref` + `traceability.recipes[].content_hash` | MatNexus `SimulationRecipeVersion`과 matobserver 레시피는 별개 개념 — 매핑 방식 협의 |
| `InverseRun.input_run_ids` | `metadata.tests[].runs[].run_id` (MatNexus uuid만) | inline run은 제외 |
| `InverseRun.free_params` `{member_position, property, bounds}` | 슬롯 `identified_params[*]`: `member_position`=슬롯 `member_position`, `property`=param 이름(E→`youngs_modulus`), `bounds`=[lower, upper] | |
| `InverseRun.fixed` `{member_position, property, value, source}` | fixed 슬롯 `card_blocks`, 각 슬롯 `fixed_params`, `compliance`, 레시피 `fric` | `source` 그대로 |
| `InverseRun.status` | `succeeded`→`done`, `failed`/`cancelled`→`failed`, `queued`/`running` 동일 | |
| `InverseRun.fit` | `metadata.residuals` | |
| `InverseRun.residual_curves` | artifact `curve_compare` | |
| `EstimatedProperty.target_kind` | 모드 A → `assembly`, 모드 B → `member` | |
| `EstimatedProperty.member_position` | 슬롯 `member_position` | |
| `EstimatedProperty.property_key` / `value_si` / `dimension` | `youngs_modulus` / `card_blocks.elastic.youngs_modulus` / `stress` | 테이블은 ADR에 자리 없음 → 카드 블록 `table`로 별도 반입(협의) |
| `EstimatedProperty.uncertainty` | `{kind: "stdev", value, n, method}` ← `metadata.uncertainty.statistics` | |
| `EstimatedProperty.valid_range` | `{temperature: [T_min, T_max] (runs 조건), rate: metadata.rate.reference_strain_rate_1_s.overall_range, plastic_strain: [0, εp_valid]}` | `plastic_strain`은 ADR 예시에 없는 확장 키 |

## 8. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| S-1 | 결과 수치 단위는 SI(Pa)로 MatNexus `value_si`와 같게 함. LS-DYNA 카드(.k)만 mm·N·s·tonne | 가정 |
| S-2 | 불확도는 1차에서 시편별 기울기 배율 통계(E)와 run별 잔차로 한정, 탄소성 변수 불확도는 사후 진단의 `relative_stdev`(하한 추정)로 대체 | 가정 |
| S-5 | artifact 실제 파일은 `work_dir/{job_id}/artifacts/` 단일 위치(module-boundaries.md §4.5) | 가정 |
| S-3 | 왕복 검사는 결과 확정 단계에서 자동 실행(실패해도 잡 성공, 경고) | 가정 |
| S-4 | 테이블은 기본적으로 유효 범위까지만 내고 외삽 행은 설정으로만 추가 | 가정 |
| U-1 | ADR 0026은 제안 상태이고 테이블형 물성을 담을 자리가 없음 → 결과 반입 형식은 MatNexus 협의(decisions: MatNexus 무수정) | 미결 |
| U-2 | ADR `classification`(기밀 여부)은 matobserver가 판단할 수 없어 출력하지 않음 | 미결 |

## 변경 이력

| 버전 | 날짜 | 변경(추가·기본값 조정만, 기존 필드 의미 불변) |
| --- | --- | --- |
| 1.0 → 1.1 | 2026-11-08 | `hardening` 계수 참고값·테이블 기준 판정(§2), 불확도 테이블 기준(§4.4) |
| 1.0 → 1.1 | 2026-11-08 | `load_mode` 출처 = TestSpec 속성(§4.7) |
| 1.0 → 1.1 | 2026-11-08 | `vs_experiment` run 평균(§4.12) |
| 1.0 → 1.1 | 2026-11-08 | 경고 `MESH_QUALITY_LOW`(§4.10) |
| 1.0 → 1.1 | 2026-11-08 | `card_lsdyna`를 결과 NeutralMaterial에서 `render_card`로 재렌더링(§5·§6) |
| 1.1 → 1.2 | 2026-11-09 | `material_model.options` = 해석된 옵션(자동 max_strain·smoothing_weight), `input_hash` 반영(§2·§4.11) |
| 1.1 → 1.2 | 2026-11-09 | 슬롯 `regularization` 필드(§2) |
| 1.1 → 1.2 | 2026-11-09 | `termination.final_stage`·`per_stage.joint`·`e_by_stage`(§4.8) |
| 1.2 → 1.3 | 2026-11-09 | 내용 변경 없음(버전 번호만 1.3) |
