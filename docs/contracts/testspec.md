schema_version: 1.0 · 상태: 초안 · 2026-11-08

# TestSpec·응답 정의·정렬 규칙 계약

근거: plan.md 「시험 정의(TestSpec)와 데이터 정렬 규칙」, 「정렬·재샘플링 규칙(표준, 1차부터 적용)」, 「변위 채널과 컴플라이언스 보정」, 「방법론」(목적함수), 「고속 시험(n차)」, decisions.md(8단계 규칙, 크로스헤드 기본·C 레시피 등록, 다중 시험 동시 피팅, 식별성 진단).

인터페이스 시그니처는 [plugins.md §4.5](plugins.md). 여기서는 데이터 모양과 수치 규칙을 정한다. 수치는 모두 SI.

## 1. TestSpec 구성

| 구성 (plan 표) | 계약 위치 | 1차 `bending` 값 |
| --- | --- | --- |
| 데이터 매핑 | `matnexus_test_type_keys`, `map_run`, `required_channels`, `required_dimensions` | §10.1 |
| 지그·경계조건 | 레시피가 담당([recipe.md](recipe.md)). TestSpec은 `engine_params`(mo_*)로 하중 경로 값만 공급 | 변위 제어, §10.4 |
| 허용 형상 | `allowed_geometries()` | `box`, `layered_box`, `stp` |
| 응답 정의 | `responses()` → `ResponseDef[]` (§3). 레시피 매니페스트 `responses`의 ID 정보를 받아 완성 | §10.3 |
| 해석 모드 | `analysis_mode()` | `quasi_static` (implicit 우선, 레시피 fallback explicit) |
| 검증 규칙 | `validate()` | §10.2 |

새 시험은 TestSpec 추가로만 대응하고 엔진은 수정하지 않는다. MatNexus `test_type_key`가 어떤 TestSpec의 `matnexus_test_type_keys`에도 없으면 잡 생성 400 `TEST_TYPE_UNMAPPED`.

## 2. 엔진 처리 흐름(시험 1건 기준)

```
RunRecord(SI) ─map_run→ TestData ─§5 1~6단계(시험측, 1회)→ 정렬된 시험 곡선 + 격자 + 구간(Ω1/Ω2)
                                                     │
θ → 해석 → Extracted ─§5 1·3·6단계(해석측, 매 평가)→ 격자 위 해석 곡선 → §8 J
```

시험측 전처리(정렬·구간·격자)는 잡 시작 시 1회 계산해 고정한다(평가마다 바뀌지 않음 → 재현성). 해석측은 같은 규칙을 매 평가에 적용한다.

## 3. 응답 정의 스키마 `ResponseDef`

YAML/JSON 형(레시피 매니페스트 `responses[]`와 TestSpec 결과가 공유):

```yaml
response_key: force_disp          # ^[a-z][a-z0-9_]{0,31}$, 시험 내 고유
kind: curve                       # curve | scalar
test_channel: force               # curve: 세로축 시험 채널 키(MatNexus 채널). scalar: 시험 스칼라 키
axis: displacement                # curve: 기준축 채널 키 displacement | deflection | time. scalar: null
extractor:                        # curve: {x, y}  /  scalar: {value}
  y: {name: rcforc_resultant, contact_id: 1, component: z, sign: -1, scale: symmetry}
  x: {name: nodout_disp, node: {ref: indenter_ref}, component: z, sign: -1}
weight: 1.0                       # 시험 내 응답 가중 w_i (>0)
window:
  x_min: null                     # 기준축 SI(m 또는 s). null=제한 없음
  x_max: null
  split: auto                     # auto(탄성/소성 분할) | none(전 구간 Ω1)
  region_weights: null            # null → objective.region_weights 사용. {elastic: w1, plastic: w2}
```

Python 형:

```python
@dataclass(frozen=True)
class ExtractorRef:
    name: str                          # §4 카탈로그
    args: Mapping[str, Any]            # 나머지 키

@dataclass(frozen=True)
class ResponseWindow:
    x_min: float | None
    x_max: float | None
    split: Literal["auto", "none"]
    region_weights: Mapping[str, float] | None

@dataclass(frozen=True)
class ResponseDef:
    response_key: str
    kind: Literal["curve", "scalar"]
    test_channel: str
    axis: Literal["displacement", "deflection", "time"] | None
    extractor: Mapping[str, ExtractorRef]   # curve: {"x","y"} / scalar: {"value"}
    weight: float
    window: ResponseWindow
```

## 4. 추출기 카탈로그

SolverAdapter `capabilities().extractors`가 지원 이름을 선언한다. 출력은 SI이며 부호(`sign`)·대칭 배율(`scale: symmetry` → 매니페스트 `symmetry.force_scale`)을 어댑터가 적용한다. 곡선의 x·y가 서로 다른 binout 블록에서 오면 어댑터가 공통 시간축으로 선형 보간해 짝짓는다.

| 이름 | 인자 | 출력 | 단계 |
| --- | --- | --- | --- |
| `rcforc_resultant` | `contact_id`(int), `side`(`master`\|`slave`, 기본 `master`), `component`(`x`\|`y`\|`z`\|`magnitude`), `sign`(±1), `scale`(`symmetry`\|number\|생략) | 시간별 힘 [N] | 1차 |
| `nodout_disp` | `node`(`{id: int}` 또는 `{ref: str}`), `component`, `sign` | 시간별 변위 [m] | 1차 |
| `nodout_rel_disp` | `node`, `ref_nodes`(목록, 평균), `component`, `sign` | 상대 변위 [m] (처짐계 대응) | 1차 |
| `time` | — | 시간 [s] | 1차(n차 기준축) |
| `field_max` | `quantity`(`eff_plastic_strain`), `per`(`slot`) | 슬롯별 최대값 → 결과 유효 범위 | 1차 |
| `glstat_ratio` | `numerator`(`kinetic`), `denominator`(`internal`) | 최대 비율 → explicit 준정적 검사 | 1차 |
| `nodout_radial_max` | `node_set`(ref), `axis` | 최대 직경 [m] (배럴링) | 2차 예약 |
| `rbdout_vel` | `part_id` | 충격체 속도 [m/s] | n차 예약 |

`node.ref`는 정의된 참조점 이름만 쓸 수 있다: 시편 = TestSpec `reference_points()`가 요청한 이름(bending: `midspan_bottom`), 지그 = 생성기 필수 참조 노드(`jig.bending3p_rollers`: `indenter_ref`, `support_top`). 고정 레시피는 참조 이름 대신 `node.id`(레시피 소유 노드)를 쓴다.

## 5. 정렬·재샘플링 8단계 (수치 규칙)

기호: 시험 점 (xᵢ, yᵢ), 해석 점 (xⱼ, yⱼ). `F_peak` = 하중 구간 내 y 최대값. 설정 키는 모두 `alignment.*`이며 응답 `window`가 일부를 덮을 수 있다.

| 단계 | 대상 | 규칙 | 설정 키(기본값) |
| --- | --- | --- | --- |
| 1 정규화 | 시험·해석 | (a) SI 확인(MatNexus 정본 단위, 해석은 어댑터가 SI). (b) NaN·inf 점 제거. (c) **시험측만** 컴플라이언스 보정(§7). (d) 단조 하중 구간: 앞에서부터 누적 최대 x를 갱신하는 점만 남기고, xₖ < max(x₀..xₖ₋₁) − `unload_tol_rel`·(x범위)인 첫 k에서 자른다(제하 구간 제외). (e) 중복 x: \|Δx\| ≤ `dedup_tol_rel`·(x범위)인 연속 점은 y 평균 한 점으로 → x 엄격 증가 | `unload_tol_rel`(0.005), `dedup_tol_rel`(1e-9), `min_raw_points`(20), `min_loading_fraction`(0.3) |
| 2 기준축 | 응답별 | `ResponseDef.axis`. 준정적은 `displacement`/`deflection`, 고속은 `time` | — |
| 3 원점 정렬 (토우 맞춤 구간은 G4 조정 항목) | 시험·해석 같은 규칙 | 접촉 시작 i_c = yᵢ ≥ `contact_force_ratio`·F_peak인 첫 i. 준정적이고 `toe_correction=true`면: y ∈ [`toe_fit_lo`, `toe_fit_hi`]·F_peak 이고 피크 이전인 점으로 y = k₀x + b 최소제곱 → x₀ = −b/k₀. 아니면 x₀ = x_{i_c}. x ← x − x₀, x<0 점 제거, (0, 0) 앞에 추가. 맞춤 점 < `toe_fit_min_points`이면 `CURVE_NO_ELASTIC_REGION`. 고속(n차): t₀ = t_{i_c}, 선택적으로 보조 변수 `time_shift` ∈ ±`time_shift_bounds_s` | `contact_force_ratio`(0.02), `toe_correction`(true), `toe_fit_lo`(0.10), `toe_fit_hi`(0.40), `toe_fit_min_points`(5), `time_shift_bounds_s`(n차, null) |
| 4 비교 구간 | 시험 기준 | 파손 절단: 피크 인덱스 p 이후 y_q < (1 − `failure_drop_ratio`)·F_peak인 첫 q → x_fail = x_{q−1}(없으면 x_max). 목표 구간 X = [X_l, X_u], X_l = max(0, user_window.x_min, window.x_min), X_u = min(x_exp_max, x_fail, user_window.x_max, window.x_max). 해석 범위는 매 평가에서 교집합: 유효 상한 U = min(X_u, x_sim_max) | `failure_cut`(true), `failure_drop_ratio`(0.3) |
| 5 공통 격자 | 시험 기준(1회) | 구간 분할(§5.1)로 경계 x_e. N1 = max(`elastic_min_points`, round(N·(x_e−X_l)/(X_u−X_l))), N2 = max(N − N1, `plastic_min_points`) (Ω2가 비면 0). Ω1 격자 = linspace(X_l, x_e, N1), Ω2 격자 = linspace(x_e, X_u, N2+1)[1:]. 원본 샘플 밀도와 무관 | `grid_points` N(200), `elastic_min_points`(30), `plastic_min_points`(30) |
| 6 보간·필터 | 시험·해석 같은 것 | 필터를 먼저 같은 방식으로 적용한 뒤 격자에 선형 보간(`numpy.interp` 동치, 외삽 금지 → 해석 범위 밖 격자점은 비교 제외). 필터: `none`; `moving_average`(원 데이터를 `filter.presample_points` 균일 점으로 선형 재표본 후 창 `filter.window` 점 이동평균); `cfc`(시간축, SAE J211 CFC `filter.cfc_class`) | `filter.kind`(준정적 `none`, 고속 `cfc`), `filter.presample_points`(2000), `filter.window`(11), `filter.cfc_class`(60) |
| 7 해석 출력 간격 | 덱 생성(**엔진 소관**) | 엔진이 TestSpec `engine_params()`의 mo_dend·mo_tend를 받은 뒤 계산한다. Δx_min = 케이스의 모든 run·응답 격자 간격 최소값. 변위 기준축: v = mo_dend/mo_tend, `dt_out` = Δx_min/(v·k). 시간 기준축: `dt_out` = Δt_min/k. `dt_plot` = mo_tend/`solver.d3plot_frames`. implicit 최대 시간 증분 `dt_max` = `dt_out`(≤ dt_out 보장, 출력 시점을 건너뛰지 않게). 예약 파라미터 `mo_dtout`, `mo_dtplt`, `mo_dtmax`로 기록([recipe.md §3.2](recipe.md)) | `output_oversample` k(4), `solver.d3plot_frames`(20) |
| 8 범위 부족 | 해석 | 미도달 비율 s = max(0, (X_u − x_sim_max)/(X_u − X_l)). J에 `objective.shortfall_penalty`·s 가산(§8). s>0이면 그 평가는 `penalized` | `objective.shortfall_penalty`(1.0) |

### 5.1 탄성/소성 구간 분할 (5단계 보조 규칙)

- `window.split = none` 또는 잡에 plastic 단계 변수가 하나도 없으면(예: 전 슬롯 `mat001`): Ω1 = X 전체, Ω2 = ∅.
- `auto`: 3단계 k₀(토우 보정 없으면 같은 맞춤 구간으로 계산)를 써서 x_e = { x > x(`toe_fit_hi`·F_peak) 이고 y(x) < (1 − `elastic_dev_tol`)·k₀·x 인 최소 x }. 없으면 x_e = X_u(전 구간 탄성). x_e는 [X_l, X_u]로 자른다.
- 설정 키: `alignment.elastic_dev_tol`(0.05), `alignment.elastic_min_raw_points`(5).

## 6. 변위 채널 선택 (굽힘)

| `displacement_channel` | 규칙 |
| --- | --- |
| `auto` (기본) | **크로스헤드(`displacement`) 기본**(결정 2026-11-08). `testspec.bending.prefer_deflection`(기본 false)을 켜고 `deflection` 채널이 있을 때만 처짐계. `displacement`가 없고 `deflection`만 있으면 처짐계(경고 없이) |
| `displacement` | 크로스헤드 사용, 컴플라이언스 보정 적용 |
| `deflection` | 처짐계 사용, 보정 생략. 채널 없으면 400 `DISPLACEMENT_CHANNEL_UNAVAILABLE` |

처짐계를 쓰면 응답의 `axis = deflection`이고 해석측 x 추출기는 매니페스트 `responses[].alt_axes.deflection`(보통 `nodout_rel_disp`: 시편 중앙 하면 노드 − 지지점 기준)을 쓴다. 매니페스트에 `alt_axes.deflection`이 없으면 400 `DISPLACEMENT_CHANNEL_UNAVAILABLE`.

## 7. 컴플라이언스 보정

```
δ_spec(i) = δ_crosshead(i) − C · F(i)        [m] = [m] − [m/N]·[N]
```

| 항목 | 규칙 |
| --- | --- |
| C 출처 | 레시피 매니페스트 `compliance.value_m_per_n`(장비+지그 교정값). **식별 변수가 아니다**(E와 비식별) |
| 적용 주체 | **엔진 정렬 1단계 (c)에서만** 적용(원점 정렬 전). `TestSpec.map_run`은 C를 `TestData.compliance_m_per_n`에 실어 전달만 하고 x를 바꾸지 않는다. 이중 적용 금지(계약 테스트 T6이 map_run 결과의 x가 원 채널과 같음을 검사). 해석측에는 적용하지 않음(지그 강체) |
| C 없음 | 보정 없이 실행, 경고 `COMPLIANCE_MISSING`("E 신뢰도 낮음"), 결과 `metadata.compliance.applied=false` |
| 처짐계 | 보정 생략, `compliance.applied=false`, `reason="deflectometer"` |
| 장비 불일치 | 매니페스트 `compliance.instruments`가 있고 run의 `instrument`가 목록에 없으면 경고 `COMPLIANCE_INSTRUMENT_MISMATCH`(보정은 적용) |
| 보정 후 검사 | 보정된 δ가 하중 구간에서 단조가 아니게 되면(C 과대) 경고 `COMPLIANCE_NONMONOTONIC` 후 1단계 (d) 규칙대로 처리 |

## 8. 목적함수와 다중 시험 결합

run r, 응답 i(곡선), 구간 k ∈ {1: 탄성 Ω1, 2: 소성 Ω2}, 격자점 g. G_k = Ω_k 격자점 전체, G_k^sim = 그중 해석 범위 안(6단계).

```
e_{r,i,k} = sqrt( Σ_{g∈G_k^sim} (F_sim(g) − F_exp(g))² ) / sqrt( Σ_{g∈G_k} F_exp(g)² )      (Ω_k=∅이면 0)

J_{r,i} = Σ_k w_k · e_{r,i,k}  +  λ_s · s_{r}                                              (plan 식 + 8단계)
          w_k = window.region_weights 또는 objective.region_weights {elastic: 1.0, plastic: 1.0}
          λ_s = objective.shortfall_penalty

스칼라 응답:  J_{r,i} = |s_sim − s_exp| / max(|s_exp|, objective.scalar_floor)

시험 t:       J_t = Σ_i w_i · mean_r(J_{r,i}) / Σ_i w_i
잡 전체:      J   = Σ_t W_t · J_t / Σ_t W_t          (W_t = tests[].weight)
```

| 규칙 | 내용 | 설정 키(기본) |
| --- | --- | --- |
| 탄성 단계 | w_plastic = 0 으로 계산(Ω1만). **해석은 탄성 전용 카드로 한다**: identify 슬롯의 `NeutralMaterial`에서 `table`을 제거해(MAT_001 등가) 넘긴다 — σy 초기값이 탄성 구간 곡선을 왜곡해 E가 편향되는 것을 막기 위함. fixed 슬롯은 기준 카드 그대로 | — |
| 소성 단계 | 위 식 그대로(E 고정) | — |
| 해석 실패 | 곡선을 못 얻으면 J_{r,i} = `objective.failed_run_J`. 부분 곡선이 있으면 위 식 + 범위 부족 페널티 | `objective.failed_run_J`(10.0) |
| 시간 초과 | 실패와 같음(부분 곡선 사용) | 요청 `budget.run_timeout_s`, 기본 `solver.run_timeout_s` |
| 케이스 공유 | 같은 케이스(§9.1)의 run들은 하나의 해석 곡선과 각자 비교 | — |
| 스칼라 하한 | 0 나눗셈 방지 | `objective.scalar_floor`(1e-12) |

## 9. 케이스·식별성

### 9.1 시뮬레이션 케이스

시험 t의 run들은 해석 입력(노출 파라미터 + 형상 치수)의 상대 차이가 모두 ≤ `cases.dimension_merge_rel_tol`(0.01)이면 평균 치수 케이스 하나로 묶고, 아니면 차이가 허용 이내인 무리별로 케이스를 나눈다(그리디, run 순서). 케이스 ID `"{test_key}-c{n}"`(n=1부터). 평가 1회 = 모든 케이스 해석 1회씩(순차).

### 9.2 사전 점검(잡 생성 시)

| 항목 | 규칙 | 결과 |
| --- | --- | --- |
| 탄성 변수 대 시험 구성 수 | n_E = identify 슬롯 E 변수 수, n_cfg = 서로 다른 시험 구성 수(레시피·노출 파라미터·형상·`stack_axis` 해시가 다른 `tests[]` 원소 수). n_E > n_cfg | `identifiability.precheck_mode`=`reject`(기본) → 400 `IDENTIFIABILITY_UNDERDETERMINED`; `warn` → 경고 |
| 소성 변수 대 데이터 | n_p > (Σ 모든 run의 Ω2 격자점) / `identifiability.min_points_per_param`(5) | 경고 `IDENTIFIABILITY_LOW_DATA` |

### 9.3 사후 진단(`phase=diagnostics`)

| 항목 | 규칙 | 설정 키(기본) |
| --- | --- | --- |
| 실행 | `identifiability.post_fit`(true)일 때, 각 단계 최종 θ*에서 식별 변수마다 전진 차분 1회(상대 스텝 h; log 변수는 곱셈 스텝). 평가는 history `stage=diagnostics`, `budget.max_evaluations`에 넣지 않음 | `post_fit`(true), `fd_step_rel` h(0.02), `max_extra_evals`(30; 초과 시 생략 + 경고 `DIAGNOSTICS_SKIPPED`) |
| 잔차 벡터 | 모든 run·응답·구간 격자점의 √w_k·(F_sim−F_exp)/‖F_exp‖_{Ω_k} 연결 | — |
| 민감도 | 상대 야코비안 S_{gj} = ∂r_g/∂ln θ_j. 정규화 민감도 ‖S_j‖₂ | — |
| 상관 | cov = σ²(SᵀS)⁻¹, σ² = ‖r‖²/(n_res − n_p). 상관행렬 = cov_{ij}/√(cov_ii cov_jj). 상대 표준편차 = √cov_jj. **이 상대 표준편차는 하한 추정이다**: 격자점 잔차를 독립으로 가정하지만 같은 곡선의 이웃 격자점은 강하게 상관돼 실제 자유도가 n_res보다 훨씬 작다. 결과에 `relative_stdev_is_lower_bound: true`로 표기. 상관행렬·민감도·조건수(σ²와 무관)는 그대로 유효 | — |
| 플래그 | \|corr\| > `corr_threshold` → `HIGH_CORRELATION`(민감도 낮은 쪽 고정 권고). ‖S_j‖ < `min_sensitivity` → `INSENSITIVE_PARAM`(고정 권고). cond(SᵀS) > `max_condition` → `ILL_CONDITIONED` | `corr_threshold`(0.95), `min_sensitivity`(1e-3), `max_condition`(1e8) |

결과 형식은 [result.md §4.5](result.md).

## 10. 3점굽힘 TestSpec `bending` (1차 구현 대상)

### 10.1 데이터 매핑

| MatNexus | 내부 | 규칙 |
| --- | --- | --- |
| `test_type_key = flexural` | `test_type = bending` | `matnexus_test_type_keys = ("flexural",)` |
| 채널 `force` [N] | y | 필수 |
| 채널 `displacement` [m] (크로스헤드) | x 후보 | §6 |
| 채널 `deflection` [m] (처짐계) | x 후보 | §6 |
| 채널 `time` [s] | 기록용 | 1차 미사용 |
| 시편 치수 `width`, `thickness` | 형상 `dimensions` | 필수(`SPECIMEN_DIMENSION_MISSING`). `measured` 우선, 없으면 `nominal` |
| 시편 치수 `span` | 레시피 파라미터 `span` (매니페스트 `matnexus_key: span`) | 필수(시편 또는 `recipe_params`) |
| 시편 치수 `load_span` | — | 값이 있으면 4점 → 400 `TEST_VARIANT_UNSUPPORTED`(확장 지점) |
| 시편 치수 `total_length` | 형상 `dimensions.total_length` | 없으면 span + 2·`overhang`(레시피 파라미터) |
| 조건 `temperature`, `test_speed` | 결과 메타 기록 | 식별에 미사용(속도 무관 가정) |
| `instrument` | 컴플라이언스 장비 대조 | §7 |

`required_channels() = (("force",), ("displacement", "deflection"))`, `required_dimensions() = ("width", "thickness", "span")`.

### 10.2 검증 규칙

| 검사 | 코드 |
| --- | --- |
| 채널·치수 누락 | `CHANNEL_MISSING`, `SPECIMEN_DIMENSION_MISSING` |
| 4점(`load_span`) | `TEST_VARIANT_UNSUPPORTED` |
| 형상 kind ∉ {box, layered_box, stp} | `GEOMETRY_KIND_NOT_ALLOWED` |
| span ≤ 0 또는 span ≥ total_length | `GEOMETRY_DIMENSION_INVALID` |
| span/thickness < `testspec.bending.min_span_ratio`(4) | `GEOMETRY_DIMENSION_INVALID` |
| span/thickness < `testspec.bending.beam_theory_span_ratio`(10) | 경고 `BEAM_THEORY_APPROXIMATE`(초기값 정확도 저하) |
| 곡선 검사 | 5단계 규칙 실패 코드(`CURVE_*`) |

### 10.3 응답 정의(1차)

| response_key | kind | test_channel | axis | extractor.y | extractor.x | weight | window |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `force_disp` | curve | `force` | `displacement` 또는 `deflection`(§6) | `rcforc_resultant`(매니페스트 `contact_id`, `component: z`, `sign: -1`, `scale: symmetry`) | 크로스헤드: `nodout_disp`(`node.ref: indenter_ref` 또는 매니페스트 ID, `sign: -1`) / 처짐계: 매니페스트 `alt_axes.deflection` | 1.0 | `split: auto` |

`reference_points()`: `midspan_bottom` = {x: 0, y: 0(대칭이면 대칭면), z: "min"} — 처짐계 대응 노드. 처짐계 기준점 `support_top`(지지 롤러 강체 노드)은 지그 생성기가 제공.

`param_space()`(비재료 파라미터, [plugins.md §3 R9](plugins.md)): 1차 bending은 빈 공간. 2차 `compression`은 `mu`(→ 레시피 `fric`), n차 고속은 `time_shift`(→ 정렬 3단계)를 낸다.

### 10.4 엔진 파라미터 `engine_params()` (예약 `mo_*`, SI → 덱 단위 변환은 어댑터)

| 이름 | 값 | 단위(SI) |
| --- | --- | --- |
| `mo_dend` | X_u · (1 + `testspec.bending.overtravel_ratio`(0.05)) | m |
| `mo_tend` | implicit: `solver.implicit.t_end_s`(1.0) / explicit: mo_dend / `solver.explicit.loading_velocity_m_s`(0.5) | s |
| `mo_htop` | `MeshResult.top_z_at_load_m` | m |
| `mo_hbot` | `MeshResult.bottom_z_m` | m |

`engine_params()`는 위 네 값만 돌려준다. `mo_dtout`, `mo_dtplt`, `mo_dtmax`는 엔진이 5단계 7로 계산해 덧붙인다(TestSpec 소관 아님).

### 10.5 초기값 `initial_hints()` (파라메트릭 형상만, STP는 빈 매핑 → 범위 기본값·FE 1D 탐색)

| 키 | 식 | 비고 |
| --- | --- | --- |
| `E0` | k₀·L³ / (4·b·h³) | 보 이론(48EI, I=bh³/12). k₀ = 3단계 기울기(보정 후). layered_box는 전 단면 등가 E로 슬롯 공통 초기값 |
| `sigma_y0` | 3·F(x_e)·L / (2·b·h²) | 외층 응력 |
| `eps_p_max0` | max(6·h·X_u/L² − sigma_y0/E0, `testspec.bending.min_eps_p0`(0.01)) | 테이블 노드 상한 참고 |

## 11. 확장 지점 (계약만, 구현 안 함)

| 단계 | TestSpec | 추가될 것 | 기존 계약에 미치는 영향 |
| --- | --- | --- | --- |
| 2차 | `compression` (MatNexus `compression`) | 치수 키 `height`, `diameter`, `width`, `thickness`(MatNexus 「압축」 칸); SW 정의 키 `top_diameter`, `bottom_diameter`(원뿔대); 형상 `cylinder`, `frustum`, `axisym`; 스칼라 응답 `barrel_diameter`(추출기 `nodout_radial_max`); 마찰 식별 단계 `friction`(시작 시 μ=0, 0.3 민감도 체크, `testspec.compression.mu_sensitivity_tol`); 변형 채널 `strain`(압축계) | enum 값·선택 필드 추가뿐(MINOR) |
| 2차 | `bending` 4점 | `load_span` 허용, 레시피 jig 생성기 `jig.bending4p_rollers` | `TEST_VARIANT_UNSUPPORTED` 해제 |
| n차 | `*_dynamic`(고속) | `axis=time`, 필터 `cfc`, `time_shift` 보조 변수, `analysis_mode=dynamic`(공칭 변형률속도 > `testspec.dynamic.rate_threshold_1_s`이면 explicit), 질량 스케일링 금지(`glstat_ratio` ≤ `solver.explicit.max_ke_ratio`), 경계조건 초기속도·질량(`mo_v0`, `mo_mass` 예약), 유사도 옵션 `objective.similarity: l2 | iso18571`, 재료 단계 `rate`(Cowper–Symonds `C`,`p` 또는 속도별 테이블) → `rate_table` 블록 | 모두 추가 |
| n차 | `generic` | 사용자 지그 덱(레시피 고정형)과 응답 정의만으로 실행. 매핑은 레시피 매니페스트 `test_type` + `responses` | 없음 |

## 12. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| T-1 | 정렬 기본값(접촉 2%, 토우 맞춤 10–40%, 파손 30% 급락, N=200, 탄성 최소 30점, k=4)은 plan에 없어 새로 정함. 실시험 적용(G4) 후 조정 | 가정 |
| T-2 | (결정 반영) `auto` 변위 채널은 크로스헤드 기본, 처짐계는 `testspec.bending.prefer_deflection=true`로 켜는 옵션 | 확정 |
| T-7 | 토우 맞춤 구간(`toe_fit_lo/hi` = 10–40%)은 실시험 적용 G4에서 조정할 항목 | 가정(G4 조정) |
| T-3 | 비교 구간 상한은 시험 기준으로 고정하고 해석 범위 부족은 8단계 페널티로만 반영(구간이 θ마다 바뀌지 않게) | 가정 |
| T-4 | 다중 run 결합은 run별 J의 산술평균(평균 곡선을 만들지 않음) — run별 잔차·시편별 통계를 남기기 위함 | 가정 |
| T-5 | 식별성 사전 점검 실패 시 거절/경고 — 사용자 결정 대기. 현재 기본값 `reject` 유지(설정 `identifiability.precheck_mode`) | 미결 |
| T-6 | 사후 진단 차분 평가는 예산(`max_evaluations`) 밖 별도 상한 | 가정 |
| U-1 | 성공 기준 오차 임계값 미정 — 결과의 `metadata.residuals`로 판정 가능하도록만 계약 | 미결 |
