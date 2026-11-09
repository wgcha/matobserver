schema_version: 1.3 · 상태: 승인(G1, MINOR 1.3) · 2026-11-09

# 레시피 계약 (지그·경계조건)

근거: plan.md 「레시피(지그·경계조건) 구조」, 「변위 채널과 컴플라이언스 보정」, 「해석 실행 전략」, decisions.md(2026-11-08 레시피 불변·관리자 전용·폴더+CLI+관리자 토큰 API, C 레시피 등록).

엔진은 항상 **레시피 + 파라미터 → 덱**만 처리한다. 파라미터형 템플릿도 레시피의 한 종류다. 지그가 바뀌면 레시피를 새로 등록하고 엔진 코드는 고치지 않는다.

## 1. 레시피 종류

| 구분 | 파라미터형 (`kind: parametric`) | 고정 (`kind: fixed`) |
| --- | --- | --- |
| 대상 | 3점굽힘 표준 지그(ASTM D790, ISO 178 등), 압축판(2차) | 사내 전용 지그, 복잡한 치구, 임의 시험 |
| 지그 메시 | matobserver가 치수로 생성(`jig.generator`, GeometryMesher `role=jig`) → `mo_jig.k` | 관리자가 덱에 포함 |
| 시편 메시 | matobserver 생성 → `mo_specimen.k` | 같음 |
| 가변값 | 스팬, 압자 R, 지지 R, 시편 L·W·T(형상), 오버행, 속도, 마찰 — 모두 매니페스트 `parameters`로 노출 | 덱이 `*PARAMETER`로 참조하고 매니페스트 `parameters`에 선언한 값만 |
| 재료 | 슬롯별 `mo_mat_<slot>.k` `*INCLUDE` | 같음(슬롯별 part ID 지정) |
| 하중 경로·출력 | `mo_params.k`(mo_*), `mo_output.k` | 같음 |

## 2. 폴더 구조

```
<recipe_folder>/                      # CLI 입력, zip 번들의 최상위
├── manifest.yaml                     # 필수, §4
├── deck/
│   ├── main.k                        # 필수, 진입 덱 (§3 규약)
│   ├── controls_implicit.k           # analysis.primary/fallback에 쓰는 모드별 제어 카드
│   ├── controls_explicit.k           # fallback=explicit이면 필수
│   └── *.k                           # 지그·접촉 등 레시피 소유 include (fixed: 지그 메시 포함)
├── verification/
│   ├── reference_card.yaml           # 필수: 기준 카드(NeutralMaterial card_blocks, SI) — 슬롯별
│   └── reference_curve.csv           # 선택: 기대 F-δ (x[m], y[N]) — 있으면 dry-run 비교
└── README.md                         # 선택, 사람용
```

- 모든 경로는 폴더 기준 상대 경로, `..`·절대 경로·심볼릭 링크 금지(`RECIPE_PATH_UNSAFE`).
- 등록 후 저장 위치: 업로드 시도마다 `recipes.store_dir/<recipe_id>/<version>/<verification_id>/` (읽기 전용 권한, 덮어쓰기 금지). `rejected` 뒤 같은 버전을 다시 올리면 새 `verification_id` 하위 경로에 저장되고, `active`가 된 시도 하나를 JobStore 레코드가 가리킨다(`ResolvedRecipe.store_dir`).
- 번들 해제 상한: 압축 해제 총크기 ≤ `recipes.max_unpacked_bytes`(기본 200 MiB), 파일 수 ≤ `recipes.max_files`(기본 500). 해제 중 초과하면 중단하고 `RECIPE_BUNDLE_INVALID`.
- 텍스트 파일은 UTF-8 또는 ASCII, 줄바꿈 LF 또는 CRLF(해시는 원본 바이트 기준).

## 3. 덱 규약 (`deck/main.k`)

### 3.1 예약 include 파일

엔진(SolverAdapter의 덱 조립기)이 실행 폴더에 생성하는 파일. `main.k`는 아래 이름을 **그대로** `*INCLUDE` 한다(경로 없이 파일명만). 레시피 폴더에 같은 이름 파일이 있으면 거절(`RECIPE_RESERVED_FILE_PRESENT`).

| 파일 | 필수 | 내용 | 위치 규칙 |
| --- | --- | --- | --- |
| `mo_params.k` | 예 | `*PARAMETER`: 노출 파라미터(덱 단위 환산값) + 엔진 예약 파라미터(§3.2) | `*KEYWORD` 직후 첫 `*INCLUDE` |
| `mo_specimen.k` | 예 | 시편 `*NODE`, `*ELEMENT_*`, `*SET_NODE_LIST`(대칭면·참조점), `*SET_PART_LIST`(id=`specimen.part_set_id`) — [module-boundaries.md §2](module-boundaries.md) | 아무 곳 |
| `mo_mat_<slot>.k` | 매니페스트 슬롯마다 예 | 슬롯의 `*PART`(part_ids), `*SECTION_*`(secid), `*MAT_*`(mid), MAT_024면 `*DEFINE_CURVE`(lcid) | 아무 곳 |
| `mo_jig.k` | parametric 예 / fixed 금지 | 생성 지그 강체 메시(노드·요소, `*PART`·`*MAT_RIGID`·`*SECTION_SHELL` 포함) | 아무 곳 |
| `mo_controls.k` | 예 | 선택된 해석 모드의 `deck.controls[<mode>]` 파일 사본 | 아무 곳 |
| `mo_output.k` | 예 | `*DATABASE_RCFORC`, `*DATABASE_NODOUT`, `*DATABASE_GLSTAT`, `*DATABASE_MATSUM`, `*DATABASE_BINARY_D3PLOT`, `*DATABASE_HISTORY_NODE`(응답·참조 노드), `*DATABASE_EXTENT_BINARY`(소성변형률 출력) | 아무 곳 |

### 3.2 `*PARAMETER` 치환 규칙

- 매니페스트 `parameters[].name`과 예약 이름은 **덱 어디에서도 `*PARAMETER`/`*PARAMETER_EXPRESSION`로 다시 정의하지 않는다**(`RECIPE_RESERVED_PARAM_REDEFINED`). 값은 `mo_params.k`에서만 온다. 덱은 `&name`으로 참조한다.
- 이름 규칙: `^[a-z][a-z0-9_]{0,7}$`(LS-DYNA 고정 폭 PRMR 칸 안전 길이). `mo_` 접두사는 엔진 예약.
- 값 결정 우선순위: 요청 `tests[].recipe_params` > MatNexus 시편 치수(`matnexus_key`) > 매니페스트 `default`. 범위 밖이면 400 `RECIPE_PARAM_OUT_OF_RANGE`.
- 환산: 매니페스트 SI 값 → 덱 단위(mm·N·s·tonne)는 `dimension`으로 결정(length: ×1e3, velocity: ×1e3 mm/s, stress: ×1e-6 MPa, density: ×1e-12 tonne/mm³, force·time·dimensionless: ×1).
- 형식: `mo_params.k`는 쉼표 구분 자유 형식 `R name,value` 한 줄씩, 값은 `%.10e`.

엔진 예약 파라미터(1차, 값은 [testspec.md §10.4](testspec.md)):

| 이름 | 타입 | 덱 단위 | 의미 |
| --- | --- | --- | --- |
| `mo_dend` | R | mm | 압자 최종 변위(하중 경로 곡선 끝값) |
| `mo_tend` | R | s | 종료 시간 |
| `mo_dtout` | R | s | binout 출력 간격(정렬 7단계) |
| `mo_dtplt` | R | s | d3plot 간격 |
| `mo_dtmax` | R | s | implicit 최대 시간 증분(= `mo_dtout`, ≤ dt_out). 엔진 계산 |
| `mo_htop` | R | mm | 하중선 위치 시편 윗면 z |
| `mo_hbot` | R | mm | 시편 아랫면 z |
| (n차 예약) `mo_v0`, `mo_mass` | R | mm/s, tonne | 초기속도·충격체 질량 |

`mo_dend`·`mo_tend`·`mo_htop`·`mo_hbot`은 TestSpec `engine_params()`가, `mo_dtout`·`mo_dtplt`·`mo_dtmax`는 엔진(정렬 7단계)이 계산한다.

**implicit 제어 강제**: `deck.controls.implicit` 파일은 `*CONTROL_IMPLICIT_AUTO`를 포함하고 DTMAX 칸에 `&mo_dtmax`를 써야 한다(출력 시점을 건너뛰는 큰 증분 방지). `*CONTROL_IMPLICIT_GENERAL`의 DT0이 상수면 `mo_dtmax` 이하인지 dry-run에서 확인한다. 위반은 정적 검사 `RECIPE_IMPLICIT_DTMAX_MISSING`.

덱은 하중 경로를 `&mo_dend`, `&mo_tend`로 정의한다(예: `*DEFINE_CURVE` 점 (0,0), (&mo_tend, &mo_dend) + `*BOUNDARY_PRESCRIBED_MOTION_RIGID`). 고정 레시피는 압자 초기 위치를 `&mo_htop`으로 맞춘다(초기 간격 0).

### 3.3 출력 간격 덮어쓰기

- 레시피 파일에는 `*DATABASE_RCFORC`, `*DATABASE_NODOUT`, `*DATABASE_GLSTAT`, `*DATABASE_MATSUM`, `*DATABASE_BINARY_D3PLOT`, `*DATABASE_HISTORY_NODE*`를 두지 않는다(`RECIPE_OUTPUT_KEYWORD_PRESENT`). 엔진이 `mo_output.k`에 정렬 7단계 간격(`mo_dtout`, `mo_dtplt`)으로 쓴다.
- 그 밖의 `*DATABASE_*`(예: SECFORC)는 허용하되 간격은 `&mo_dtout` 사용을 권장(경고 `RECIPE_OUTPUT_INTERVAL_HARDCODED`).
- 솔버 노이즈 억제용 출력 필터·시간 스케일링 설정은 레시피 제어 파일(`controls_*.k`)에 고정한다(plan 방법론).

## 4. 매니페스트 `manifest.yaml` 스키마

### 4.1 최상위

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | --- | --- |
| `schema_version` | string | 예 | `"1.0"` |
| `recipe_id` | string `^[a-z0-9][a-z0-9-]{2,63}$` | 예 | |
| `version` | int ≥1 | 예 | 같은 id 안에서 증가 권장(강제는 아님) |
| `kind` | `parametric` \| `fixed` | 예 | |
| `test_type` | string | 예 | TestSpec 레지스트리 키(`bending`) |
| `title` | string | 예 | |
| `description` | string | 아니오 | |
| `standards` | string[] | 아니오 | 예: `["ASTM D790-17", "ISO 178:2019"]` |
| `unit_system` | `mm-N-s-tonne` | 예 | 1.0은 이 값만(`RECIPE_UNIT_SYSTEM_UNSUPPORTED`) |
| `solver` | object | 예 | §4.2 |
| `deck` | object | 예 | §4.2 |
| `symmetry` | object | 예 | §4.2 |
| `specimen` | object | 예 | §4.3 |
| `jig` | object | parametric 예 | §4.5 |
| `material_slots` | list | 예(≥1) | §4.4 |
| `parameters` | list | 예(빈 목록 허용) | §4.6 |
| `responses` | list | 예(≥1) | §4.7 |
| `compliance` | object \| null | 아니오 | §4.8 |
| `verification` | object | 예 | §4.9 |
| `owner` | string | 아니오 | 담당 부서·사람 |

### 4.2 `solver`, `deck`, `symmetry`

```yaml
solver:
  name: lsdyna                       # SolverAdapter 레지스트리 이름
  min_version: "R12.0"               # 선택. 실행 버전이 낮으면 잡 400 SOLVER_ANALYSIS_UNSUPPORTED
  analysis:
    primary: implicit                # implicit | explicit
    fallback: explicit               # explicit | none  (implicit 수렴 실패 시 자동 전환)
deck:
  main: deck/main.k
  controls:
    implicit: deck/controls_implicit.k
    explicit: deck/controls_explicit.k
  files: [deck/contact.k]            # main.k가 include하는 레시피 소유 파일 전체(해시·검사 대상)
symmetry:
  type: quarter                      # none | half | quarter
  planes: [x0, y0]                   # 대칭면: x0 = (x=0 평면), y0 = (y=0 평면)
  force_scale: 4.0                   # 반력 배율(none=1, half=2, quarter=4)
```

### 4.3 `specimen` — 시편 슬롯과 배치 규약

```yaml
specimen:
  geometry_kinds: [box, layered_box, stp]   # TestSpec allowed_geometries의 부분집합
  element:
    topology: hex8                   # hex8 | tet4 | tet10 (stp 기본 tet10). 2차: quad4_axisym
    target_size_m: 0.0005            # 권장 시편 요소 크기
    min_elems_through_thickness: 4
    stp_topology: tet10
  part_set_id: 900                   # mo_specimen.k의 *SET_PART_LIST ID (덱 접촉이 참조)
  node_sets:                         # 대칭면 노드 세트 ID (덱 *BOUNDARY_SPC_SET이 참조)
    sym_x0: 901
    sym_y0: 902
  id_ranges:                         # 엔진 생성 시편 노드·요소 ID 범위
    node: [1, 4999999]
    element: [1, 4999999]
  placement:                         # 배치 규약(굽힘)
    length_axis: x                   # 시편 길이(스팬) 방향
    width_axis: y
    load_axis: -z                    # 압자 진행 방향
    origin: midspan_bottom           # 원점 = 스팬 중앙, 시편 아랫면(지지 롤러 접선 z=0)
    initial_gap: 0.0                 # 지지·압자와 시편 초기 간격 [m]
```

배치 규약(굽힘, 전 형상 공통):

| 항목 | 규칙 |
| --- | --- |
| 좌표 | x=길이(스팬 방향), y=폭, z=하중축(압자는 −z로 이동) |
| 원점 | 스팬 중앙(x=0), 폭 중앙(y=0), 시편 아랫면 z=0 |
| 지지 롤러 | 윗면 접선이 z=0, 중심 x=±span/2 |
| 압자 | 아랫면 접선이 z=`mo_htop`, 중심 x=0 |
| 초기 간격 | 0 (관통 검사 대상) |
| 대칭 | quarter: x≥0, y≥0 영역만 메시. half: y≥0 |
| STP `placement.mode=auto` | 바운딩 박스 기준: x·y 중심을 원점으로, 최저 z를 0으로 이동. 회전 없음(STP의 길이 방향이 x여야 함) |
| STP `placement.mode=explicit` | 요청 `rotate_deg`(x→y→z 순 고정축 회전) 후 `translate` 적용, 그 뒤 최저 z가 0이 아니면 400 `STP_CHECK_FAILED` |
| STP 단위 | STEP 파일 선언 단위를 읽어 m로 변환. 1차 STP는 대칭 미적용(`symmetry.type`과 무관하게 none으로 메시, 경고 `SYMMETRY_IGNORED_STP`) |

### 4.4 `material_slots`

```yaml
material_slots:
  - slot: body                       # ^[a-z][a-z0-9_]{0,15}$, 요청 materials.slots[].slot과 대응
    regions: [specimen]              # 메시 영역: specimen | layer:<i> | body:<i> | "*"(나머지 전부)
    part_id: 1                       # 이 슬롯 요소의 PID (mo_mat_<slot>.k가 *PART 정의)
    mid: 101
    secid: 101
    lcid: 1101                       # MAT_024 응력-변형률 곡선 ID (mat001이면 미사용)
    section:
      keyword: SECTION_SOLID         # SECTION_SOLID | (2차) SECTION_SHELL
      elform: -2                     # hex8: -2 | -1 | 1 | 2 ; tet10: 16 | 17 ; tet4: 10 | 13
```

| 규칙 | 내용 |
| --- | --- |
| 영역 매핑 | 형상 영역 → 슬롯: 매니페스트 `regions`가 기본. 요청 `geometry.layers[].slot`/`body_slots`가 있으면 그것이 우선(단, 슬롯은 매니페스트에 있어야 함) |
| ID 소유 | `part_id`, `mid`, `secid`, `lcid`는 엔진 생성 파일만 정의. 레시피 덱이 같은 ID를 정의하면 `RECIPE_SLOT_ID_CONFLICT` |
| 접촉 참조 | 덱 접촉은 `specimen.part_set_id`(시편 전체) 또는 슬롯 `part_id`를 참조 |
| 요소 호환 | `topology`–`elform` 조합이 표 밖이면 `RECIPE_SECTION_INVALID` |

### 4.5 `jig` (parametric)

```yaml
jig:
  generator: jig.bending3p_rollers   # GeometryMesher(role=jig) 레지스트리 이름
  part_ids: {indenter: 1001, support: 1002}   # 생성 강체 PID (mo_jig.k가 정의)
  mids: {indenter: 1001, support: 1002}       # *MAT_RIGID ID
  id_ranges: {node: [5000000, 5999999], element: [5000000, 5999999]}
  params:                            # 생성기 입력 ↔ parameters 이름
    span: span
    indenter_radius: r_ind
    support_radius: r_sup
    roller_length: auto              # auto = 시편 폭 + 2·target_size
  reference_nodes: [indenter_ref, support_top]   # 생성기 필수 참조 노드: 압자 강체 노드, 지지 롤러 강체 노드(처짐계 기준)
```

- 생성 강체의 `*MAT_RIGID` 값: E = `mesh.jig.rigid_E_pa`(기본 2.1e11), ν = `mesh.jig.rigid_poisson`(0.3), ρ = `mesh.jig.rigid_density_kg_m3`(7850). 강체라 변형에는 영향이 없지만 **페널티 접촉 강성이 이 E로 계산되므로** 실제 지그 재질(강재)과 다른 값을 넣으면 접촉 강성·초기 기울기·implicit 수렴이 달라진다. 레시피별로 바꿔야 하면 새 설정 키가 아니라 매니페스트 `jig.rigid: {E, poisson, density}`(선택, SI)로 덮는다.
- 생성기별 필수 참조 노드는 레지스트리 생성기가 선언하며, 매니페스트 `reference_nodes`는 그 부분집합이어야 한다(`RECIPE_RESPONSE_ID_MISSING`).

fixed 레시피에는 `jig`를 두지 않는다(지그는 `deck.files`에 포함).

### 4.6 `parameters`

```yaml
parameters:
  - name: span                       # ^[a-z][a-z0-9_]{0,7}$
    type: R                          # R | I
    dimension: length                # length | velocity | force | stress | density | time | dimensionless
    unit_si: m
    default: 0.064
    range: [0.010, 0.300]
    matnexus_key: span               # MatNexus 시편 치수 키(없으면 null)
    description: 지지 스팬
  - {name: r_ind, type: R, dimension: length, unit_si: m, default: 0.005, range: [0.001, 0.02], matnexus_key: null}
  - {name: r_sup, type: R, dimension: length, unit_si: m, default: 0.005, range: [0.001, 0.02], matnexus_key: null}
  - {name: overhang, type: R, dimension: length, unit_si: m, default: 0.005, range: [0.0, 0.05], matnexus_key: null}
  - {name: fric, type: R, dimension: dimensionless, unit_si: "1", default: 0.1, range: [0.0, 0.6], matnexus_key: null}
```

- 마찰은 노출 파라미터 `fric`로 둔다. 요청 `friction.mode=fixed, value`는 `fric`를 덮어쓴다(1차).
- 시편 L·W·T는 파라미터가 아니라 형상(`geometry.dimensions`, MatNexus 치수)에서 온다. 파라메트릭 레시피 덱이 시편 치수를 써야 하면(예: 롤러 길이) 생성 지그가 처리하고, 덱 참조용으로 엔진이 `mo_params.k`에 넣지 않는다(1차).

### 4.7 `responses`

```yaml
responses:
  - response_key: force_disp
    test_channel: force
    axis: displacement
    extractor:
      y: {name: rcforc_resultant, contact_id: 1, side: master, component: z, sign: -1, scale: symmetry}
      x: {name: nodout_disp, node: {ref: indenter_ref}, component: z, sign: -1}
    alt_axes:                        # 선택: 다른 기준축 채널 대응
      deflection:
        x: {name: nodout_rel_disp, node: {ref: midspan_bottom}, ref_nodes: [{ref: support_top}], component: z, sign: -1}
    weight: 1.0
    window: {split: auto}
```

필드 의미는 [testspec.md §3](testspec.md) `ResponseDef`와 같다. `contact_id`는 덱의 `*CONTACT_..._ID` CID, `node.id`는 레시피 소유 노드, `node.ref`는 정의된 참조점 이름만 허용: 지그 생성기 필수 참조 노드(`indenter_ref`, `support_top`) 또는 TestSpec 시편 참조점(bending: `midspan_bottom`). 고정 레시피는 지그 노드를 `node.id`로 지정한다. 엔진은 응답 노드 전부를 `mo_output.k`의 `*DATABASE_HISTORY_NODE`에 넣는다.

### 4.8 `compliance` — 장비+지그 컴플라이언스 C

```yaml
compliance:
  value_m_per_n: 1.8e-7              # C [m/N], >0. null이면 미교정
  method: rigid_block                # rigid_block | steel_specimen | other
  calibrated_at: "2026-10-01"
  instruments: ["UTM-01"]            # 선택: 이 C가 유효한 장비 이름(MatNexus TestRun.instrument)
  note: "강체 블록 3회 평균"
```

C는 식별 변수가 아니다. 장비·지그 조합이 바뀌면 새 레시피 버전으로 등록한다. 보정식은 [testspec.md §7](testspec.md).

### 4.9 `verification`

```yaml
verification:
  reference_card: verification/reference_card.yaml   # {slot: card_blocks} (SI)
  reference_dimensions: {width: 0.010, thickness: 0.004, total_length: 0.080}   # SI m
  reference_params: {span: 0.064}                    # 선택, 기본값 덮어쓰기
  reference_window_m: 0.004                          # dry-run 압자 변위(mo_dend 산정용 X_u)
  mesh_convergence:
    size_factors: [2.0, 1.0]                         # target_size 배율. 0.5 추가 가능
    quantities: [initial_slope, force_at_end]
  reference_curve: verification/reference_curve.csv  # 선택
```

## 5. 버전·해시·불변·폐기

| 규칙 | 내용 |
| --- | --- |
| 참조 | 잡은 항상 `recipe_id@version`으로 참조하고 결과에 `content_hash`와 함께 기록 → 결과 재현 |
| 해시 | `content_hash = "sha256:" + hex(sha256(L))`, L = 폴더 내 모든 파일을 상대 경로(POSIX `/`) 사전순으로 정렬해 `"{path}\0{sha256(file bytes)}\n"`을 이어 붙인 바이트. `README.md` 포함 |
| 불변 | 등록(정적 검사 통과) 이후 파일·매니페스트 수정 불가. 변경은 새 `version` |
| 충돌 | 같은 `recipe_id@version`·다른 해시: `verifying`/`active`/`retired`가 있으면 409 `RECIPE_VERSION_CONFLICT`. `rejected`만 있으면 새 업로드 허용(이전 시도는 `attempts`에 보존) |
| 멱등 | 같은 id@version·같은 해시 → 기존 레코드 반환 |
| 상태 | `verifying` → `active` \| `rejected`; `active` → `retired` |
| 폐기 | 관리자만, 사유 필수. 새 잡에서 400 `RECIPE_RETIRED`. 기존 잡·결과·파일은 유지(삭제 없음). 되살리기 없음(새 버전 등록) |
| 권한 | 등록·폐기는 관리자 토큰(API) 또는 CLI(내부적으로 같은 API 호출)만 |

## 6. CLI 사양

공통: 실행 파일 `matobserver`. 서버 주소 `--server`(기본 설정 `cli.server_url`), 관리자 토큰은 환경변수 `MATOBSERVER_ADMIN_TOKEN` 또는 `--token-file`(명령행 평문 토큰 인자 없음). 출력 기본 사람용 표, `--json`이면 API 응답 JSON 그대로.

| 명령 | 인자·옵션 | 동작 | 종료 코드 |
| --- | --- | --- | --- |
| `matobserver recipe add <folder>` | `--strict`(경고도 거절), `--wait`(검증 완료까지 대기, 폴링 간격 `cli.poll_interval_s`), `--timeout <s>` | ① 로컬 정적 검사(§7.1과 같은 코드) ② zip 묶음(해시는 서버가 계산, CLI는 로컬 계산값을 함께 출력) ③ `POST /v1/admin/recipes` ④ `--wait`면 A3를 폴링해 검증 보고서 출력 | 0 등록·검증 통과(또는 비대기 접수), 2 정적 검사 실패, 3 검증 rejected, 4 409 충돌, 5 통신·인증 오류 |
| `matobserver recipe list` | `--status <s>`(복수), `--test-type <k>`, `--recipe-id <id>`, `--limit`, `--offset` | `GET /v1/admin/recipes` 표 출력(recipe_ref, kind, test_type, status, 경고 수, 등록일) | 0, 5 |
| `matobserver recipe retire <recipe_id@version>` | `--reason <text>`(필수), `--yes`(확인 생략) | `POST .../retire` | 0, 4(verifying), 5 |

## 7. 등록 검증

### 7.1 정적 검사 (동기, 등록 요청 시)

실패는 400 `VALIDATION_FAILED` + 아래 사유 코드(CLI 종료 코드 2).

| 코드 | 검사 |
| --- | --- |
| `RECIPE_BUNDLE_INVALID` | zip 손상, 최상위 `manifest.yaml` 없음, 크기 초과 |
| `RECIPE_MANIFEST_INVALID` | §4 스키마 위반(필수·타입·패턴) |
| `RECIPE_TEST_TYPE_UNKNOWN` | `test_type` 미등록 TestSpec |
| `RECIPE_UNIT_SYSTEM_UNSUPPORTED` | |
| `RECIPE_SOLVER_UNKNOWN` / `RECIPE_GENERATOR_UNKNOWN` | 레지스트리에 없음 |
| `RECIPE_PATH_UNSAFE` | 절대 경로·`..`·링크 |
| `RECIPE_FILE_MISSING` | 매니페스트가 가리키는 파일 없음 |
| `RECIPE_INCLUDE_UNRESOLVED` | `main.k`·`deck.files`의 `*INCLUDE`가 폴더 안 파일 또는 예약 파일로 해석 안 됨 |
| `RECIPE_RESERVED_INCLUDE_MISSING` | §3.1 필수 예약 include 누락, `mo_params.k`가 첫 include가 아님, 슬롯별 `mo_mat_<slot>.k` 누락 |
| `RECIPE_RESERVED_FILE_PRESENT` | 레시피 폴더에 `mo_*.k` 존재 |
| `RECIPE_RESERVED_PARAM_REDEFINED` | 노출·예약 파라미터를 덱이 정의 |
| `RECIPE_PARAM_UNDECLARED` | 덱의 `&name` 참조가 매니페스트·예약·덱 자체 `*PARAMETER` 어디에도 없음 |
| `RECIPE_PARAM_NAME_INVALID` | 이름 규칙 위반, `default`가 `range` 밖, `range` 역전 |
| `RECIPE_OUTPUT_KEYWORD_PRESENT` | §3.3 금지 키워드 |
| `RECIPE_SLOT_ID_CONFLICT` | 슬롯 part/mid/secid/lcid를 덱이 정의 |
| `RECIPE_SECTION_INVALID` | topology–elform 비호환 |
| `RECIPE_ID_RANGE_CONFLICT` | 덱의 노드·요소 ID가 엔진 범위(시편·지그)와 겹침 |
| `RECIPE_RESPONSE_ID_MISSING` | `contact_id`가 덱 `*CONTACT_*_ID`에 없음, `node.id`가 덱에 없음, `node.ref`가 생성기·TestSpec 참조점에 없음 |
| `RECIPE_SYMMETRY_INVALID` | `force_scale`이 type과 불일치, 대칭 노드 세트 ID 누락 |
| `RECIPE_COMPLIANCE_INVALID` | `value_m_per_n` ≤ 0 |
| `RECIPE_REFERENCE_CARD_INVALID` | 기준 카드가 NeutralMaterial 불변식 위반([plugins.md §1](plugins.md)), 슬롯 누락 |
| `RECIPE_KIND_MISMATCH` | parametric인데 `jig` 없음, fixed인데 `jig` 있음 |
| `RECIPE_IMPLICIT_DTMAX_MISSING` | implicit 제어 파일에 `*CONTROL_IMPLICIT_AUTO` DTMAX=`&mo_dtmax` 없음 |

### 7.2 dry-run 검증 (비동기, Verifier 대상)

기준 카드·기준 치수·기본 파라미터로 실제 덱을 조립해 선택 솔버로 실행한다. 판정 FAIL이 하나라도 있으면 `rejected`, WARN만 있으면 `active`(경고 기록), `strict=true`면 WARN도 `rejected`.

| 코드 | 검사 | 판정 기준 | 실패 시 |
| --- | --- | --- | --- |
| `VERIFY_TERMINATION` | 정상 종료 | d3hsp/messag `Normal termination`. primary 실패 후 fallback 성공은 PASS + WARN `VERIFY_FALLBACK_USED` | FAIL |
| `VERIFY_RESPONSE` | 응답 추출 | 모든 `responses`에서 곡선 점 ≥ `recipe_verify.min_points`(20), x 단조 하중 구간 존재, F_peak > 0 | FAIL |
| `VERIFY_UNITS` | 단위 일관성 | parametric 굽힘: dry-run 초기 기울기 k_sim과 보 이론 k_th = 4·E·b·h³/L³의 비 ∈ [1/`recipe_verify.unit_ratio_tol`, `unit_ratio_tol`](기본 1.5). fixed: 비 ∈ [0.1, 10](자릿수 오류 검출) | FAIL |
| `VERIFY_PENETRATION` | 시편–지그 관통 | d3hsp 초기 관통 보고 최대값 ≤ `recipe_verify.max_initial_penetration_m`(1e-6) 그리고 첫 출력 시점 접촉력 ≤ `recipe_verify.max_initial_force_ratio`(0.01)·F_peak | FAIL |
| `VERIFY_MESH_CONVERGENCE` | 메시 수렴 | `size_factors` 각각 실행, 인접 크기 간 `initial_slope`·`force_at_end` 상대 변화 ≤ `recipe_verify.mesh_convergence_tol`(0.02) | FAIL |
| `VERIFY_REFERENCE_CURVE` | 기대 곡선(있을 때) | 엔진 공개 함수(`engine.alignment.align_pair` + `engine.objective.curve_error`, [module-boundaries.md §4.6](module-boundaries.md))로 정렬 8단계 후 J(Ω 전체) ≤ `recipe_verify.reference_curve_tol`(0.05) | FAIL |
| `WARN_MASS_SCALING` | 질량 스케일링 | `*CONTROL_TIMESTEP` DT2MS ≠ 0 또는 `*CONTROL_IMPLICIT_*` 질량 관련 설정 | WARN |
| `WARN_DAMPING` | 감쇠 | `*DAMPING_*` 존재 | WARN |
| `WARN_HARDCODED_MATERIAL` | 하드코딩 재료 | 슬롯 외 파트에 `*MAT_RIGID`가 아닌 `*MAT_*` | WARN |
| `WARN_KE_RATIO` | explicit 준정적 | `glstat_ratio` 최대 > `solver.explicit.max_ke_ratio`(0.05) | WARN |
| `WARN_NO_FALLBACK` | primary=implicit, fallback=none | — | WARN |
| `WARN_COMPLIANCE_MISSING` | C 미교정 | `compliance` null | WARN |

### 7.3 `VerificationReport` (A3 응답 `verification`)

```json
{
  "verification_id": "ver_01JC...",
  "status": "passed",
  "started_at": "...", "finished_at": "...",
  "solver": {"name": "lsdyna", "version": "R13.1.1", "adapter_version": "1.0.0"},
  "checks": [
    {"code": "VERIFY_TERMINATION", "result": "pass", "detail": "Normal termination (implicit)"},
    {"code": "VERIFY_UNITS", "result": "pass", "value": 1.07, "threshold": 1.5},
    {"code": "WARN_DAMPING", "result": "warn", "detail": "*DAMPING_GLOBAL in deck/controls_explicit.k"}
  ],
  "artifacts": [{"artifact_id": "art_01JC...", "kind": "verification_bundle"}]
}
```

`status`: `passed` | `passed_with_warnings` | `failed` | `error`(검증 실행 자체 실패, 레시피는 `rejected`). `result`: `pass` | `warn` | `fail` | `skipped`.

## 8. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| R-1 | *PARAMETER 치환은 엔진 생성 `mo_params.k` 포함 방식(덱 텍스트 직접 수정 없음). 노출 파라미터를 덱이 재정의하면 거절 | 가정 |
| R-2 | 출력 키워드(RCFORC·NODOUT·GLSTAT·MATSUM·D3PLOT·HISTORY_NODE)는 레시피 금지, 엔진이 `mo_output.k`로 생성 | 가정 |
| R-3 | 지그 강체 메시는 파라메트릭 레시피에서 GeometryMesher(`role=jig`)가 생성. 고정 레시피 압자 높이는 `&mo_htop`으로 맞춤 | 가정 |
| R-4 | 엔진 생성 ID 범위: 시편 노드·요소 1–4,999,999, 지그 5,000,000–5,999,999. 레시피 소유는 그 밖 | 가정 |
| R-5 | 파라미터 이름 ≤ 8자(LS-DYNA 고정 폭 안전) | 가정 |
| R-6 | C 장비 대조는 MatNexus `TestRun.instrument` 문자열 비교 | 가정 |
| R-7 | dry-run 판정 임계값(단위비 1.5, 관통 1e-6 m, 메시 수렴 2%) 초기값 | 가정 |
| R-8 | implicit DTMAX를 예약 파라미터 `mo_dtmax`로 강제 | 가정 |
| R-9 | 생성 지그 *MAT_RIGID 기본값은 강재(E 2.1e11 Pa, ν 0.3, ρ 7850) | 가정 |
| R-10 | 레시피 업로드 시도별 하위 경로(`<verification_id>/`) 저장 | 가정 |
| U-1 | 사내 LS-DYNA 배치 정보 미수령 — dry-run도 어댑터 설정(`solver.lsdyna.batch_command`)에 의존 | 미결 |
| U-2 | MatNexus 관리자 화면 연동(레시피 등록 UI)은 추후 협의 | 미결 |

## 변경 이력

| 버전 | 날짜 | 변경(추가·기본값 조정만, 기존 필드 의미 불변) |
| --- | --- | --- |
| 1.0 → 1.1 | 2026-11-08 | 내용 변경 없음(버전 번호만 1.1로 맞춤) |
| 1.1 → 1.2 | 2026-11-09 | 내용 변경 없음(버전 번호만 1.2로 맞춤) |
| 1.2 → 1.3 | 2026-11-09 | 내용 변경 없음(버전 번호만 1.3) |
