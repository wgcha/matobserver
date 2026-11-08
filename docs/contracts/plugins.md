schema_version: 1.1 · 상태: 승인(G1, MINOR 1.1) · 2026-11-08

# 플러그인 계약

근거: plan.md 「모듈 분리·교체 구조」, 「재료 슬롯과 멀티스케일 등가화」, 「시험 정의(TestSpec)」, decisions.md(플러그인 5종 분리, 슬롯 구조, 기본 카드 MAT_001/MAT_024).

식별 엔진은 아래 5개 인터페이스에만 의존한다. 하나를 바꿔도 엔진과 나머지 플러그인은 수정하지 않는다. 구현 이름은 엔진 코드에 쓰지 않고 레지스트리 설정에서만 나온다.

| 플러그인 | 레지스트리 `type` | 1차 구현(이름) | 확장 후보 |
| --- | --- | --- | --- |
| Optimizer | `optimizer` | `bo`, `lsq`, `random`(시험용) | `srsm`, 외부 라이브러리 |
| SolverAdapter | `solver` | `lsdyna`, `surrogate_beam`(기준 구현·대리모델) | 타 솔버, `surrogate_compression`(2차) |
| MaterialModel | `material_model` | `mat001`, `mat024_coeff`, `mat024_table` | `mat187`, 사용자 경화식 |
| GeometryMesher | `geometry_mesher` | `box`, `layered_box`, `stp`, `jig.bending3p_rollers` | `cylinder`, `frustum`, `axisym`(2차) |
| TestSpec | `test_spec` | `bending` | `compression`(2차), `generic`, `shear`, `bending_dynamic`(n차) |

## 1. 공통 타입 (`matobserver.core.types`)

모든 Python 객체 수치는 **SI**다. LS-DYNA 파일(.k)만 mm·N·s·tonne이며 변환은 SolverAdapter·GeometryMesher의 파일 writer 안에서만 한다([module-boundaries.md §3](module-boundaries.md)). 아래는 타입 힌트 수준 계약이다(구현은 `dataclass(frozen=True)` 권장).

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping, Protocol, Sequence, runtime_checkable
import numpy as np

Theta = Mapping[str, float]          # 전역 이름 "{slot}.{param}" 또는 "_test.{test_key}.{param}" → SI 값
Stage = Literal["elastic", "plastic"]  # 확장: "rate"(n차), "friction"(2차), "alignment"(n차 time_shift)

@dataclass(frozen=True)
class Issue:                         # 검증 결과 1건 (api.md §7.4 코드 체계)
    code: str
    message: str
    severity: Literal["error", "warning"]
    field: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ParamSpec:
    name: str                        # 슬롯 내부 이름 ^[A-Za-z][A-Za-z0-9_]{0,15}$ (예: "E", "sigma_y")
    unit: str                        # SI 단위 문자열: "Pa", "1", "m/N" ...
    lower: float
    upper: float
    initial: float | None            # None이면 엔진이 TestSpec.initial_guess 또는 범위 중앙(log면 기하평균)
    scale: Literal["linear", "log"]  # 최적화기 정규화 공간
    stage: Stage
    description: str = ""

@dataclass(frozen=True)
class Constraint:
    kind: Literal["monotonic_nondecreasing", "linear_le"]
    params: tuple[str, ...]          # 같은 ParamSpace 안의 이름
    coeffs: tuple[float, ...] = ()   # linear_le: Σ coeffs[i]·θ[params[i]] ≤ bound
    bound: float = 0.0

@dataclass(frozen=True)
class ParamSpace:
    params: tuple[ParamSpec, ...]
    constraints: tuple[Constraint, ...] = ()
    def names(self) -> tuple[str, ...]: ...
    def subset(self, stage: Stage) -> ParamSpace: ...
    def qualify(self, slot: str) -> ParamSpace: ...   # 이름 앞에 "{slot}." 부착

# ---- 솔버 중립 재료 (MatNexus 카드 블록 어휘) ----
@dataclass(frozen=True)
class ElasticBlock:
    youngs_modulus: float            # Pa
    poisson_ratio: float             # 1, 입력값 기록(식별 안 함)
    density: float                   # kg/m^3, 입력값 기록

@dataclass(frozen=True)
class TableRow:
    plastic_strain: float            # 1 (진소성변형률)
    true_stress: float               # Pa

@dataclass(frozen=True)
class RateTableRow:                  # n차 예약
    strain_rate: float               # 1/s
    plastic_strain: float
    true_stress: float

@dataclass(frozen=True)
class RateTableBlock:                # MatNexus `rate_table` 블록과 같은 {values, rows} 모양 (n차 예약)
    values: Mapping[str, Any]        # "model"("cowper_symonds"|"table"...), "cs_d"(1/s), "cs_p"(1), "rate_count"(int) 등
    rows: tuple[RateTableRow, ...]   # 속도별 표. Cowper–Symonds 요약만 있으면 빈 튜플

@dataclass(frozen=True)
class NeutralMaterial:
    elastic: ElasticBlock
    table: tuple[TableRow, ...] | None = None          # None = 탄성 전용
    rate_table: RateTableBlock | None = None           # n차
    extensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # extensions 키 = "<solver>.<keyword>" (예: "lsdyna.mat187"). 솔버 전용 필드만.
    def to_card_blocks(self) -> dict[str, Any]: ...    # result.md §3 card_blocks JSON

class CancelToken(Protocol):         # 취소 신호 (Backend 워커가 구현, api.md §4.12)
    def is_set(self) -> bool: ...

# ResponseDef / ExtractorRef / ResponseWindow 는 testspec.md §3 정의를 같은 모듈(core/types.py)에 둔다.
# MeshResult / JigResult / GeometrySpec 은 §4.4, RunRecord / TestData 는 §4.5.
```

`to_card_blocks()` 출력(MatNexus `matcore/export/dyna.py`·export-profile의 `Need(block, values)` 어휘와 동일 키):

```json
{
  "elastic": {"youngs_modulus": 2.31e9, "poisson_ratio": 0.38, "density": 1200.0},
  "table": [{"plastic_strain": 0.0, "true_stress": 4.1e7}, {"plastic_strain": 0.01, "true_stress": 4.8e7}],
  "rate_table": null
}
```

n차 `rate_table` 형(MatNexus `deck.values("rate_table")`·`deck.rows("rate_table")`와 같은 키): `{"values": {"model": "cowper_symonds", "cs_d": 40.4, "cs_p": 5.0, "rate_count": 0}, "rows": [{"strain_rate": 0.001, "plastic_strain": 0.0, "true_stress": 3.0e8}]}`.

`NeutralMaterial` 불변식(모든 MaterialModel 공통, 계약 테스트 M2):

| 규칙 | 기준 |
| --- | --- |
| E | `youngs_modulus > 0`, 유한 |
| ν | `-1 < poisson_ratio < 0.5` |
| ρ | `density > 0` |
| table 길이 | None 또는 ≥ 2행(MatNexus `MIN_POINTS = 2`) |
| 첫 행 | `plastic_strain == 0.0` (항복점, MAT_024 SIGY) |
| 변형률 | 엄격 증가 |
| 응력 | 비감소(`true_stress[i+1] ≥ true_stress[i]`), > 0 |

## 2. 레지스트리 (설정 파일)

- 위치: `registry.paths`(기본 `["config/plugins.d"]`) 아래 `*.yaml`을 이름순으로 모두 읽어 합친다. 에이전트별 파일을 나눠 충돌을 막는다: `mesh.yaml`(Impl-Mesh: geometry_mesher), `solver.yaml`(Impl-Solver: lsdyna), `optim.yaml`(Impl-Optim: optimizer, material_model, test_spec, surrogate_beam). [module-boundaries.md §1](module-boundaries.md)과 같음.
- 같은 (`type`, `name`)이 두 번 나오면 기동 실패.

```yaml
schema_version: "1.0"
plugins:
  - type: material_model            # optimizer | solver | material_model | geometry_mesher | test_spec
    name: mat024_table              # 요청·결과에 쓰는 이름 ^[a-z0-9_.]{2,40}$
    class: "matobserver.materials.mat024:Mat024Table"   # "모듈:클래스"
    version: "1.0.0"                # SemVer. 결과 traceability.plugins에 기록
    enabled: true
    aliases:                        # 선택
      - name: mat024
        when: {plasticity_mode: table}
    defaults:                       # 생성자 options 기본값(요청 options가 덮음)
      table_nodes: 8
      max_strain: 0.2
      node_spacing: geometric
```

로딩 규칙:

1. `class`를 import하고 해당 Protocol 필수 속성·메서드가 있는지 검사(`runtime_checkable` + 클래스 속성 `name`, `version`이 YAML과 일치). 불일치 시 기동 실패.
2. `enabled: false`는 로드하지만 요청에서 고르면 400 `*_UNKNOWN`.
3. 생성자 시그니처는 타입별로 고정: `Cls(options: Mapping[str, Any])`. 옵션 병합 = `defaults` ← 요청 `options`.
4. **등록 조건**: `tests/contract/`의 해당 타입 계약 테스트가 레지스트리의 `enabled: true` 항목 전부에 대해 매개변수화되어 돈다. CI(및 Verifier)에서 실패한 플러그인은 `enabled: true`로 병합할 수 없다. 대리모델 솔버 `surrogate_beam`이 SolverAdapter 계약 테스트의 기준 구현이다.

## 3. 재료 슬롯과 θ 분할 규칙

잡의 재료 = 슬롯 목록. 슬롯 = (이름, MaterialModel 인스턴스, `role` identify|fixed, 기준 카드, 파라미터 덮어쓰기). 요청 형식은 [api.md §4.3](api.md), 매니페스트 슬롯은 [recipe.md §4.4](recipe.md).

| 규칙 | 내용 |
| --- | --- |
| R1 전역 이름 | 재료 θ 키 = `"{slot}.{param}"`(슬롯 이름에 `.` 금지), 비재료 θ 키 = `"_test.{test_key}.{param}"`(R9) |
| R2 조립 | 엔진이 `identify` 슬롯마다 `model.param_space()`에 요청 `params` 덮어쓰기를 적용하고 `fixed_value`인 변수를 제거한 뒤 `qualify(slot)`해 슬롯 순서(요청 순)대로 이어 붙인다 |
| R3 단계 | 단계 `s`의 최적화 공간 = 조립 공간의 `subset(s)`. 다른 단계 변수는 그 단계의 확정값(앞 단계 최적값 또는 initial)으로 고정 |
| R4 분할 | 평가 시 θ를 접두사 `"{slot}."`로 나눠 각 슬롯 `to_neutral(θ_local)`에 넘긴다. 고정 변수는 엔진이 θ_local에 채워 넣는다(모델은 항상 자기 `param_space()` 전체 이름을 받는다) |
| R5 fixed 슬롯 | `role=fixed`면 `to_neutral`을 부르지 않고 `reference`를 `NeutralMaterial`로 그대로 쓴다. θ에 기여 0 |
| R6 결과 | 결과는 슬롯별 `card_blocks`([result.md §3](result.md)) |
| R7 제약 | 슬롯 내부 제약만 허용(슬롯 간 제약 없음, 1차) |
| R8 순서 | 2단계: `elastic` 단계(모든 identify 슬롯의 E) → `plastic` 단계(E 고정). plastic 변수가 없으면 plastic 단계 생략. **elastic 단계 해석은 탄성 전용 카드**: 엔진이 identify 슬롯 `to_neutral` 결과에서 `table`을 None으로 바꿔(MAT_001 등가) 솔버에 넘긴다(σy 초기값에 의한 E 편향 방지, [testspec.md §8](testspec.md)) |
| R9 비재료 파라미터 | θ에는 재료 외 공급자의 변수도 들어간다. 공급자 = 시험별 `TestSpec.param_space(test_input, manifest)`(레시피 매니페스트 파라미터 중 식별 대상 포함). 이름 공간 `"_test.{test_key}.{param}"`(예: `_test.t1.mu`, `_test.t1.time_shift`). 슬롯 이름은 `^[a-z]`로 시작하므로 `_` 접두사와 충돌하지 않는다. R2 조립 시 슬롯 공간 뒤에 시험 순서대로 붙이고, 단계 태그(`friction`, `alignment` 등 Stage 확장값)를 따른다. 평가 시 엔진이 `"_test.{test_key}."` 접두사로 잘라 `TestSpec.param_targets()`가 가리키는 대상(레시피 파라미터 덮어쓰기 `recipe:fric`, 정렬 보조 변수 `alignment:time_shift`)에 넣는다. **1차 bending은 빈 공간**이지만 이 경로는 계약·엔진에 존재해야 한다. 결과는 `metadata.tests[].identified_params`([result.md §4.1](result.md)) |

## 4. 인터페이스

### 4.1 Optimizer

plan: `ask() → θ 목록`, `tell(θ, J)`, `state()/restore()`.

```python
@dataclass(frozen=True)
class EvalOutcome:
    J: float                       # 페널티 포함 최종 값(유한)
    penalized: bool                # 해석 실패·시간 초과·범위 부족 페널티 여부
    components: Mapping[str, float] = field(default_factory=dict)
    # 1.1 정식화: 잔차 벡터는 키 "residual:{i:07d}"(i=0부터, 7자리 0채움)로 싣는다. 키 사전순 = 벡터 순서.
    # 그 밖의 키는 J 구성요소 등 자유. 잔차를 쓰지 않는 최적화기는 무시한다.

@dataclass(frozen=True)
class StopSignal:
    stop: bool
    reason: Literal["converged", "exhausted", "none"]

@runtime_checkable
class Optimizer(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    max_batch: ClassVar[int]         # 한 번 ask로 낼 수 있는 최대 점 수(1차 실행은 항상 1을 요청)

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def setup(self, space: ParamSpace, seed: int, initial_points: Sequence[Theta]) -> None: ...
    def ask(self, n: int = 1) -> list[Theta]: ...
    def tell(self, thetas: Sequence[Theta], outcomes: Sequence[EvalOutcome]) -> None: ...
    def suggest_stop(self) -> StopSignal: ...
    def best(self) -> tuple[Theta, float] | None: ...
    def state(self) -> dict[str, Any]: ...           # JSON 직렬화 가능
    def restore(self, state: Mapping[str, Any]) -> None: ...
```

- `space`의 이름은 전역(`slot.param`), 값은 SI 물리량. 정규화(log 포함)는 최적화기 내부 책임.
- `ask` 결과는 경계 안, 제약 만족. `initial_points`(TestSpec 해석식 초기값)는 첫 `ask`들에서 그대로 먼저 나와야 한다.
- 종료 판정의 주체는 엔진: 예산(`budget.max_evaluations`), 정체, 시간, 취소. 최적화기는 `suggest_stop`으로 수렴만 알린다.
- (1.1) 정체 판정: 단계 최적 J의 상대 개선이 최근 **k·(d+1) 평가**(k = `engine.stagnation.k`, d = 단계 변수 수) 동안 `engine.stagnation.rel_tol` 미만이면 종료. 차분 기반 최적화기의 한 반복이 d+1 평가이므로 「연속 k회」를 반복 단위로 본다.
- (1.1) 단계별 예산: plastic 단계가 있으면 elastic 단계 평가 상한 = max(1, ceil(0.3·N)) (N = `budget.max_evaluations`), 남은 평가는 plastic 단계. 진단·왕복·explicit 검사는 예산 밖.
- (1.1) 잔차 전달 규약: 엔진은 `tell`의 `EvalOutcome.components`에 잔차 벡터 r을 `"residual:{i:07d}"` 키로 싣는다. r = [testspec.md §9.3](testspec.md)의 √(scale·w_k)·(F_sim − F_exp)/‖F_exp‖_{Ω_k}을 시험·run·응답·구간 순으로 연결한 것(scale = 그 run·응답의 결합 가중, 해석 범위 밖 격자점은 F_sim = 0, w_k = 0 구간은 제외해 단계 안에서 길이 고정). `lsq`는 이 벡터가 있으면 Gauss–Newton/LM, 없으면 J 스칼라 모드. 접두사는 옵션 `residual_prefix`(기본 `"residual:"`)로만 바꿀 수 있다.
- (1.1) 단계별 기본 최적화기: `engine.stages.elastic.optimizer`(=`lsq`), `engine.stages.plastic.optimizer`(=`lsq`). BO는 옵션(요청 `optimizer: "bo"`). 근거: G2에서 Swift 계수 강상관으로 BO 테이블 오차 3–11%, lsq 1% 이내. 1.0 키 `engine.elastic_stage.optimizer`·`engine.plastic_stage.optimizer`는 호환 별칭으로 읽되 새 키가 우선.
- `bo` 기본 옵션 키: `n_initial`(기본 `max(4, 2·d)`), `acquisition`(`ei`), `kernel`(`matern52`), `xi`(0.01). `lsq` 옵션: `fd_step_rel`(0.01), `max_iter`, `ftol`.

### 4.2 SolverAdapter

plan: `prepare(model, card, workdir)`, `run(ncpu, timeout) → 상태`, `extract() → F-δ`. 상태를 객체에 숨기지 않도록 `PreparedRun` 핸들을 주고받는다.

```python
class RunState(str, Enum):
    NORMAL = "normal"; ERROR = "error"; TIMEOUT = "timeout"
    DIVERGED = "diverged"; LICENSE_TIMEOUT = "license_timeout"; CANCELLED = "cancelled"

@dataclass(frozen=True)
class SlotBinding:                 # recipe.md §4.4 매니페스트 값
    slot: str
    part_ids: tuple[int, ...]
    mid: int
    secid: int
    section: Mapping[str, Any]     # {keyword: "SECTION_SOLID", elform: -2, ...}
    lcid: int | None               # MAT_024 LCSS 곡선 ID

@dataclass(frozen=True)
class OutputRequest:               # 엔진이 정렬 7단계로 계산(TestSpec 소관 아님)
    dt_out_s: float                # binout(RCFORC/NODOUT/GLSTAT) 간격 → mo_dtout
    dt_plot_s: float               # d3plot 간격 → mo_dtplt
    dt_max_s: float                # implicit 최대 시간 증분 ≤ dt_out_s → mo_dtmax
    history_nodes: tuple[int, ...]

@dataclass(frozen=True)
class SimulationCase:              # 엔진 → 솔버 경계 데이터 (module-boundaries.md §4)
    case_id: str                   # "{test_key}-c{n}"
    test_key: str
    recipe_ref: str                # "recipe_id@version"
    recipe_dir: Path               # 등록 레시피 저장소(읽기 전용)
    manifest: Mapping[str, Any]    # 파싱된 매니페스트
    params_si: Mapping[str, float]                 # 노출 파라미터(이름 → SI)
    engine_params_si: Mapping[str, Mapping[str, float]]  # 모드("implicit"/"explicit") → mo_* 예약 파라미터(SI)
    specimen_mesh: MeshResult
    jig_mesh: JigResult | None     # 파라메트릭 레시피만
    slots: tuple[SlotBinding, ...]
    responses: tuple[ResponseDef, ...]   # testspec.md §3
    analysis_primary: Literal["implicit", "explicit"]
    analysis_fallback: Literal["explicit", "none"]
    output: OutputRequest

@dataclass(frozen=True)
class PreparedRun:
    workdir: Path
    case: SimulationCase
    main_file: Path
    mode: Literal["implicit", "explicit"]

@dataclass(frozen=True)
class RunStatus:
    state: RunState
    mode_used: Literal["implicit", "explicit"]
    fallback_used: bool
    elapsed_s: float
    termination_message: str       # d3hsp/messag에서 읽은 종료 문구
    solver_version: str | None     # 예 "R13.1.1 SMP d"
    license_waits: int
    warnings: tuple[Issue, ...]

@dataclass(frozen=True)
class CurveData:
    x: np.ndarray                  # SI(m 또는 s), 부호·대칭 배율 적용 완료
    y: np.ndarray                  # SI(N 등)
    x_quantity: str                # "displacement" | "deflection" | "time"
    y_quantity: str                # "force"

@dataclass(frozen=True)
class Extracted:
    curves: Mapping[str, CurveData]      # response_key → 곡선
    scalars: Mapping[str, float]         # response_key → 스칼라(SI)
    field_max: Mapping[str, Mapping[str, float]]  # slot → {"eff_plastic_strain": 최대값}
    energy: Mapping[str, float]          # {"ke_ie_ratio_max": ...} (explicit일 때)

@dataclass(frozen=True)
class SolverCapabilities:
    analysis_modes: frozenset[str]       # {"implicit", "explicit"}
    extractors: frozenset[str]           # testspec.md §4 카탈로그 중 지원 이름
    material_keywords: frozenset[str]    # {"MAT_001", "MAT_024"} — NeutralMaterial을 쓸 수 있는 키워드
    extension_keys: frozenset[str]       # 해석 가능한 NeutralMaterial.extensions 키
    unit_system: str                     # 파일 단위계 "mm-N-s-tonne"
    supports_overrides: bool             # RunOverrides(요소 정식화·hourglass·질량 스케일링 금지) 지원 — explicit 재현 검사 필요조건
    card_format: str | None = None       # (1.1) render_card 출력 형식. lsdyna: "lsdyna_k", surrogate_beam: None(카드 산출물 없음)

@dataclass(frozen=True)
class RunOverrides:                      # explicit 재현 검사용 실행 덮어쓰기 (testspec.md §8.1)
    section_by_slot: Mapping[str, Mapping[str, Any]]   # slot → {elform, hourglass: {ihq, qm}}; mo_mat_<slot>.k의 *SECTION·*HOURGLASS·PART HGID로 씀
    forbid_mass_scaling: bool            # True면 제어 파일에 DT2MS≠0이 있을 때 prepare가 IncompatibleOverride 예외(덱 수정 안 함)
    engine_params_si: Mapping[str, float] | None = None # 덮어쓸 mo_* (예: 검사 속도로 다시 계산한 mo_tend, mo_dtout)

@runtime_checkable
class SolverAdapter(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def capabilities(self) -> SolverCapabilities: ...
    def prepare(self, case: SimulationCase, materials: Mapping[str, NeutralMaterial],
                workdir: Path, mode: Literal["implicit", "explicit"] | None = None,
                overrides: RunOverrides | None = None) -> PreparedRun: ...
        # overrides가 있으면 mode="explicit" 고정, implicit→explicit fallback 없음
    def run(self, prepared: PreparedRun, ncpu: int, timeout_s: float,
            cancel: CancelToken) -> RunStatus: ...
    def extract(self, prepared: PreparedRun, status: RunStatus) -> Extracted: ...
    def solver_version(self) -> str | None: ...
    def cleanup(self, prepared: PreparedRun, keep: Literal["all", "results", "minimal"]) -> None: ...
    def single_element_uniaxial(self, material: NeutralMaterial, strain_max: float,
                                workdir: Path, timeout_s: float) -> CurveData: ...
        # 1요소 단축 덱 실행 → x=유효소성변형률[1], y=진응력[Pa] (왕복 검사용, module-boundaries.md §4.6)
    def render_card(self, neutral: NeutralMaterial, slot: SlotBinding,
                    options: Mapping[str, Any]) -> str: ...
        # (1.1) 결과 카드 렌더링. 입력은 결과 NeutralMaterial(유효 범위 절단·외삽 행 반영 후, result.md §3.2).
        # 덱 조립과 같은 재료 writer(deck/materials) 재사용. mm·N·s·tonne, 머리 주석에 단위·슬롯·출처.
        # options: {"title": str, "header_comments": list[str]}. card_format=None이면 NotImplementedError.
```

`cleanup` keep 단계와 설정 `solver.keep_d3plot` 매핑:

| keep | 남기는 것 | 지우는 것 |
| --- | --- | --- |
| `all` | 전부 | 없음 |
| `results` | 덱·mo_*.k·binout·d3hsp·messag·d3plot | d3dump·runrsf·재시작·임시 파일 |
| `minimal` | 덱·mo_*.k·binout·d3hsp·messag | `results`에서 지우는 것 + d3plot |

| `solver.keep_d3plot` | 최적점 평가 | 그 외 평가 |
| --- | --- | --- |
| `best` (기본) | `results` | `minimal` |
| `all` | `results` | `results` |
| `none` | `minimal` | `minimal` |

`solver.keep_all_files: true`(기본 false, 디버그용)이면 위 표와 무관하게 모두 `all`.

- `materials`는 슬롯 이름 → `NeutralMaterial`. 키워드 작성(*PART, *SECTION, *MAT, *DEFINE_CURVE)은 어댑터 책임. `NeutralMaterial.extensions`에 지원하지 않는 키가 있으면 `IncompatibleMaterial` 예외.
- `run`: 사내 배치 실행을 `solver.lsdyna.batch_command`(설정, 인자 템플릿 리스트)로 감싸 호출. 라이선스 오류는 `solver.license_retry.interval_s` 간격으로 `max_wait_s`까지 재시도. 시간 초과는 강제 종료 후 `TIMEOUT`. implicit 비정상 종료이고 `case.analysis_fallback == "explicit"`이면 같은 작업 폴더 하위 `explicit/`에서 자동 재실행하고 `fallback_used=True`.
- `extract`는 `NORMAL`이 아니어도 부분 곡선을 돌려줄 수 있다(범위 부족 페널티 계산용). 곡선이 없으면 빈 배열.
- `cleanup`: 작업 폴더 **내부에서만** 삭제. 엔진이 위 매핑표대로 keep을 고른다.
- `CancelToken`: `is_set() -> bool` 하나. 어댑터는 `solver.poll_interval_s` 간격으로 확인해 프로세스를 종료하고 `CANCELLED`.

### 4.3 MaterialModel

plan: `param_space()`(이름·범위·제약), `to_neutral(θ)`(E, ν, σ-εp 테이블), `capabilities()`.

```python
@dataclass(frozen=True)
class MaterialCapabilities:
    solvers: frozenset[str]              # 지원 솔버 어댑터 이름 {"lsdyna", "surrogate_beam"}
    solver_keyword: Mapping[str, str]    # {"lsdyna": "MAT_024"}
    stages: frozenset[Stage]             # mat001 → {"elastic"}
    produces_table: bool
    rate_dependent: bool                 # 1차 모두 False

@runtime_checkable
class MaterialModel(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def bind(self, reference: NeutralMaterial | None) -> None: ...   # ν·ρ·기준 E 수령
    def param_space(self) -> ParamSpace: ...
    def to_neutral(self, theta_local: Mapping[str, float]) -> NeutralMaterial: ...
    def capabilities(self) -> MaterialCapabilities: ...
    def initial_guess(self, hints: Mapping[str, float]) -> Mapping[str, float]: ...
    def describe(self, theta_local: Mapping[str, float]) -> Mapping[str, Any]: ...  # 결과 메타(경화식·외삽)
```

(1.1) 엔진 호출 순서: `__init__(options)` → `bind(reference)` → `initial_guess(hints)` → `param_space()` → `to_neutral(θ)`… `param_space()`의 기본 범위·initial은 `bind`·`initial_guess`에서 받은 값으로 정해지므로 이 순서를 지킨다.

`hints` 키(TestSpec이 채움, SI): `E0`(해석식 E), `sigma_y0`(항복 추정), `eps_p_max0`(최대 소성변형률 추정). 없는 키는 무시. 힌트가 없으면(STP 형상) 기본 범위는 설정 `material.default_E_range_pa`([1e6, 1e12], log)와 `material.default_sigma_y_range_pa`([1e5, 5e9], log)를 쓰고, initial은 기준 카드(`reference`) 값, 그것도 없으면 log 중앙값.

1차 구현 파라미터 이름(요청 `params`와 결과 `identified_params`에서 그대로 쓴다):

| 모델 | 단계 | 파라미터(단위) | 기본 범위 | 비고 |
| --- | --- | --- | --- | --- |
| `mat001` | elastic | `E`(Pa) | [E0/10, E0·10], log | table=None. 취성(유리·실리콘·IC PKG 등가) |
| `mat024_coeff` | elastic | `E`(Pa) | 같음 | |
| | plastic | `hardening_law=swift`: `K`(Pa), `eps0`(1), `n`(1) | K [σy0, 20σy0], eps0 [1e-4, 0.05] log, n [0.01, 0.8] | σ = K(ε0+εp)^n. SIGY = K·ε0^n |
| | plastic | `hardening_law=voce`: `sigma_y`(Pa), `Q`(Pa), `b`(1) | σy [0.3σy0, 3σy0], Q [0, 5σy0], b [1, 500] log | σ = σy + Q(1−e^(−bεp)) |
| | plastic | `hardening_law=swift_voce`: 위 둘 + `alpha`(1) | alpha [0,1] | σ = α·Swift + (1−α)·Voce |
| `mat024_table` | elastic | `E`(Pa) | 같음 | |
| | plastic | `sigma_y`(Pa), `dsig_1..dsig_m`(Pa) | σy [0.3σy0, 3σy0], dsig_i [0, 2σy0] | 노드 응력 = σy + Σ_{j≤i} dsig_j → 단조 증가를 변수 정의로 보장. 노드 변형률은 옵션 |

옵션 키:

| 모델 | 옵션 | 기본 | 설명 |
| --- | --- | --- | --- |
| `mat024_coeff` | `hardening_law` | `material.mat024_coeff.default_law`(=`swift`) | `swift`\|`voce`\|`swift_voce` |
| | (1.1 주의) | — | 경화식 개별 계수(K·ε0·n 등)는 강상관으로 **비유일할 수 있다**. 결과 판정·불확도·외삽 판단은 계수가 아니라 샘플링된 테이블(`card_blocks.table`) 기준([result.md §2](result.md)) |
| | `table_points` | `result.table_points`(=50) | to_neutral 테이블 샘플 수 |
| | `max_strain` | `material.mat024.max_strain`(=0.2) | 샘플링 상한(유효 범위와 별개, 결과에서 잘림 [result.md §3.2](result.md)) |
| `mat024_table` | `table_nodes` | `material.mat024_table.nodes`(=8) | m+1개 노드(εp=0 포함) |
| | `node_spacing` | `geometric` | `geometric`\|`linear`\|`explicit` |
| | `strain_nodes` | — | `explicit`일 때 εp 리스트(0으로 시작, 엄격 증가) |
| | `first_node_strain` | `material.mat024_table.first_node_strain`(=0.002) | (1.1) `geometric` 배치의 첫 비영(非零) 노드 εp. 이후 노드는 `max_strain`까지 등비 |
| | `max_strain` | 0.2 | 마지막 노드 εp |

### 4.4 GeometryMesher

```python
@dataclass(frozen=True)
class ReferencePointRequest:
    name: str                       # "midspan_bottom" 등, 메시 결과 reference_nodes 키
    locate: Mapping[str, Any]       # {"x": 0.0, "y": 0.0, "z": "min"} (SI, 또는 "min"/"max")

@dataclass(frozen=True)
class GeometrySpec:
    kind: str                       # "box" | "layered_box" | "stp" | (2차) "cylinder" | "frustum"
    dimensions: Mapping[str, float] # SI m. 굽힘 box: width, thickness, total_length
    layers: tuple[tuple[str, float], ...] = ()   # (region 이름 "layer:{i}", 두께 m)
    stack_axis: Literal["z", "y"] = "z"
    stp_path: Path | None = None
    placement: Mapping[str, Any] = field(default_factory=dict)   # recipe.md §4.3
    region_part_ids: Mapping[str, int] = field(default_factory=dict)  # "specimen"|"layer:{i}"|"body:{i}" → PID
    reference_points: tuple[ReferencePointRequest, ...] = ()

@dataclass(frozen=True)
class MeshOptions:
    topology: Literal["hex8", "tet4", "tet10"]   # 확장: "quad4_axisym", "shell4"
    target_size_m: float
    min_elems_through_thickness: int
    max_elements: int               # mesh.max_elements; 초과 시 target_size를 키워 재시도
    size_factor: float = 1.0        # 메시 수렴 검사용 배율
    symmetry: Literal["none", "half", "quarter"] = "none"
    node_id_range: tuple[int, int] = (1, 4_999_999)
    element_id_range: tuple[int, int] = (1, 4_999_999)
    part_set_id: int = 0            # 매니페스트 specimen.part_set_id
    node_set_ids: Mapping[str, int] = field(default_factory=dict)  # "sym_x0","sym_y0" → 매니페스트 값

@dataclass(frozen=True)
class MeshQuality:
    min_scaled_jacobian: float
    max_aspect_ratio: float
    elems_through_thickness: int | None

@dataclass(frozen=True)
class MeshResult:
    mesh_path: Path                 # module-boundaries.md §2 .k 규격
    node_count: int
    element_count: int
    regions: Mapping[str, Mapping[str, float]]   # region → {"part_id", "element_count", "volume_m3"}
    volume_m3: float                # 메시 체적(대칭이면 전체 환산)
    reference_volume_m3: float      # 해석적 또는 CAD 체적
    bbox_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    reference_nodes: Mapping[str, int]
    node_sets: Mapping[str, int]
    top_z_at_load_m: float          # 하중선(x=0)에서 시편 윗면 z → mo_htop
    bottom_z_m: float               # → mo_hbot
    symmetry: str
    quality: MeshQuality
    target_size_used_m: float
    warnings: tuple[Issue, ...]          # (1.1) 품질 임계 미달은 MESH_QUALITY_LOW(경고)로 여기에

@dataclass(frozen=True)
class JigSpec:
    generator: str                  # "jig.bending3p_rollers"
    params_si: Mapping[str, float]  # span, indenter_r, support_r, jig_len ...
    part_ids: Mapping[str, int]     # 매니페스트 jig.part_ids (SECTION_SHELL ID = part_id)
    mids: Mapping[str, int]         # 매니페스트 jig.mids (*MAT_RIGID)
    rigid: Mapping[str, float]      # {"E": Pa, "poisson": 1, "density": kg/m^3} — 매니페스트 jig.rigid, 없으면 mesh.jig.* 기본값을 엔진이 채움
    specimen: MeshResult
    symmetry: str
    node_id_range: tuple[int, int] = (5_000_000, 5_999_999)
    element_id_range: tuple[int, int] = (5_000_000, 5_999_999)

@dataclass(frozen=True)
class JigResult:
    mesh_path: Path
    reference_nodes: Mapping[str, int]   # jig.bending3p_rollers 필수: "indenter_ref"(압자 강체 노드), "support_top"(지지 롤러 강체 노드, 처짐계 기준)
    part_ids: Mapping[str, int]

@runtime_checkable
class GeometryMesher(Protocol):
    name: ClassVar[str]             # 레지스트리 이름 = kind ("box") 또는 "jig.<generator>"
    version: ClassVar[str]
    role: ClassVar[Literal["specimen", "jig"]]
    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def validate(self, spec: GeometrySpec | JigSpec) -> list[Issue]: ...   # 예외 대신 Issue
    def mesh(self, spec: GeometrySpec | JigSpec, opts: MeshOptions | None, out_dir: Path) -> MeshResult | JigResult: ...
```

- 시편 메시는 θ와 무관하므로 케이스당 1회 생성해 모든 평가에서 재사용한다.
- 결정성: 같은 입력이면 바이트 동일 파일(gmsh 시드·스레드 1 고정).

### 4.5 TestSpec

```python
@dataclass(frozen=True)
class RunRecord:                     # MatNexus run 또는 inline run을 Backend가 정규화한 것
    run_id: str                      # MatNexus uuid 또는 "inline:{run_key}"
    test_type_key: str               # MatNexus 키 ("flexural")
    channels: Mapping[str, np.ndarray]   # 채널 키 → SI 값 (force, displacement, deflection, time)
    specimen_dimensions: Mapping[str, float]   # SI, measured 우선
    conditions: Mapping[str, Any]
    instrument: str | None
    specimen_id: str | None
    material_id: str | None
    source_sha256: str | None
    fetched_hash: str                # 정규화 데이터 sha256
    scalars: Mapping[str, float] = field(default_factory=dict)   # 스칼라 측정값(SI) 예약. 2차 "barrel_diameter" 등. 1차 빈 매핑

@dataclass(frozen=True)
class TestData:                      # TestSpec.map_run 결과
    run_id: str
    x_quantity: str                  # 선택된 기준축 채널 ("displacement"|"deflection"|"time")
    x: np.ndarray                    # SI
    y: np.ndarray                    # SI
    displacement_source: Literal["crosshead", "deflectometer", "time"]
    compliance_m_per_n: float | None # 전달만. 보정은 엔진 정렬 1단계(c)에서 1회 (testspec.md §7)
    scalars: Mapping[str, float]     # RunRecord.scalars 중 TestSpec이 쓰는 것
    dimensions: Mapping[str, float]
    meta: Mapping[str, Any]

@runtime_checkable
class TestSpec(Protocol):
    name: ClassVar[str]                              # "bending"
    version: ClassVar[str]
    matnexus_test_type_keys: ClassVar[tuple[str, ...]]   # ("flexural",)
    load_mode: ClassVar[str]                             # (1.1) result.md §4.7 equivalence.load_mode (bending: "three_point_bending")
    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def allowed_geometries(self) -> tuple[str, ...]: ...
    def required_channels(self) -> tuple[tuple[str, ...], ...]: ...   # AND of OR-groups
    def required_dimensions(self) -> tuple[str, ...]: ...
    def validate(self, test_input: Mapping[str, Any], runs: Sequence[RunRecord],
                 manifest: Mapping[str, Any]) -> list[Issue]: ...
    def map_run(self, run: RunRecord, displacement_channel: str,
                compliance_m_per_n: float | None) -> TestData: ...   # C는 TestData에 싣기만, x 보정 금지
    def param_space(self, test_input: Mapping[str, Any], manifest: Mapping[str, Any]) -> ParamSpace: ...
        # 비재료 식별 변수(§3 R9). 1차 bending: 빈 ParamSpace
    def param_targets(self) -> Mapping[str, str]: ...   # param → "recipe:<name>" | "alignment:<name>"
    def responses(self, manifest: Mapping[str, Any], x_quantity: str) -> tuple[ResponseDef, ...]: ...
    def analysis_mode(self, test_input: Mapping[str, Any], data: Sequence[TestData]) -> Literal["quasi_static", "dynamic"]: ...
    def reference_points(self, manifest: Mapping[str, Any]) -> tuple[ReferencePointRequest, ...]: ...
    def initial_hints(self, data: Sequence[TestData], geometry: GeometrySpec,
                      params_si: Mapping[str, float]) -> Mapping[str, float]: ...
    def reference_strain_rate(self, data: TestData, geometry: GeometrySpec,
                              params_si: Mapping[str, float]) -> RateInfo: ...
        # 공칭 기준 변형률속도 ε̇_ref (testspec.md §10.6). 계산식은 TestSpec 소관
    def engine_params(self, window: Mapping[str, float], mode: Literal["implicit", "explicit"],
                      mesh: MeshResult) -> Mapping[str, float]: ...
        # mo_dend, mo_tend, mo_htop, mo_hbot (SI)만. mo_dtout/mo_dtplt/mo_dtmax는 엔진이 계산 (testspec.md §5 7단계)
```

```python
@dataclass(frozen=True)
class RateInfo:
    value_1_s: float | None              # 계산 불가면 None (경고 REFERENCE_RATE_UNKNOWN)
    formula: str                         # 예 "6*h*V/L^2"
    inputs: Mapping[str, float]          # {"h": m, "V": m/s, "L": m}
    velocity_source: Literal["condition.test_speed", "time_channel", "none"]
```

정렬·재샘플링·목적함수는 엔진 공통 기능이며 TestSpec은 응답 정의와 데이터 매핑만 제공한다([testspec.md](testspec.md)).

## 5. 계약 테스트 (`tests/contract/`)

모든 항목은 레지스트리의 `enabled: true` 구현마다 매개변수화한다. 픽스처는 `tests/fixtures/`(MatNexus 1요소 덱 `dp600-1elem-lsdyna.k` 사본, flexural run JSON 사본 포함).

### 5.1 Optimizer

| ID | 항목 | 판정 |
| --- | --- | --- |
| O1 | `ask` 결과가 경계·제약 만족 | 1000점 중 위반 0 |
| O2 | `initial_points`가 첫 ask에서 순서대로 나옴 | 동일 |
| O3 | `tell`에 `penalized=True`·큰 J 섞어도 예외 없음 | 통과 |
| O4 | 같은 seed → 같은 ask 수열 | 처음 20점 비트 동일 |
| O5 | `state()`가 JSON 직렬화 가능, `restore` 후 다음 ask 동일 | 비트 동일 |
| O6 | 2D 이차 함수(최소점 경계 내부)에서 수렴 | `bo`: 40평가 내 J < 1e-3 / `lsq`: 20평가 내 |
| O7 | log 스케일 변수 경계 [1e6, 1e12]에서 정규화 동작 | 수렴(O6 기준) |
| O8 | `max_batch` 초과 n 요청 시 `ValueError` | 발생 |
| O9 | (1.1) `components`의 `residual:` 키가 있으면 사용, 없으면 스칼라 모드로 동작(`lsq`), 다른 최적화기는 무시해도 예외 없음 | 통과 |

### 5.2 SolverAdapter

| ID | 항목 | 판정 |
| --- | --- | --- |
| S1 | `capabilities()` 필드 채움, `unit_system=="mm-N-s-tonne"` | 통과 |
| S2 | `prepare`가 `workdir` 밖에 쓰지 않음, 레시피 저장소 수정 없음 | 파일시스템 감시로 0건 |
| S3 | `prepare` 결정성: 같은 입력 → 같은 파일 바이트 | 해시 동일 |
| S4 | 정상 덱 → `NORMAL`, 오류 덱 → `ERROR`, 시간 초과 → `TIMEOUT` (timeout_s + `solver.kill_grace_s` 이내) | 통과 |
| S5 | `extract` 곡선 SI, 하중 구간 x 단조, 힘 양수(압입 방향), 대칭 배율 적용 | 기준 곡선 대비 상대오차 ≤1e-6(`surrogate_beam`), LS-DYNA는 기준 binout 픽스처로 |
| S6 | 재료 왕복: `NeutralMaterial` → 키워드 → 파서로 다시 읽기 | E·ν·ρ·테이블 상대오차 ≤ 1e-6, 단위 변환(Pa→MPa, kg/m³→tonne/mm³) 확인 |
| S7 | 라이선스 오류 모사 → 재시도 후 `LICENSE_TIMEOUT` | 통과 |
| S8 | 미지원 `extensions` → `IncompatibleMaterial` | 통과 |
| S9 | `CancelToken` 설정 → `poll_interval_s`+grace 내 `CANCELLED` | 통과 |
| S10 | `cleanup`이 작업 폴더 밖 삭제 없음, keep 단계별 남는 파일이 매핑표와 일치 | 0건, 일치 |
| S11 | `single_element_uniaxial`: MAT_024 테이블 입력 → 출력 응력이 입력 테이블 재현 | 최대 상대오차 ≤ `verify.roundtrip_tol` |
| S12 | `supports_overrides=True`면 `RunOverrides` 반영(*SECTION elform·*HOURGLASS·HGID가 덱에 기록), 질량 스케일링 제어 파일 + `forbid_mass_scaling` → `IncompatibleOverride`, explicit 고정·fallback 없음 | 통과 |
| S13 | (1.1) `render_card`: 결과 NeutralMaterial → 카드 → `.k` 리더 재파싱 시 E·ν·ρ·테이블 상대오차 ≤1e-6, 테이블 마지막 행 = 입력 마지막 행(절단·외삽 반영값). `card_format=None`이면 `NotImplementedError` | 통과 |

### 5.3 MaterialModel

| ID | 항목 | 판정 |
| --- | --- | --- |
| M1 | `param_space()` 이름 고유, lower<upper, initial 경계 내, stage 태그 ⊆ `capabilities().stages` | 통과 |
| M2 | 경계·초기값·랜덤 500점에서 `to_neutral` 결과가 §1 불변식 만족 | 위반 0 |
| M3 | 결정성 | 동일 |
| M4 | `to_card_blocks()` 키 집합 = {`elastic`, `table`, `rate_table`}, `elastic` 키 = {`youngs_modulus`,`poisson_ratio`,`density`}, 행 키 = {`plastic_strain`,`true_stress`} | 정확히 일치 |
| M5 | ν·ρ는 `bind(reference)` 값 그대로 | 동일 |
| M6 | `produces_table=False`면 table None | 통과 |
| M7 | `mat024_coeff`: Swift/Voce 해석식 값과 테이블 점 일치 | 상대오차 ≤1e-9 |
| M8 | `mat024_table`: 임의 θ에서 단조성(변수 정의로 보장) | 위반 0 |
| M9 | (1.1) 호출 순서 `bind → initial_guess → param_space`에서 initial이 경계 안, 순서를 지키면 결정적 | 통과 |

### 5.4 GeometryMesher

| ID | 항목 | 판정 |
| --- | --- | --- |
| G1 | 체적 오차 | box/layered_box ≤ `mesh.volume_rel_tol_parametric`(1e-9), stp ≤ `mesh.volume_rel_tol_stp`(0.01) |
| G2 | 품질 | min scaled Jacobian ≥ `mesh.min_scaled_jacobian`(0.2), aspect ≤ `mesh.max_aspect_ratio`(10), 두께 방향 요소 ≥ `min_elems_through_thickness` |
| G3 | `region_part_ids`대로 요소 PID 부여, 모든 요소가 어떤 region에 속함 | 통과 |
| G4 | `reference_nodes` 위치가 요청 locate와 ≤ 0.5·target_size | 통과 |
| G5 | .k가 [module-boundaries.md §2](module-boundaries.md) 규격(허용 키워드만, 고정 폭, mm) | 리더로 재파싱 성공 |
| G6 | ID가 지정 범위 안 | 통과 |
| G7 | 대칭면 노드 세트: 세트 노드 전부 평면 위(|좌표| ≤ 1e-9 m) | 통과 |
| G8 | 결정성 | 바이트 동일 |
| G9 | 잘못된 spec → `validate`가 Issue 반환(예외 없음) | 통과 |
| G10 | `max_elements` 초과 입력 → 조대화 후 상한 이하 + 경고 `MESH_COARSENED` | 통과 |
| G11 | `layered_box`: 레이어별 체적 합 = 전체, 계면 노드 공유(연속 메시) | 통과 |
| G12 | (1.1) 품질 임계(`mesh.min_scaled_jacobian`·`mesh.max_aspect_ratio`) 미달이지만 유효한(뒤집힘 없는) 메시 → 실패가 아니라 `MeshResult.warnings`에 `MESH_QUALITY_LOW`. scaled Jacobian ≤ 0(뒤집힘)은 `mesh` 실패(엔진 `MESH_FAILED`) | 통과 |

### 5.5 TestSpec

| ID | 항목 | 판정 |
| --- | --- | --- |
| T1 | flexural run 픽스처 → `map_run` 결과 SI, 채널 선택 규칙([testspec.md §6](testspec.md)) | 통과 |
| T2 | 채널·치수 누락 → 해당 코드(`CHANNEL_MISSING` 등) Issue | 통과 |
| T3 | `responses()`의 extractor 이름 ⊆ 선택 솔버 `capabilities().extractors` | 통과 |
| T4 | `allowed_geometries()` 원소가 모두 레지스트리에 있음 | 통과 |
| T5 | 합성 선형 곡선(보 이론)에서 `initial_hints["E0"]` 복원 | 상대오차 ≤ 1e-3 |
| T6 | `map_run`이 컴플라이언스를 적용하지 않음(x = 원 채널, `compliance_m_per_n` 전달) — 이중 적용 방지 | x 비트 동일 |
| T8 | `param_space()`가 ParamSpace를 돌려주고(bending은 빈 공간) `param_targets()` 키와 일치 | 통과 |
| T9 | `reference_strain_rate`: bending 합성 입력(h, V, L)에서 6hV/L² 일치, test_speed 없고 time 채널 있으면 시간 채널 추정, 둘 다 없으면 `value_1_s=None` | 상대오차 ≤1e-12 / 규칙 일치 |
| T10 | (1.1) 클래스 속성 `load_mode`가 result.md §4.7 enum 값 | 통과 |
| T7 | 결정성 | 동일 |

## 6. 가정·미결

| ID | 내용 | 종류 |
| --- | --- | --- |
| P-1 | 플러그인 Python 객체는 SI, .k 파일만 mm·N·s·tonne. 변환 책임은 파일 writer(SolverAdapter·GeometryMesher) | 가정 |
| P-2 | plan의 `prepare/run/extract`를 `PreparedRun` 핸들 방식으로 구체화(무상태 어댑터). implicit→explicit 자동 전환은 어댑터 내부 | 가정 |
| P-3 | 레지스트리 이름은 `mat024_coeff`/`mat024_table` 두 개, `mat024`는 `plasticity_mode`로 고르는 별칭 | 가정 |
| P-4 | `mat024_table`은 증분 변수(dsig)로 단조 제약을 변수 정의에 넣음(최적화기 제약 처리 의존 제거) | 가정 |
| P-5 | 탄성 단계 기본 최적화기는 `lsq`(1D·다슬롯 공통). plan의 「FE 1D 탐색」을 포함 | 가정 |
| P-6 | 지그 강체 메시 생성기를 GeometryMesher(`role=jig`)로 둠 | 가정 |
| P-7 | 플러그인 등록 조건(계약 테스트 통과)은 CI·Verifier 단계에서 강제, 런타임은 인터페이스 형태만 검사 | 가정 |
| P-8 | 비재료 식별 변수는 TestSpec이 공급하고 이름 공간 `_test.{test_key}.`로 θ에 합류(1차 빈 공간) | 가정 |
| P-9 | 탄성 단계는 identify 슬롯을 탄성 전용 카드로 해석 | 가정 |
| P-10 | explicit 재현 검사는 `prepare(..., overrides=RunOverrides)` 한 경로로 처리(새 모드 없음). 질량 스케일링이 있는 레시피는 덱을 고치지 않고 검사 불가로 처리 | 가정 |
| U-1 | 탄소성 기본 모드(coeff/table)와 기본 경화식 — 사용자 결정 대기. 현재 기본값(`engine.default_plasticity_mode`, `material.mat024_coeff.default_law`=`swift`) 유지 | 미결 |

## 변경 이력

| 버전 | 날짜 | 변경(추가·기본값 조정만, 기존 필드 의미 불변) |
| --- | --- | --- |
| 1.0 → 1.1 | 2026-11-08 | `EvalOutcome.components` 잔차 키 `residual:{i:07d}` 정식화, 잔차 전달 규약(§4.1) |
| 1.0 → 1.1 | 2026-11-08 | 정체 판정 k·(d+1), elastic 예산 상한 ceil(0.3·N), 단계별 기본 최적화기 lsq·BO 옵션(§4.1) |
| 1.0 → 1.1 | 2026-11-08 | `SolverCapabilities.card_format`, `SolverAdapter.render_card`(§4.2), 계약 S13 |
| 1.0 → 1.1 | 2026-11-08 | MaterialModel 호출 순서 bind→initial_guess→param_space(§4.3), M9 |
| 1.0 → 1.1 | 2026-11-08 | `mat024_table.first_node_strain`(0.002), 계수 비유일 주의(§4.3) |
| 1.0 → 1.1 | 2026-11-08 | `MESH_QUALITY_LOW` 경고(§4.4), G12 |
| 1.0 → 1.1 | 2026-11-08 | TestSpec `load_mode` 클래스 속성(§4.5), T10, 계약 O9 |
