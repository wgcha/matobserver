schema_version: 1.3 · 상태: 승인(G1, MINOR 1.3) · 2026-11-09

# 모듈 경계·구현 소유 계약

근거: plan.md 「작업 분해·위임 계획」(2·3·4·5 병렬, 2와 3의 경계는 메시 .k 규격), 「해석 실행 전략」, 「시스템 구성」, decisions.md 전체. 구현 에이전트 4개(Impl-Mesh, Impl-Solver, Impl-Optim, Impl-Backend)는 이 문서와 나머지 5개 계약만 보고 병렬 구현한다.

## 1. 패키지 레이아웃과 파일 소유

```
matobserver/
├── pyproject.toml                         # Impl-Backend (의존성은 각자 PR로 추가 요청)
├── scripts/windows/                       # Impl-Backend (1.3): install.ps1 (deploy.md §1), tests/ (Pester 스모크)
├── config/
│   ├── matobserver.example.yaml           # Impl-Backend (전 설정 키 기본값, §6)
│   └── plugins.d/
│       ├── mesh.yaml                      # Impl-Mesh
│       ├── solver.yaml                    # Impl-Solver
│       ├── optim.yaml                     # Impl-Optim (optimizer, material_model, test_spec, surrogate solver)
│       └── (backend 없음)
├── src/matobserver/
│   ├── core/                              # Impl-Backend 소유 — plugins.md §1·§4, testspec.md §3 타입을 그대로 옮김
│   │   ├── types.py                       #   공통 dataclass·Protocol (계약 문서가 원본, 임의 변경 금지)
│   │   ├── units.py                       #   SI ↔ mm·N·s·tonne 변환표
│   │   ├── registry.py                    #   plugins.d 로더·검사
│   │   ├── config.py                      #   설정 로더(§6)
│   │   ├── errors.py                      #   오류·경고 코드 상수(api.md §7, result.md §4.10)
│   │   └── ids.py, hashing.py             #   ULID, 정규화 JSON sha256
│   ├── api/                               # Impl-Backend: FastAPI 앱, 라우트, pydantic 스키마(api.md)
│   ├── jobs/                              # Impl-Backend: JobStore(SQLite), 큐, 워커 루프, 상태 머신, 취소
│   ├── artifacts/                         # Impl-Backend: artifact 등록·다운로드
│   ├── recipes/                           # Impl-Backend: 매니페스트 파싱·정적 검사(recipe.md §7.1)·저장소·해시·상태
│   ├── matnexus/                          # Impl-Backend: MatNexus 클라이언트, RunRecord 정규화
│   ├── validation/                        # Impl-Backend: 잡 생성 검증 파이프라인(api.md §4.7 순서), 플러그인 validate 호출
│   ├── cli.py                             # Impl-Backend: `matobserver serve|worker|recipe|start|stop|status|doctor|db migrate` (1.3, deploy.md §2)
│   ├── engine/                            # Impl-Optim: 식별 엔진(단계·θ 조립·목적함수·정렬·케이스·식별성·불확도·결과 조립)
│   │   ├── run.py                         #   run_calibration() 진입점(§4.1)
│   │   ├── roundtrip.py                   #   1요소 왕복 검사(result.md §4.9, §4.6)
│   │   ├── explicit_check.py              #   explicit 재현 검사(testspec.md §8.1, result.md §4.12)
│   │   ├── alignment.py                   #   testspec.md §5
│   │   ├── objective.py                   #   testspec.md §8
│   │   ├── identifiability.py             #   testspec.md §9
│   │   └── result.py                      #   result.md 조립
│   ├── optimizers/                        # Impl-Optim: bo, lsq, random
│   ├── materials/                         # Impl-Optim: mat001, mat024_coeff, mat024_table
│   ├── testspecs/                         # Impl-Optim: bending
│   ├── solvers/
│   │   ├── surrogate/                     # Impl-Optim: surrogate_beam (계약 테스트 기준 구현, G2용)
│   │   └── lsdyna/                        # Impl-Solver: 어댑터, 배치 실행, 종료 판정, binout 추출
│   ├── deck/                              # Impl-Solver: 덱 조립(mo_*.k 생성), 재료 키워드 writer, .k 리더
│   ├── verify/                            # Impl-Solver: 레시피 dry-run 검사(recipe.md §7.2)
│   └── mesh/                              # Impl-Mesh: box, layered_box, stp(gmsh), jig.bending3p_rollers, 품질·대칭·.k writer
└── tests/
    ├── contract/                          # 타입별 파일 소유 = 그 플러그인 소유자. 공통 픽스처 conftest는 Impl-Backend
    │   ├── test_optimizer_contract.py     # Impl-Optim
    │   ├── test_material_contract.py      # Impl-Optim
    │   ├── test_testspec_contract.py      # Impl-Optim
    │   ├── test_solver_contract.py        # Impl-Solver (surrogate_beam 파라미터화 케이스는 Impl-Optim 구현으로 통과해야 함)
    │   └── test_mesher_contract.py        # Impl-Mesh
    ├── unit/<package>/                    # 패키지 소유자
    ├── api/                               # Impl-Backend
    ├── integration/                       # Verifier (G3)
    └── fixtures/                          # 소유자별 하위 폴더: fixtures/{mesh,solver,optim,backend}/
                                           #   MatNexus 자산 사본(dp600-1elem-lsdyna.k, flexural run JSON)은 fixtures/shared/ (Impl-Backend)
```

| 규칙 | 내용 |
| --- | --- |
| 소유 | 표의 소유자만 해당 디렉터리를 쓴다. 다른 소유 디렉터리 변경이 필요하면 소유자에게 요청 |
| `core/` | Impl-Backend가 **첫 커밋**으로 계약 문서의 타입을 그대로 옮긴다. 다른 에이전트는 그 전까지 같은 정의를 `tests/` 안 임시 스텁으로 쓰고 병합 시 제거. 타입 변경은 계약 개정(schema_version MINOR)으로만 |
| 의존 방향 | `api`/`jobs` → `engine` → (`core`의 Protocol) ← 플러그인들. 엔진은 플러그인 구현 모듈을 import하지 않고 `core.registry`로만 얻는다. 플러그인끼리 서로 import 금지(예외: `deck`의 .k 리더를 `verify`가 사용 — 같은 소유자). `verify` → `engine.alignment`·`engine.objective`(순수 함수만, §4.6·B-7) 허용, `engine` → `verify` 금지 |
| 외부 라이브러리 | 메시: gmsh(파이썬 API). 최적화: 자유(scikit-learn GP, BoTorch 등)이되 결정성(O4) 보장. binout 읽기: 자유(예: lasso-python). 버전 고정은 pyproject |

## 2. 메시 파일(.k include) 규격 — Impl-Mesh ↔ Impl-Solver 경계

GeometryMesher가 쓰고 덱 조립기가 `mo_specimen.k`(시편)·`mo_jig.k`(지그)로 그대로 복사한다.

| 항목 | 시편 `mo_specimen.k` | 지그 `mo_jig.k` |
| --- | --- | --- |
| 단위 | mm, (힘·질량은 미기재) | 같음 |
| 인코딩 | ASCII, LF, 첫 줄 `*KEYWORD`, 끝 `*END` | 같음 |
| 머리 주석 | `$ matobserver mesh` + `$ generator=<name> version=<v> topology=<t> target_size_mm=<h> units=mm-N-s-tonne` + `$ input_sha256=<입력 spec 정규화 해시>` | 같음 |
| 허용 키워드 | `*NODE`, `*ELEMENT_SOLID`(hex8·tet4·tet10 공통, 아래 형식), `*SET_NODE_LIST`(+`_TITLE`), `*SET_PART_LIST`. **(2차 예약)** 2D 축대칭 `quad4_axisym`용 `*ELEMENT_SHELL`(대응 `*SECTION_SHELL`은 여전히 슬롯 파일 소관) | `*NODE`, `*ELEMENT_SHELL`, `*PART`, `*SECTION_SHELL`, `*MAT_RIGID`, `*SET_NODE_LIST` |
| 금지 | `*PART`, `*SECTION_*`, `*MAT_*`, `*INCLUDE`, `*PARAMETER`, 경계조건·접촉, `*ELEMENT_SOLID_TET4TOTET10`(변환 옵션 금지 — 10절점은 메셔가 직접 씀) | `*INCLUDE`, `*PARAMETER`, 경계조건·접촉(접촉은 레시피 덱) |
| 형식 | 고정 폭 표준 형식. `*NODE`: NID i8, X·Y·Z e16.9, TC·RC i8 (한 줄 80칸) | `*ELEMENT_SHELL`: EID·PID·N1..N4 i8 한 줄 |
| `*ELEMENT_SOLID` 줄 배치 | 키워드는 옵션 없는 `*ELEMENT_SOLID` 하나, **요소당 2줄 형식**: 1줄 = EID i8, PID i8 (16칸) / 2줄 = 절점 i8 × 최대 10칸. hex8: N1..N8 (8칸). tet4: N1 N2 N3 N4 N4 N4 N4 N4 (8칸, 축퇴, ELFORM 10/13). tet10: N1..N10 (10칸, 80칸 꽉 참; N1–N4 꼭짓점, N5=1-2, N6=2-3, N7=3-1, N8=1-4, N9=2-4, N10=3-4 중간절점; ELFORM 16/17). 한 파일 안에서 형식 혼용 금지 | — |
| 지그 강체 재료 | — | `*MAT_RIGID` RO = `mesh.jig.rigid_density_kg_m3`(7850) → tonne/mm³, E = `mesh.jig.rigid_E_pa`(2.1e11) → MPa, PR = `mesh.jig.rigid_poisson`(0.3); 매니페스트 `jig.rigid`가 있으면 그 값. 주석: E는 페널티 접촉 강성 계산에 쓰이므로 실제 지그 재질값을 쓴다(recipe.md §4.5) |
| 요소 PID | `GeometrySpec.region_part_ids`의 값 | 매니페스트 `jig.part_ids` |
| ID 범위 | 노드·요소 1–4,999,999(매니페스트 `specimen.id_ranges`) | 5,000,000–5,999,999(`jig.id_ranges`) |
| 세트 | 대칭면 `*SET_NODE_LIST` id = 매니페스트 `specimen.node_sets.sym_x0/sym_y0`; 참조점 세트는 `MeshResult.node_sets`에 보고(ID는 4,900,001부터); 시편 전체 `*SET_PART_LIST` id = `specimen.part_set_id` | `indenter_ref` 등 참조 노드는 강체 파트 소속 노드 |
| 좌표 | [recipe.md §4.3](recipe.md) 배치 규약 | 같음, 롤러 축은 y |
| 대칭 | quarter: x≥0, y≥0. 대칭면 노드 좌표 정확히 0 | 대칭 영역만 생성 |
| 결정성 | 같은 입력 → 바이트 동일 | 같음 |

주: 위 tet10 2줄 형식·절점 순서는 LS-DYNA R12+ 매뉴얼 Vol I `*ELEMENT_SOLID` 기준으로 고정한다. Impl-Mesh는 `tests/fixtures/mesh/`에 hex8·tet4·tet10 샘플을 두고, Impl-Solver는 S-6에서 tet10 샘플 덱을 실제 실행(정상 종료·음 체적 없음)으로 확인한다. 실행 결과 형식이 다르면 계약 개정(MINOR)으로 고친다.

## 3. 단위계

| 영역 | 단위 | 근거 |
| --- | --- | --- |
| API·MatNexus·Python 객체(엔진·플러그인 인터페이스·결과) | **SI 고정**(m, N, Pa, s, kg/m³) | MatNexus 정본 SI, 결과 `value_si` 일치 |
| LS-DYNA 파일(.k: 메시·덱·재료 카드·mo_params) | **mm·N·s·tonne 고정**(응력 MPa, 밀도 tonne/mm³) | plan 「해석 단위계(mm·N·MPa·s) 변환은 이 SW가 한다」, 매니페스트 `unit_system` |
| 변환 위치 | 파일 writer·reader 안에서만: Impl-Mesh(.k 좌표), Impl-Solver(덱·재료·추출 결과 SI 환산) | 경계에서 단위 혼동 방지 |
| 변환표 | `core/units.py`: length 1e3, stress 1e-6, density 1e-12, velocity 1e3, force 1, time 1, compliance(m/N→mm/N) 1e3 | |

설정으로 바꾸지 않는다(1.0). 다른 단위계 레시피는 MAJOR 변경.

## 4. 모듈 간 경계 데이터

### 4.1 Backend → Engine (Impl-Backend ↔ Impl-Optim)

```python
@dataclass(frozen=True)
class CalibrationSpec:                 # 검증·정규화 완료된 요청 (api.md §4.2 + 해석된 기본값)
    job_id: str
    schema_version: str
    tests: tuple[ResolvedTest, ...]    # test_key, test_type, recipe(ResolvedRecipe), geometry(GeometrySpec 원형 dict),
                                       # runs(RunRecord...), displacement_channel, recipe_params_si, user_window, weight
    materials: ResolvedMaterials       # mode, slots(SlotInput + 병합 옵션 + reference NeutralMaterial)
    optimizer: tuple[str, Mapping]     # 탄소성 단계
    elastic_optimizer: tuple[str, Mapping]
    solver: tuple[str, Mapping]
    friction: Mapping[str, Any]
    budget: Mapping[str, float]
    seed: int
    request_hash: str
    plasticity_mode: str | None = None          # (1.1) 요청 plasticity_mode(해석값). None이면 아래 우선순위
    explicit_check: Mapping[str, Any] | None = None  # (1.1) 요청 explicit_check(해석값, api.md §4.6.1)

@dataclass(frozen=True)
class ResolvedRecipe:
    recipe_ref: str
    content_hash: str
    store_dir: Path                    # 읽기 전용. recipes.store_dir/<recipe_id>/<version>/<verification_id>/ 중 active 시도 폴더(recipe.md §2)
    manifest: Mapping[str, Any]

class EngineServices(Protocol):
    registry: Registry                 # core.registry
    config: Config
    owner_kind: Literal["job", "recipe_verification"]
    owner_id: str                                          # job_id 또는 verification_id
    def workdir(self, *parts: str) -> Path: ...           # job: paths.work_dir/{job_id}/... / recipe_verification: paths.work_dir/_recipes/{verification_id}/... 생성·반환
    def add_artifact(self, path: Path, kind: str, *, slot: str | None = None,
                     test_key: str | None = None) -> str: ...   # artifact_id
    def record_eval(self, item: Mapping[str, Any]) -> None: ...  # api.md HistoryItem 형
    def progress(self, stage: str, **fields: Any) -> None: ...   # JobStatus.progress 갱신
    cancel: CancelToken
    job_options: Mapping[str, Any]     # (1.1) Backend 통로: {"plasticity_mode", "explicit_check"} (1.0 구현 호환)

def run_calibration(spec: CalibrationSpec, services: EngineServices) -> Mapping[str, Any]: ...
    # 반환: result.md §1 본문(artifacts 포함). 예외 EngineFailure(code)는 api.md §7.3 코드
```

잡 생성 시 검증(api.md §4.7 ⑤⑥⑩)에 필요한 엔진 기능은 순수 함수로 노출: `engine.preflight(spec_draft, services) -> list[Issue]`(곡선 정렬 1–5단계 시험측, 식별성 사전 점검). Backend의 `validation/`이 호출한다.

(1.1) 엔진의 잡 옵션 읽기 우선순위: **`CalibrationSpec` 필드 → `services.job_options[key]` → 설정**(`engine.default_plasticity_mode`, `verify.explicit_check.*`). 값이 None이면 다음 단계로 넘어간다. 현재 Backend는 `job_options`로 넘기고 엔진은 세 곳을 모두 읽는다. Backend는 1.1부터 spec 필드도 채운다(둘이 다르면 spec 우선).

(1.1) 취소: 엔진은 `CancelToken.is_set()`을 평가 사이·해석 폴링 중에 확인하고 `CalibrationCancelled`(`matobserver.engine`에서 export, `core.errors.MatObserverError` 하위)를 던진다. `EngineFailure`가 아니므로 실행 오류 코드(api.md §7.3)가 없다. Backend 처리 규칙은 [api.md §4.12](api.md)(상태 `cancelled`, 결과 미저장, history·등록 artifact 보존).

### 4.2 Backend → 레시피 검증 (Impl-Backend ↔ Impl-Solver)

```python
def verify_recipe(recipe: ResolvedRecipe, services: EngineServices, strict: bool) -> Mapping[str, Any]: ...
    # 반환: recipe.md §7.3 VerificationReport. 정적 검사(§7.1)는 Backend recipes/가 먼저 수행.
    # 메시는 registry의 GeometryMesher로 직접 생성(Impl-Solver가 호출만), TestSpec은 registry로 얻는다.
```

### 4.3 Engine → Mesh (Impl-Optim ↔ Impl-Mesh)

`GeometryMesher.mesh(GeometrySpec, MeshOptions, out_dir) → MeshResult`, 지그 `→ JigResult` ([plugins.md §4.4](plugins.md)). 엔진은 매니페스트에서 `MeshOptions`를 만든다: topology(형상이 stp면 `specimen.element.stp_topology`), target_size, min_elems, `mesh.max_elements`, symmetry(stp면 none), ID 범위, 세트 ID. 케이스당 1회.

### 4.4 Engine → Solver (Impl-Optim ↔ Impl-Solver)

`SolverAdapter.prepare(SimulationCase, {slot: NeutralMaterial}, workdir) → run → extract` ([plugins.md §4.2](plugins.md)). 엔진 책임: `SimulationCase` 구성(파라미터 값 결정, mo_* SI 값, OutputRequest, SlotBinding). 어댑터 책임: 예약 include 파일 생성(recipe.md §3.1), 단위 변환, 실행, 종료 판정, SI 추출.

### 4.5 작업 폴더 레이아웃

```
{paths.work_dir}/{job_id}/
├── inputs/            request.json, runs/{run_id}.json(정규화 RunRecord), stp/{file_id}.stp  — Backend
├── cases/{case_id}/   mesh/specimen.k, mesh/jig.k, mesh/result.json                        — Engine(호출)·Mesh(쓰기)
├── evals/{eval_index:05d}/{case_id}/   덱·mo_*.k·binout·d3hsp·messag(·d3plot)              — Solver
├── best/{case_id}/    최적점 실행 폴더 사본                                                  — Engine
├── roundtrip/{slot}/  1요소 왕복                                                           — Solver
├── explicit_check/{case_id}/   explicit 재현 검사 실행 폴더                                  — Solver
└── artifacts/         다운로드 대상 파일                                                    — Backend 등록
```

산출물 위치는 **`{paths.work_dir}/{job_id}/artifacts/` 하나**다(별도 `paths.artifact_dir` 없음). 레시피 검증 산출물은 `{paths.work_dir}/_recipes/{verification_id}/artifacts/`.

삭제는 이 폴더 내부에서만(`cleanup`, `solver.keep_d3plot: best|all|none`). 레시피 저장소·입력 파일은 읽기 전용.

### 4.6 검증 함수 계약 (왕복 검사·기대 곡선 비교)

| 함수 | 시그니처 | 소유 | 호출자 | 의존 방향 |
| --- | --- | --- | --- | --- |
| 1요소 단축 실행 | `SolverAdapter.single_element_uniaxial(material: NeutralMaterial, strain_max: float, workdir: Path, timeout_s: float) -> CurveData` | Impl-Solver(`lsdyna`), Impl-Optim(`surrogate_beam`: 해석적 반환) | 엔진 | 엔진 → Protocol |
| 왕복 검사 | `engine.roundtrip.check(slot: str, material: NeutralMaterial, adapter: SolverAdapter, workdir: Path, strain_max: float, tol: float) -> Mapping[str, Any]` (반환 = result.md §4.9 `per_slot` 원소) | Impl-Optim(`engine/roundtrip.py`) | `engine.run` finalize 단계 | 엔진 → Protocol. `verify/`를 import하지 않음 |
| explicit 재현 검사 | `engine.explicit_check.run(case: SimulationCase, materials: Mapping[str, NeutralMaterial], implicit_best: Extracted, exp: Sequence[TestData], adapter: SolverAdapter, overrides: RunOverrides, workdir: Path, cfg: Config) -> Mapping[str, Any]` (반환 = result.md §4.12 `per_case` 원소). 내부에서 `adapter.prepare(..., mode="explicit", overrides=...)` → `run` → `extract`, 지표는 `align_pair`·`curve_error` | Impl-Optim(`engine/explicit_check.py`), 어댑터 쪽 `RunOverrides` 처리는 Impl-Solver | `engine.run` finalize 단계, 왕복 검사 이후 | 엔진 → Protocol |
| 곡선 정렬 | `engine.alignment.align_pair(exp: CurveData, sim: CurveData, response: ResponseDef, cfg: Config, *, compliance_m_per_n: float \| None = None) -> AlignedPair` (`AlignedPair`: 격자 x, f_exp, f_sim, region 라벨, shortfall) | Impl-Optim | 엔진, `verify/` | `verify` → `engine`(순수 함수만). 역방향 금지 |
| 곡선 오차 | `engine.objective.curve_error(pair: AlignedPair, region_weights: Mapping[str, float], cfg: Config) -> float` | Impl-Optim | 엔진, `verify/` | 같음 |
| 레시피 기대 곡선 검사 | `verify.recipe_checks.check_reference_curve(curve: CurveData, reference_csv: Path, response: ResponseDef, cfg: Config) -> Mapping[str, Any]` (반환 = recipe.md §7.3 `checks[]` 원소, code `VERIFY_REFERENCE_CURVE`) | Impl-Solver | `verify_recipe` | `verify` → `engine.alignment`/`engine.objective` |

`engine.alignment`·`engine.objective`는 I/O·레지스트리 의존이 없는 순수 모듈로 유지한다(Impl-Solver가 import해도 순환이 생기지 않게).

## 5. 에이전트별 완료 기준 테스트

### 5.1 Impl-Mesh

| # | 테스트 | 기준 |
| --- | --- | --- |
| M-1 | 계약 테스트 G1–G12 (`box`, `layered_box`, `stp`, `jig.bending3p_rollers`) | 전부 통과 |
| M-2 | box quarter/half/none 체적·노드 세트 | G1·G7 |
| M-3 | layered_box 2·3층, `stack_axis` z·y | G11, 레이어 PID |
| M-4 | STP 픽스처 3종(단일 바디 육면체, 필렛 있는 블록, 2바디) tet10/tet4 | 체적 오차 ≤1%, 품질, 바디→PID |
| M-5 | 잘못된 STP(손상 파일, 얇은 부위) | `validate`가 `STP_UNREADABLE`/`STP_CHECK_FAILED` Issue |
| M-6 | `max_elements` 초과 조대화 | G10 |
| M-7 | 지그 롤러: span·R 변화 시 위치·초기 간격 0, `indenter_ref`·`support_top` 존재, *MAT_RIGID E·ν·ρ가 `mesh.jig.*`(또는 매니페스트 `jig.rigid`) 값 | 해석적 위치와 ≤1e-9 m |
| M-8 | §2 규격 준수 — Impl-Solver `.k` 리더로 재파싱(리더 미완성 시 자체 고정 폭 파서 + 샘플 공유) | 성공 |
| M-9 | (1.1) 계약 G12: 품질 임계 미달 → `MESH_QUALITY_LOW` 경고, 뒤집힘 → 실패 | 통과 |

### 5.2 Impl-Solver

| # | 테스트 | 기준 |
| --- | --- | --- |
| S-1 | 계약 테스트 S1–S13 (`lsdyna`: 실행 없는 단위는 가짜 배치 스크립트로, 실행 필요 항목은 `@pytest.mark.lsdyna`) | 전부 통과 |
| S-2 | 덱 조립: 픽스처 레시피(parametric·fixed 각 1)에서 예약 include 6종 생성, *PARAMETER 값 환산 | 기대 텍스트와 일치 |
| S-3 | 재료 writer 왕복: MatNexus `dp600-1elem-lsdyna.k` 사본의 *MAT_024 값 재현 | 상대오차 ≤1e-6 |
| S-4 | 종료 판정: Normal/Error termination, 라이선스 오류 로그 픽스처 | 상태 정확 |
| S-5 | binout 추출: 기준 binout 픽스처에서 RCFORC·NODOUT 곡선, 부호·대칭 배율·SI | 기준 CSV와 ≤1e-9 |
| S-6 | 3점굽힘 샘플 덱 실제 실행(LS-DYNA 있는 환경), implicit 정상 종료, F-δ 추출 | 보 이론 초기 기울기와 ≤5% |
| S-7 | implicit 실패 모사 → explicit fallback | `fallback_used=True` |
| S-8 | 레시피 dry-run 검사 각 코드(recipe.md §7.2) PASS/FAIL 픽스처 | 판정 일치 |
| S-9 | `single_element_uniaxial`(계약 S11) 및 `check_reference_curve`(§4.6) | 입력 테이블 ≤1% 재현, 기대 곡선 판정 일치 |
| S-10 | implicit 제어 `&mo_dtmax` 반영, keep 매핑표대로 정리 | 생성 덱·남은 파일 일치 |
| S-11 | 계약 S12(`RunOverrides`): ELFORM 1 + IHQ 6 덱 생성, 질량 스케일링 제어 → `IncompatibleOverride`, 샘플 굽힘 덱 explicit 실행 KE/IE 기록 | 통과 |
| S-12 | (1.1) 계약 S13 `render_card`(lsdyna): 절단·외삽 반영 NeutralMaterial → 카드 재파싱 일치, MatNexus dp600 1요소 덱 *MAT_024와 필드 배치 일치 | 통과 |

### 5.3 Impl-Optim

| # | 테스트 | 기준 |
| --- | --- | --- |
| O-1 | 계약 테스트 O1–O9, M1–M9, T1–T10, `surrogate_beam`의 S1–S5·S9·S11(`single_element_uniaxial`, 해석적) | 통과 |
| O-2 | 정렬 8단계 단위 테스트: 각 단계 수치 규칙(토우·파손 절단·격자 점 수·출력 간격·범위 부족) | testspec.md §5 값과 일치 |
| O-3 | 컴플라이언스 보정식(엔진 정렬 1단계 c에서만 1회 적용, map_run 경로와 합쳐도 1회) | 합성 곡선 δ_xhead = δ_spec + C·F 에서 복원한 δ의 최대 절대오차 ≤ 1e-12 × (δ_spec 범위). 이중 적용 시 오차가 C·F 크기로 나타나 실패 |
| O-4 | **G2 가상 역식별(모드 A 탄성)**: `surrogate_beam`으로 만든 곡선에서 E 복원. 노이즈 모델·시드는 아래 주 | 노이즈 0%: 상대오차 ≤1%(개별). 노이즈 0.5%(1.2 분포 기준): 시드 13개 전부 실행, E 상대오차 **중앙값 ≤0.5%, 최대 ≤1.5%** |
| O-5 | **G2 가상 역식별(탄소성)**: 알려진 Swift 계수로 만든 곡선에서 `mat024_coeff` 계수 복원 / `mat024_table`로 테이블 복원 | 노이즈 0%: F-δ J ≤ 0.01. 노이즈 있을 때: J ≤ 1.2 × J(참값 파라미터로 계산한 J, 노이즈 바닥값). 공통: 테이블 응력 최대 상대오차 ≤3%(유효 범위 내), `mat024_coeff`·`mat024_table` 둘 다(1.1 보완: 노이즈 시 J 0.01은 구조적으로 도달 불가 — Verifier G2) 1.2 분포 기준(노이즈 0.5%): 시드 13개 전부, 테이블 노드 응력 상대오차(유효 범위 내 노드별 최대) **중앙값 ≤3%, 최대 ≤5%**, J/J(참값) ≤1.2 **전 시드**. 노이즈 0%는 위 개별 기준 유지 |
| O-6 | 모드 B 탄성: 2슬롯 층상, 서로 다른 `stack_axis` 시험 2개 동시 피팅 | 두 E 각각 ≤2% |
| O-7 | 식별성: 단일 시험 + 2슬롯 → 사전 점검 `IDENTIFIABILITY_UNDERDETERMINED`; 상관 높은 테이블 노드 → `HIGH_CORRELATION` | 판정 일치 |
| O-8 | 재현성: 같은 `CalibrationSpec`·seed 2회 | 결과 JSON(시각 제외) 동일, `input_hash` 동일 |
| O-9 | 실패 평가 페널티: 무작위 30% 실패 주입 | 루프 유지, `penalized` 기록 |
| O-10 | 결과 조립: result.md 스키마 검증(JSON Schema 생성 후) | 통과 |
| O-11 | 탄성 단계에서 솔버로 넘어가는 identify 슬롯 카드의 `table`이 None | 통과 |
| O-12 | 비재료 파라미터 경로(R9): 가짜 TestSpec이 `_test.t1.mu`를 내면 θ에 합류하고 레시피 `fric`로 전달 | 통과 |
| O-13 | `engine.roundtrip.check`, `align_pair`/`curve_error` 공개 함수(§4.6) | 시그니처·반환 형식 일치 |
| O-14 | explicit 재현 검사(가짜 어댑터): 단계 순서(왕복 이후), 지표 계산, 임계값 초과 시 경고만·잡 성공, 실패·건너뜀 코드 | 일치 |
| O-15 | 기준 변형률속도 집계(T9 값 → result.md §4.13), `RATE_DEPENDENCE_NOT_IDENTIFIED` 항상 기록 | 일치 |
| O-16 | (1.1) 탄소성 기본 `lsq`(설정 `engine.stages.plastic.optimizer`), 1.0 별칭 키 호환 | 통과 |
| O-17 | (1.1) 결과 조립 시 `card_lsdyna`를 `render_card`로 생성(최적점 카드 복사 금지), `card_format=None`이면 생략 | 통과 |
| O-18 | (1.1) elastic 예산 상한 ceil(0.3·N), 정체 k·(d+1), MaterialModel 호출 순서, `load_mode` 수집, explicit 검사 run 평균 | 일치 |
| O-19 | (1.1) 옵션 우선순위 spec → job_options → config, `CalibrationCancelled` 발생 | 일치 |
| O-20 | (1.2) joint 단계: 예산 포함·예약, E 범위 ±`e_bound_rel`, 채택 규칙, 사후 진단이 최종 채택 단계 기준, `e_by_stage` 기록 | 일치 |
| O-21 | (1.2) 정칙화: J(데이터)·J_reg 분리 기록, 최적화 목적에만 J_reg, 해석된 옵션(`max_strain` auto·`smoothing_weight`)이 결과·`input_hash`에 반영 | 일치 |

주(G2 노이즈, 1.2): 노이즈 모델 = 가산 정규 잡음 F_noisy(x) = F_true(x) + N(0, σ²), σ = 0.005·max\|F_true\| (곡선마다 독립, 원 샘플점마다). 시드 고정 목록 `[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]`(numpy `default_rng(seed)`), 시드는 잡음 생성에만 쓰고 최적화 seed는 잡 기본값. 시드 추가는 허용, 판정은 이 13개 포함 전부.

### 5.4 Impl-Backend

| # | 테스트 | 기준 |
| --- | --- | --- |
| B-1 | API 계약 테스트: E1–E12, A1–A4 요청·응답 스키마(api.md) | 전부 통과 |
| B-2 | 400 사유 코드: api.md §7.4 각 코드별 최소 1개 요청 | 코드 일치 |
| B-3 | 인증: 토큰 없음/불일치/서비스 토큰으로 admin | 401/401/403 |
| B-4 | 잡 상태 머신: queued→running→succeeded/failed/cancelled, 취소 2경로, 워커 재시작 시 `WORKER_LOST` | 전이 일치 |
| B-5 | 워커 1개·잡 1건 동시 실행(2잡 제출 시 순차) | 통과 |
| B-6 | MatNexus 클라이언트: 기록된 응답(픽스처)으로 run·치수·curve 매핑, 5xx/타임아웃 → 502 | 통과 |
| B-7 | 레시피: 정적 검사 코드(recipe.md §7.1), 해시 규칙, 멱등·409, 폐기 후 400 `RECIPE_RETIRED` | 통과 |
| B-8 | CLI `recipe add/list/retire` 종료 코드(recipe.md §6) | 일치 |
| B-9 | artifact: 경로 비노출, 루트 밖 접근 차단, ETag, 410 | 통과 |
| B-10 | 레지스트리 로더: 중복·클래스 불일치 → 기동 실패 | 통과 |
| B-11 | 엔진 대역(가짜 `run_calibration`)으로 E3→E5→E6 종단 흐름 | 통과 |
| B-12 | (1.1) `CalibrationSpec.plasticity_mode`·`explicit_check` 채움(job_options와 동일값), 취소 3경로(api.md §4.12) → `cancelled`·error null·결과 미저장 | 통과 |
| B-13 | (1.1) 설정 기본값 갱신: `alignment.elastic_dev_tol`=0.01, `material.mat024_table.first_node_strain`=0.002, `engine.stages.{elastic,plastic}.optimizer`=lsq(1.0 키 별칭) | 통과 |
| B-15 | (1.3) `doctor` D01–D11 항목별 단위 테스트: 각 항목 OK/WARN/FAIL/SKIP 픽스처, 종료 코드 0/1/2/3, `--json` 스키마(deploy.md §2.2) | 통과 |
| B-16 | (1.3) `start/stop/status`: process 모드 PID 파일 생성·삭제·stale 처리, 이미 실행 중 start 멱등, 잡 실행 중 stop 거부(코드 4)·`--force`, status 항목·종료 코드(0/3/4/1). scheduler 모드는 `schtasks` 호출 대역(mock) | 통과 |
| B-17 | (1.3) `db migrate`: `user_version` 전진, 백업 생성, 실패 롤백, 새 DB 거부 | 통과 |
| B-18 | (1.3) `file:` 비밀값 참조 로드·누락 시 D01 FAIL | 통과 |
| B-19 | (1.3) install.ps1 Windows CI(windows-latest, Python 3.11): 비대화형 설치(`-NonInteractive -NoSchedule -Wheel <빌드 wheel> -LsDynaBatchScript <더미 .bat>`) → `doctor` 종료 코드 ≤1 → `-Update`(같은 wheel 재설치, 설정·DB 보존 확인) → `-Uninstall`(데이터 보존 확인) 스모크. 종료 코드 11(Python 미달 모사)·14(필수값 누락) 검사 | 통과 |
| B-20 | (1.3) 버전 일관성: `__version__`·`pyproject` 일치, `CONTRACT_SCHEMA_VERSION == "1.3"`, `/v1/health`·`--version` 노출 | 통과 |

## 6. 설정 파일

- 파일: `MATOBSERVER_CONFIG`(기본 `./config/matobserver.yaml`). 환경변수 덮어쓰기 `MATOBSERVER__<SECTION>__<KEY>`(대문자, `__` 구분). 비밀값은 `env:VAR` 참조 허용, (1.3) `file:<경로>` 참조 허용(deploy.md §2.4).
- 환경별 값(경로·배치 스크립트·코어 수·임계값)은 코드에 쓰지 않고 아래 키로만 둔다. 기본값은 각 계약 문서 표의 괄호 값.

| 섹션 | 주요 키 | 정의 문서 |
| --- | --- | --- |
| `paths` | `data_dir`, `work_dir` | 이 문서 §4.5 |
| `runtime` | (1.3) `pid_dir`(기본 `<paths.data_dir>/run`) | deploy.md §2.1 |
| `logging` | (1.3) `dir`, `max_bytes`(10 MiB), `backups`(5) | deploy.md §1.8 |
| `doctor` | (1.3) `min_free_bytes`(10 GiB), `command_timeout_s`(10) | deploy.md §2.2 |
| `jobstore` | `sqlite_path` | |
| `api` | `host`, `port`, `page_default_limit`, `page_max_limit`, `idempotency_ttl_s`, `max_tests_per_job`, `max_runs_per_test`, `max_slots` | api.md |
| `auth` | `service_tokens[]`, `admin_tokens[]` | api.md §2 |
| `files` | `max_bytes` | api.md §4.1 |
| `artifacts` | `retention_days` | api.md §6 |
| `matnexus` | `base_url`, `token`, `timeout_s`, `curve_max_points`, (1.3) `probe_path`(`/api/test-runs?limit=1`) | api.md §2, result.md §7.1 |
| `worker` | `cancel_poll_s`, (1.3) `heartbeat_s`(10) | api.md §4.12 |
| `registry` | `paths` | plugins.md §2 |
| `recipes` | `store_dir`, `max_bundle_bytes`, `max_unpacked_bytes`, `max_files` | recipe.md §2 |
| `recipe_verify` | `min_points`, `unit_ratio_tol`, `max_initial_penetration_m`, `max_initial_force_ratio`, `mesh_convergence_tol`, `reference_curve_tol` | recipe.md §7.2 |
| `cli` | `server_url`, `poll_interval_s` | recipe.md §6 |
| `solver` | `run_timeout_s`, `kill_grace_s`, `poll_interval_s`, `keep_d3plot`(best\|all\|none), `keep_all_files`, `d3plot_frames`, `implicit.t_end_s`, `explicit.loading_velocity_m_s`, `explicit.max_ke_ratio`, `license_retry.{interval_s,max_wait_s}` | plugins.md §4.2, testspec.md |
| `solver.lsdyna` | (1.3) `executable`(선택, 버전 정보 읽기용), `version_command`(선택, 실행 없이 버전 출력), `batch_command`(인자 템플릿 리스트, 예 `["{script}", "-i", "{main}", "-n", "{ncpu}"]`), `script`, `mode`(smp\|mpp), `ncpu`(기본 4), `precision`, `termination_patterns`, `license_error_patterns`, `version_pattern` | plugins.md §4.2 |
| `mesh` | `max_elements`, `min_scaled_jacobian`, `max_aspect_ratio`, `volume_rel_tol_parametric`, `volume_rel_tol_stp`, `gmsh_threads`(1), `jig.rigid_E_pa`(2.1e11), `jig.rigid_poisson`(0.3), `jig.rigid_density_kg_m3`(7850) | plugins.md §5.4, 이 문서 §2 |
| `geometry` | `layer_sum_rel_tol` | api.md §4.4 |
| `engine` | `default_solver`, `default_plasticity_mode`, `default_seed`, `stages.elastic.optimizer`(lsq), `stages.plastic.optimizer`(lsq, 1.1), `stages.joint.enabled`(true, 1.2), `stages.joint.optimizer`(lsq), `stages.joint.max_evaluations`(null → ceil(0.2·N)), `stages.joint.e_bound_rel`(0.05), (1.0 별칭) `elastic_stage.optimizer`·`plastic_stage.optimizer`, `stagnation.{k,rel_tol}`, `abort_if_initial_all_failed` | plugins.md §4.1 |
| `budget` | `default_max_evaluations`, `max_evaluations_limit`, `default_max_wall_time_s` | api.md §4.6 |
| `alignment` | testspec.md §5 표의 키 전부 | testspec.md §5 |
| `objective` | `region_weights`, `shortfall_penalty`, `failed_run_J`, `scalar_floor`, (n차 예약) `similarity` | testspec.md §8 |
| `cases` | `dimension_merge_rel_tol` | testspec.md §9.1 |
| `identifiability` | `precheck_mode`, `min_points_per_param`, `post_fit`, `fd_step_rel`, `max_extra_evals`, `corr_threshold`, `min_sensitivity`, `max_condition` | testspec.md §9 |
| `testspec.bending` | `prefer_deflection`(기본 false — 크로스헤드 기본, 결정 2026-11-08), `min_span_ratio`, `beam_theory_span_ratio`, `overtravel_ratio`, `min_eps_p0`, `rate_from_time_channel`(true) | testspec.md §10 |
| `material` | `default_density_kg_m3`, `default_E_range_pa`, `default_sigma_y_range_pa`, `mat024.max_strain`, `mat024_coeff.default_law`, `mat024_table.nodes`, `mat024_table.first_node_strain`(0.002, 1.1), `mat024_table.smoothing_weight`(0.01, 1.2) | plugins.md §4.3, api.md §4.3 |
| `result` | `table_points`, `table_spacing`, `table_first_step`, `extrapolate_to_strain`, `extrapolation_method`, `at_bound_tol` | result.md §2–3 |
| `uncertainty` | `per_specimen_refit`, `refit_max_evaluations` | result.md §4.4 |
| `verify` | `roundtrip_enabled`, `roundtrip_tol`, `roundtrip_strain`; `explicit_check.enabled`(false), `explicit_check.elform`(1), `explicit_check.hourglass.ihq`(6), `explicit_check.hourglass.qm`(0.1), `explicit_check.loading_velocity_m_s`(0.5), `explicit_check.max_ke_ratio`(0.05), `explicit_check.max_region_error`(0.05), `explicit_check.max_peak_rel_diff`(0.05), `explicit_check.run_timeout_s`(null) | result.md §4.9·§4.12, testspec.md §8.1 |

`budget.default_max_evaluations`, `budget.max_evaluations_limit`, `default_max_wall_time_s`, `solver.run_timeout_s`는 plan 미결(해석 예산)이라 예시 파일에 자리표시값(`120`, `300`, `172800`, `3600`)과 주석 「미결」을 둔다.

## 7. 결정 추적표

decisions.md·plan.md 확정 사항이 어느 계약에 반영됐는지.

| 결정(날짜) | 반영 위치 |
| --- | --- |
| 패키지명 `matobserver` (10-24) | 이 문서 §1 |
| 1차 굽힘·2차 압축·n차, 계약 추가만·`schema_version` (10-24) | api.md §9, testspec.md §11, 각 파일 첫 줄 |
| 플러그인 5종 분리, 엔진 무수정 (10-24) | plugins.md 전체, 이 문서 §1 의존 방향 |
| 솔버 중립 1차 산출물(E, ν 입력 기록, σ–εp 테이블 + 유효 범위·시험 종류·가정·잔차), LS-DYNA 카드 2차 (10-24) | result.md §2–5 |
| 기본 카드 MAT_001(E)·MAT_024(탄소성), 그 외 옵션 (10-24) | plugins.md §4.3, result.md §2 `derived` |
| MatNexus 어휘(SI) 채택, 별도 중립 포맷 없음 (10-24) | api.md §4.5, testspec.md §10.1, result.md §7.1 |
| MatNexus 무수정, 결과 반입은 협의 (10-24) | result.md §7.3·§8, api.md §10 U-1 |
| SQLite JobStore, API·워커 분리, 워커 1개 (10-24) | api.md §5, 이 문서 §1 `jobs/`, §5.4 B-5 |
| 대용량 파일만 작업 폴더, `artifact_id`로만 제공 (10-24) | api.md §6, result.md §6, 이 문서 §4.5 |
| API `POST /v1/files`, `GET /v1/calibrations`, `GET /v1/artifacts/{id}` (10-24) | api.md §3 E2·E4·E9 |
| STP 오픈소스 메셔, 키워드 writer 자체 작성 (10-24) | plugins.md §4.4, 이 문서 §1 `mesh/`·§2 |
| 사내 배치 스크립트 래핑 (10-24) | plugins.md §4.2 `run`, 이 문서 §6 `solver.lsdyna` |
| 정렬·재샘플링 8단계 1차부터 (10-24) | testspec.md §5 |
| 고속: 시간 기준축 + explicit, n차 (10-24) | testspec.md §5(2·3·6단계), §11 |
| 압축 μ 기본 식별 변수(2차) (10-24) | api.md §4.6 Friction, testspec.md §11 |
| 레시피 `recipe_id@version` 불변, 파라미터형도 레시피 (11-08) | recipe.md §1·§5 |
| 레시피 등록·폐기 관리자 전용, 폴더+CLI+관리자 토큰 API (11-08) | recipe.md §6, api.md §2·§8 |
| 크로스헤드 기본, C 레시피 등록(식별 변수 아님), 처짐계 옵션 (11-08) | testspec.md §6–7, recipe.md §4.8, result.md §4.6 |
| 재료 슬롯 목록(모드 A·B) (11-08) | plugins.md §3, api.md §4.3, recipe.md §4.4 |
| 1차 모드 A 전체 + 모드 B 탄성, 식별성 진단, 다중 시험 동시 피팅 (11-08) | api.md §4.3 1차 제약, testspec.md §8–9, result.md §4.5, 이 문서 §5.3 O-6·O-7 |
| (plan) 입력 검증 실패 400 | api.md §4.7·§7.4 |
| (plan) 잡 상태 5개 | api.md §5 |
| (plan) 2단계 식별 E → 탄소성, BO 기본, 계수/테이블 모드 | plugins.md §3 R8·§4.3 |
| (plan) 종료 조건(예산 N, 연속 k회 정체) | plugins.md §4.1 |
| (plan) 실패 해석 페널티 J | testspec.md §8 |
| (plan) implicit 우선·수렴 실패 시 explicit 자동 전환, 1회 시간 상한 | plugins.md §4.2, recipe.md §4.2 |
| (plan) 1/4 대칭, STP 요소 수 상한 | recipe.md §4.2 `symmetry`, plugins.md §4.4 `max_elements` |
| (plan) d3plot 최적점만 보존, 작업 폴더 내부 삭제 | plugins.md §4.2 `cleanup`, 이 문서 §4.5 |
| (plan) Verifier 1요소 왕복, 외삽 표시 | result.md §3.2·§4.9 |
| (plan) 등가 물성 메타(구성·하중 모드) | result.md §4.7 |
| (plan) 대리모델 솔버 = 계약 테스트 기준 구현, 4는 대리모델로 먼저 검증 | plugins.md §2, 이 문서 §5.3 O-4·O-5 |
| (plan) MatNexus 폴링(콜백 없음) | api.md §4.9 (E5 폴링) |
| (사용자 승인 2026-11-08) explicit 재현 검사 1차 옵션, 경고 전용 | api.md §4.6.1, plugins.md §4.2 RunOverrides·S12, testspec.md §8.1, result.md §4.12, 이 문서 §4.6·§6 |
| (사용자 승인 2026-11-08) 기준 변형률속도·속도 의존 미식별 표기, n차 확장 | plugins.md §4.5 RateInfo·T9, testspec.md §10.6·§11, result.md §4.13 |
| (사용자 결정 2026-11-09) Windows 배포는 PowerShell 설치 스크립트 | deploy.md 전체, 이 문서 §1·§5.4 B-15–B-20·§6 |
| (plan) 레시피 등록 검증 항목(dry-run, 관통, 메시 수렴, 질량 스케일링·감쇠·하드코딩 재료 경고) | recipe.md §7.2 |

## 8. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| B-1 | `core/`는 Impl-Backend 소유, 첫 커밋으로 계약 타입을 옮김. 그 전 다른 에이전트는 테스트 스텁 사용 | 가정 |
| B-2 | `surrogate_beam`(대리모델 솔버)은 Impl-Optim 소유(G2를 LS-DYNA 없이 통과하기 위함). SolverAdapter 계약 테스트 파일은 Impl-Solver 소유 | 가정 |
| B-3 | TestSpec·MaterialModel은 Impl-Optim 소유(엔진과 수치 규칙을 공유) | 가정 |
| B-4 | 레시피 dry-run 검사 로직은 Impl-Solver(`verify/`), 정적 검사·저장·상태·CLI는 Impl-Backend | 가정 |
| B-5 | 단위계: Python 객체 SI 고정, .k 파일 mm·N·s·tonne 고정(설정 불가) | 가정 |
| U-1 | LS-DYNA 버전·라이선스 형태·OS 미정 → `solver.lsdyna.*` 키로만 둠 | 미결 |
| B-6 | 산출물 위치는 `work_dir/{job_id}/artifacts/` 단일, `paths.artifact_dir` 폐지 | 가정 |
| B-7 | `verify/`는 `engine.alignment`·`engine.objective` 순수 함수만 import(역방향 금지) | 가정 |
| U-2 | 해석 예산·성공 기준 임계값 — 사용자 결정 대기. §6 현재 자리표시값 유지 | 미결 |
| U-3 | 식별성 사전 점검 실패 시 거절/경고, 탄소성 기본 모드·경화식 — 사용자 결정 대기, 현재 기본값 유지(testspec.md T-5, plugins.md U-1) | 미결 |

## 변경 이력

| 버전 | 날짜 | 변경(추가·기본값 조정만, 기존 필드 의미 불변) |
| --- | --- | --- |
| 1.0 → 1.1 | 2026-11-08 | `CalibrationSpec.plasticity_mode`·`explicit_check`, `EngineServices.job_options`, 옵션 읽기 우선순위, `CalibrationCancelled`(§4.1) |
| 1.0 → 1.1 | 2026-11-08 | 설정 키 `engine.stages.*.optimizer`, `material.mat024_table.first_node_strain`(§6) |
| 1.0 → 1.1 | 2026-11-08 | 완료 테스트 M-9, S-12, O-16–O-19, B-12–B-13, 계약 테스트 범위 갱신(§5) |
| 1.1 → 1.2 | 2026-11-09 | G2 노이즈 분포 기준(O-4·O-5), 노이즈 모델·시드 목록 1–13 |
| 1.1 → 1.2 | 2026-11-09 | 설정 `engine.stages.joint.*`, `material.mat024_table.smoothing_weight`(§6), 완료 테스트 O-20·O-21 |
| 1.2 → 1.3 | 2026-11-09 | deploy.md 추가: `scripts/windows/`, CLI 운영 명령 소유(§1), 설정 `runtime`·`logging`·`doctor`·`matnexus.probe_path`·`worker.heartbeat_s`·`solver.lsdyna.executable/version_command`, `file:` 참조(§6), 완료 테스트 B-15–B-20 |
