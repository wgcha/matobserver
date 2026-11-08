"""공통 dataclass·Protocol — 계약 문서가 원본이다 (임의 변경 금지, 변경은 계약 개정 MINOR로만).

원본:
* plugins.md §1 (Theta, Stage, Issue, ParamSpec, Constraint, ParamSpace, NeutralMaterial 계열, CancelToken)
* plugins.md §4.1–4.5 (Optimizer, SolverAdapter, MaterialModel, GeometryMesher, TestSpec 와 부속 타입)
* testspec.md §3 (ExtractorRef, ResponseWindow, ResponseDef)
* module-boundaries.md §4.1 (ResolvedRecipe, ResolvedTest, ResolvedMaterials, CalibrationSpec, EngineServices)

모든 Python 객체 수치는 SI. LS-DYNA 파일(.k)만 mm·N·s·tonne 이며 변환은 파일 writer 안에서만 한다.
numpy 배열을 담는 dataclass 는 `eq=False`(배열 비교의 모호성 방지, 동일성 비교)로 둔다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

import numpy as np

if TYPE_CHECKING:  # 순환 import 방지: 타입 힌트 전용
    from .config import Config
    from .registry import Registry

Theta = Mapping[str, float]  # 전역 이름 "{slot}.{param}" 또는 "_test.{test_key}.{param}" → SI 값
Stage = Literal["elastic", "plastic"]  # 확장: "rate"(n차), "friction"(2차), "alignment"(n차 time_shift)

#: ParamSpec.name 규칙 (슬롯 내부 이름). qualify() 이후의 전역 이름에는 적용하지 않는다.
PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,15}$")


@dataclass(frozen=True)
class Issue:  # 검증 결과 1건 (api.md §7.4 코드 체계)
    code: str
    message: str
    severity: Literal["error", "warning"]
    field: str | None = None
    context: Mapping[str, Any] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class ParamSpec:
    name: str  # 슬롯 내부 이름 ^[A-Za-z][A-Za-z0-9_]{0,15}$ (예: "E", "sigma_y")
    unit: str  # SI 단위 문자열: "Pa", "1", "m/N" ...
    lower: float
    upper: float
    initial: float | None  # None이면 엔진이 TestSpec.initial_guess 또는 범위 중앙(log면 기하평균)
    scale: Literal["linear", "log"]  # 최적화기 정규화 공간
    stage: Stage
    description: str = ""


@dataclass(frozen=True)
class Constraint:
    kind: Literal["monotonic_nondecreasing", "linear_le"]
    params: tuple[str, ...]  # 같은 ParamSpace 안의 이름
    coeffs: tuple[float, ...] = ()  # linear_le: Σ coeffs[i]·θ[params[i]] ≤ bound
    bound: float = 0.0


@dataclass(frozen=True)
class ParamSpace:
    params: tuple[ParamSpec, ...]
    constraints: tuple[Constraint, ...] = ()

    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)

    def subset(self, stage: Stage) -> ParamSpace:
        """stage 태그가 같은 변수만 남긴다. 제약은 모든 변수가 남은 것만 유지한다."""
        kept = tuple(p for p in self.params if p.stage == stage)
        names = {p.name for p in kept}
        cons = tuple(c for c in self.constraints if all(n in names for n in c.params))
        return ParamSpace(params=kept, constraints=cons)

    def qualify(self, slot: str) -> ParamSpace:
        """이름 앞에 "{slot}." 부착 (제약의 변수 이름도 함께)."""
        params = tuple(replace(p, name=f"{slot}.{p.name}") for p in self.params)
        cons = tuple(replace(c, params=tuple(f"{slot}.{n}" for n in c.params)) for c in self.constraints)
        return ParamSpace(params=params, constraints=cons)


# ---- 솔버 중립 재료 (MatNexus 카드 블록 어휘) ----
@dataclass(frozen=True)
class ElasticBlock:
    youngs_modulus: float  # Pa
    poisson_ratio: float  # 1, 입력값 기록(식별 안 함)
    density: float  # kg/m^3, 입력값 기록


@dataclass(frozen=True)
class TableRow:
    plastic_strain: float  # 1 (진소성변형률)
    true_stress: float  # Pa


@dataclass(frozen=True)
class RateTableRow:  # n차 예약
    strain_rate: float  # 1/s
    plastic_strain: float
    true_stress: float


@dataclass(frozen=True)
class RateTableBlock:  # MatNexus `rate_table` 블록과 같은 {values, rows} 모양 (n차 예약)
    values: Mapping[str, Any]  # "model"("cowper_symonds"|"table"...), "cs_d"(1/s), "cs_p"(1), "rate_count"(int) 등
    rows: tuple[RateTableRow, ...]  # 속도별 표. Cowper–Symonds 요약만 있으면 빈 튜플


@dataclass(frozen=True)
class NeutralMaterial:
    elastic: ElasticBlock
    table: tuple[TableRow, ...] | None = None  # None = 탄성 전용
    rate_table: RateTableBlock | None = None  # n차
    extensions: Mapping[str, Mapping[str, Any]] = dc_field(default_factory=dict)
    # extensions 키 = "<solver>.<keyword>" (예: "lsdyna.mat187"). 솔버 전용 필드만.

    def to_card_blocks(self) -> dict[str, Any]:
        """result.md §3 card_blocks JSON. 키 집합 = {elastic, table, rate_table} (계약 테스트 M4)."""
        e = self.elastic
        table = (
            None
            if self.table is None
            else [{"plastic_strain": float(r.plastic_strain), "true_stress": float(r.true_stress)} for r in self.table]
        )
        rate_table = None
        if self.rate_table is not None:
            rate_table = {
                "values": dict(self.rate_table.values),
                "rows": [
                    {
                        "strain_rate": float(r.strain_rate),
                        "plastic_strain": float(r.plastic_strain),
                        "true_stress": float(r.true_stress),
                    }
                    for r in self.rate_table.rows
                ],
            }
        return {
            "elastic": {
                "youngs_modulus": float(e.youngs_modulus),
                "poisson_ratio": float(e.poisson_ratio),
                "density": float(e.density),
            },
            "table": table,
            "rate_table": rate_table,
        }


def neutral_material_violations(m: NeutralMaterial) -> list[str]:
    """NeutralMaterial 불변식(plugins.md §1 표, 계약 테스트 M2) 위반 설명 목록. 비어 있으면 만족.

    E>0·유한, -1<ν<0.5, ρ>0, table None 또는 ≥2행, 첫 행 εp==0.0, εp 엄격 증가, 응력 비감소·>0.
    """
    out: list[str] = []
    e = m.elastic
    if not (math.isfinite(e.youngs_modulus) and e.youngs_modulus > 0):
        out.append(f"youngs_modulus must be finite and > 0: {e.youngs_modulus!r}")
    if not (math.isfinite(e.poisson_ratio) and -1.0 < e.poisson_ratio < 0.5):
        out.append(f"poisson_ratio must satisfy -1 < nu < 0.5: {e.poisson_ratio!r}")
    if not (math.isfinite(e.density) and e.density > 0):
        out.append(f"density must be finite and > 0: {e.density!r}")
    if m.table is not None:
        rows = m.table
        if len(rows) < 2:
            out.append(f"table must have >= 2 rows (MatNexus MIN_POINTS): {len(rows)}")
        if rows and rows[0].plastic_strain != 0.0:
            out.append(f"first table row must have plastic_strain == 0.0: {rows[0].plastic_strain!r}")
        for i, r in enumerate(rows):
            if not (math.isfinite(r.plastic_strain) and math.isfinite(r.true_stress)):
                out.append(f"table row {i} is not finite")
            elif r.true_stress <= 0:
                out.append(f"table row {i}: true_stress must be > 0: {r.true_stress!r}")
            if i > 0:
                if not rows[i].plastic_strain > rows[i - 1].plastic_strain:
                    out.append(f"table row {i}: plastic_strain must be strictly increasing")
                if rows[i].true_stress < rows[i - 1].true_stress:
                    out.append(f"table row {i}: true_stress must be non-decreasing")
    return out


@runtime_checkable
class CancelToken(Protocol):  # 취소 신호 (Backend 워커가 구현, api.md §4.12)
    def is_set(self) -> bool: ...


# ======================================================================================
# testspec.md §3 응답 정의
# ======================================================================================
@dataclass(frozen=True)
class ExtractorRef:
    name: str  # §4 카탈로그
    args: Mapping[str, Any]  # 나머지 키


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
    extractor: Mapping[str, ExtractorRef]  # curve: {"x","y"} / scalar: {"value"}
    weight: float
    window: ResponseWindow


# ======================================================================================
# plugins.md §4.1 Optimizer
# ======================================================================================
@dataclass(frozen=True)
class EvalOutcome:
    J: float  # 페널티 포함 최종 값(유한)
    penalized: bool  # 해석 실패·시간 초과·범위 부족 페널티 여부
    components: Mapping[str, float] = dc_field(default_factory=dict)


@dataclass(frozen=True)
class StopSignal:
    stop: bool
    reason: Literal["converged", "exhausted", "none"]


@runtime_checkable
class Optimizer(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    max_batch: ClassVar[int]  # 한 번 ask로 낼 수 있는 최대 점 수(1차 실행은 항상 1을 요청)

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def setup(self, space: ParamSpace, seed: int, initial_points: Sequence[Theta]) -> None: ...
    def ask(self, n: int = 1) -> list[Theta]: ...
    def tell(self, thetas: Sequence[Theta], outcomes: Sequence[EvalOutcome]) -> None: ...
    def suggest_stop(self) -> StopSignal: ...
    def best(self) -> tuple[Theta, float] | None: ...
    def state(self) -> dict[str, Any]: ...  # JSON 직렬화 가능
    def restore(self, state: Mapping[str, Any]) -> None: ...


# ======================================================================================
# plugins.md §4.4 GeometryMesher (SimulationCase 가 MeshResult·JigResult 를 참조하므로 먼저 둔다)
# ======================================================================================
@dataclass(frozen=True)
class ReferencePointRequest:
    name: str  # "midspan_bottom" 등, 메시 결과 reference_nodes 키
    locate: Mapping[str, Any]  # {"x": 0.0, "y": 0.0, "z": "min"} (SI, 또는 "min"/"max")


@dataclass(frozen=True)
class GeometrySpec:
    kind: str  # "box" | "layered_box" | "stp" | (2차) "cylinder" | "frustum"
    dimensions: Mapping[str, float]  # SI m. 굽힘 box: width, thickness, total_length
    layers: tuple[tuple[str, float], ...] = ()  # (region 이름 "layer:{i}", 두께 m)
    stack_axis: Literal["z", "y"] = "z"
    stp_path: Path | None = None
    placement: Mapping[str, Any] = dc_field(default_factory=dict)  # recipe.md §4.3
    region_part_ids: Mapping[str, int] = dc_field(default_factory=dict)  # "specimen"|"layer:{i}"|"body:{i}" → PID
    reference_points: tuple[ReferencePointRequest, ...] = ()


@dataclass(frozen=True)
class MeshOptions:
    topology: Literal["hex8", "tet4", "tet10"]  # 확장: "quad4_axisym", "shell4"
    target_size_m: float
    min_elems_through_thickness: int
    max_elements: int  # mesh.max_elements; 초과 시 target_size를 키워 재시도
    size_factor: float = 1.0  # 메시 수렴 검사용 배율
    symmetry: Literal["none", "half", "quarter"] = "none"
    node_id_range: tuple[int, int] = (1, 4_999_999)
    element_id_range: tuple[int, int] = (1, 4_999_999)
    part_set_id: int = 0  # 매니페스트 specimen.part_set_id
    node_set_ids: Mapping[str, int] = dc_field(default_factory=dict)  # "sym_x0","sym_y0" → 매니페스트 값


@dataclass(frozen=True)
class MeshQuality:
    min_scaled_jacobian: float
    max_aspect_ratio: float
    elems_through_thickness: int | None


@dataclass(frozen=True)
class MeshResult:
    mesh_path: Path  # module-boundaries.md §2 .k 규격
    node_count: int
    element_count: int
    regions: Mapping[str, Mapping[str, float]]  # region → {"part_id", "element_count", "volume_m3"}
    volume_m3: float  # 메시 체적(대칭이면 전체 환산)
    reference_volume_m3: float  # 해석적 또는 CAD 체적
    bbox_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    reference_nodes: Mapping[str, int]
    node_sets: Mapping[str, int]
    top_z_at_load_m: float  # 하중선(x=0)에서 시편 윗면 z → mo_htop
    bottom_z_m: float  # → mo_hbot
    symmetry: str
    quality: MeshQuality
    target_size_used_m: float
    warnings: tuple[Issue, ...]


@dataclass(frozen=True)
class JigSpec:
    generator: str  # "jig.bending3p_rollers"
    params_si: Mapping[str, float]  # span, indenter_r, support_r, jig_len ...
    part_ids: Mapping[str, int]  # 매니페스트 jig.part_ids (SECTION_SHELL ID = part_id)
    mids: Mapping[str, int]  # 매니페스트 jig.mids (*MAT_RIGID)
    rigid: Mapping[str, float]  # {"E": Pa, "poisson": 1, "density": kg/m^3} — 매니페스트 jig.rigid, 없으면 mesh.jig.* 기본값을 엔진이 채움
    specimen: MeshResult
    symmetry: str
    node_id_range: tuple[int, int] = (5_000_000, 5_999_999)
    element_id_range: tuple[int, int] = (5_000_000, 5_999_999)


@dataclass(frozen=True)
class JigResult:
    mesh_path: Path
    reference_nodes: Mapping[str, int]  # jig.bending3p_rollers 필수: "indenter_ref"(압자 강체 노드), "support_top"(지지 롤러 강체 노드, 처짐계 기준)
    part_ids: Mapping[str, int]


@runtime_checkable
class GeometryMesher(Protocol):
    name: ClassVar[str]  # 레지스트리 이름 = kind ("box") 또는 "jig.<generator>"
    version: ClassVar[str]
    role: ClassVar[Literal["specimen", "jig"]]

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def validate(self, spec: GeometrySpec | JigSpec) -> list[Issue]: ...  # 예외 대신 Issue
    def mesh(
        self, spec: GeometrySpec | JigSpec, opts: MeshOptions | None, out_dir: Path
    ) -> MeshResult | JigResult: ...


# ======================================================================================
# plugins.md §4.2 SolverAdapter
# ======================================================================================
class RunState(str, Enum):
    NORMAL = "normal"
    ERROR = "error"
    TIMEOUT = "timeout"
    DIVERGED = "diverged"
    LICENSE_TIMEOUT = "license_timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SlotBinding:  # recipe.md §4.4 매니페스트 값
    slot: str
    part_ids: tuple[int, ...]
    mid: int
    secid: int
    section: Mapping[str, Any]  # {keyword: "SECTION_SOLID", elform: -2, ...}
    lcid: int | None  # MAT_024 LCSS 곡선 ID


@dataclass(frozen=True)
class OutputRequest:  # 엔진이 정렬 7단계로 계산(TestSpec 소관 아님)
    dt_out_s: float  # binout(RCFORC/NODOUT/GLSTAT) 간격 → mo_dtout
    dt_plot_s: float  # d3plot 간격 → mo_dtplt
    dt_max_s: float  # implicit 최대 시간 증분 ≤ dt_out_s → mo_dtmax
    history_nodes: tuple[int, ...]


@dataclass(frozen=True)
class SimulationCase:  # 엔진 → 솔버 경계 데이터 (module-boundaries.md §4)
    case_id: str  # "{test_key}-c{n}"
    test_key: str
    recipe_ref: str  # "recipe_id@version"
    recipe_dir: Path  # 등록 레시피 저장소(읽기 전용)
    manifest: Mapping[str, Any]  # 파싱된 매니페스트
    params_si: Mapping[str, float]  # 노출 파라미터(이름 → SI)
    engine_params_si: Mapping[str, Mapping[str, float]]  # 모드("implicit"/"explicit") → mo_* 예약 파라미터(SI)
    specimen_mesh: MeshResult
    jig_mesh: JigResult | None  # 파라메트릭 레시피만
    slots: tuple[SlotBinding, ...]
    responses: tuple[ResponseDef, ...]  # testspec.md §3
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
    termination_message: str  # d3hsp/messag에서 읽은 종료 문구
    solver_version: str | None  # 예 "R13.1.1 SMP d"
    license_waits: int
    warnings: tuple[Issue, ...]


@dataclass(frozen=True, eq=False)
class CurveData:
    x: np.ndarray  # SI(m 또는 s), 부호·대칭 배율 적용 완료
    y: np.ndarray  # SI(N 등)
    x_quantity: str  # "displacement" | "deflection" | "time"
    y_quantity: str  # "force"


@dataclass(frozen=True, eq=False)
class Extracted:
    curves: Mapping[str, CurveData]  # response_key → 곡선
    scalars: Mapping[str, float]  # response_key → 스칼라(SI)
    field_max: Mapping[str, Mapping[str, float]]  # slot → {"eff_plastic_strain": 최대값}
    energy: Mapping[str, float]  # {"ke_ie_ratio_max": ...} (explicit일 때)


@dataclass(frozen=True)
class SolverCapabilities:
    analysis_modes: frozenset[str]  # {"implicit", "explicit"}
    extractors: frozenset[str]  # testspec.md §4 카탈로그 중 지원 이름
    material_keywords: frozenset[str]  # {"MAT_001", "MAT_024"} — NeutralMaterial을 쓸 수 있는 키워드
    extension_keys: frozenset[str]  # 해석 가능한 NeutralMaterial.extensions 키
    unit_system: str  # 파일 단위계 "mm-N-s-tonne"
    supports_overrides: bool  # RunOverrides(요소 정식화·hourglass·질량 스케일링 금지) 지원 — explicit 재현 검사 필요조건
    card_format: str | None = None  # (1.1) render_card 출력 형식. lsdyna: "lsdyna_k", surrogate_beam: None


@dataclass(frozen=True)
class RunOverrides:  # explicit 재현 검사용 실행 덮어쓰기 (testspec.md §8.1)
    section_by_slot: Mapping[str, Mapping[str, Any]]  # slot → {elform, hourglass: {ihq, qm}}; mo_mat_<slot>.k의 *SECTION·*HOURGLASS·PART HGID로 씀
    forbid_mass_scaling: bool  # True면 제어 파일에 DT2MS≠0이 있을 때 prepare가 IncompatibleOverride 예외(덱 수정 안 함)
    engine_params_si: Mapping[str, float] | None = None  # 덮어쓸 mo_* (예: 검사 속도로 다시 계산한 mo_tend, mo_dtout)


@runtime_checkable
class SolverAdapter(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def capabilities(self) -> SolverCapabilities: ...
    def prepare(
        self,
        case: SimulationCase,
        materials: Mapping[str, NeutralMaterial],
        workdir: Path,
        mode: Literal["implicit", "explicit"] | None = None,
        overrides: RunOverrides | None = None,
    ) -> PreparedRun: ...
    # overrides가 있으면 mode="explicit" 고정, implicit→explicit fallback 없음
    def run(self, prepared: PreparedRun, ncpu: int, timeout_s: float, cancel: CancelToken) -> RunStatus: ...
    def extract(self, prepared: PreparedRun, status: RunStatus) -> Extracted: ...
    def solver_version(self) -> str | None: ...
    def cleanup(self, prepared: PreparedRun, keep: Literal["all", "results", "minimal"]) -> None: ...
    def single_element_uniaxial(
        self, material: NeutralMaterial, strain_max: float, workdir: Path, timeout_s: float
    ) -> CurveData: ...
    # 1요소 단축 덱 실행 → x=유효소성변형률[1], y=진응력[Pa] (왕복 검사용, module-boundaries.md §4.6)
    def render_card(self, neutral: NeutralMaterial, slot: SlotBinding, options: Mapping[str, Any]) -> str: ...
    # (1.1) 결과 카드 렌더링. options: {"title": str, "header_comments": list[str]}. card_format=None이면 NotImplementedError.


# ======================================================================================
# plugins.md §4.3 MaterialModel
# ======================================================================================
@dataclass(frozen=True)
class MaterialCapabilities:
    solvers: frozenset[str]  # 지원 솔버 어댑터 이름 {"lsdyna", "surrogate_beam"}
    solver_keyword: Mapping[str, str]  # {"lsdyna": "MAT_024"}
    stages: frozenset[Stage]  # mat001 → {"elastic"}
    produces_table: bool
    rate_dependent: bool  # 1차 모두 False


@runtime_checkable
class MaterialModel(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def bind(self, reference: NeutralMaterial | None) -> None: ...  # ν·ρ·기준 E 수령
    def param_space(self) -> ParamSpace: ...
    def to_neutral(self, theta_local: Mapping[str, float]) -> NeutralMaterial: ...
    def capabilities(self) -> MaterialCapabilities: ...
    def initial_guess(self, hints: Mapping[str, float]) -> Mapping[str, float]: ...
    def describe(self, theta_local: Mapping[str, float]) -> Mapping[str, Any]: ...  # 결과 메타(경화식·외삽)


# ======================================================================================
# plugins.md §4.5 TestSpec
# ======================================================================================
@dataclass(frozen=True, eq=False)
class RunRecord:  # MatNexus run 또는 inline run을 Backend가 정규화한 것
    run_id: str  # MatNexus uuid 또는 "inline:{run_key}"
    test_type_key: str  # MatNexus 키 ("flexural")
    channels: Mapping[str, np.ndarray]  # 채널 키 → SI 값 (force, displacement, deflection, time)
    specimen_dimensions: Mapping[str, float]  # SI, measured 우선
    conditions: Mapping[str, Any]
    instrument: str | None
    specimen_id: str | None
    material_id: str | None
    source_sha256: str | None
    fetched_hash: str  # 정규화 데이터 sha256
    scalars: Mapping[str, float] = dc_field(default_factory=dict)  # 스칼라 측정값(SI) 예약. 2차 "barrel_diameter" 등. 1차 빈 매핑


@dataclass(frozen=True, eq=False)
class TestData:  # TestSpec.map_run 결과
    __test__: ClassVar[bool] = False  # pytest 가 Test* 이름 클래스를 수집하지 않게 한다

    run_id: str
    x_quantity: str  # 선택된 기준축 채널 ("displacement"|"deflection"|"time")
    x: np.ndarray  # SI
    y: np.ndarray  # SI
    displacement_source: Literal["crosshead", "deflectometer", "time"]
    compliance_m_per_n: float | None  # 전달만. 보정은 엔진 정렬 1단계(c)에서 1회 (testspec.md §7)
    scalars: Mapping[str, float]  # RunRecord.scalars 중 TestSpec이 쓰는 것
    dimensions: Mapping[str, float]
    meta: Mapping[str, Any]


@dataclass(frozen=True)
class RateInfo:
    value_1_s: float | None  # 계산 불가면 None (경고 REFERENCE_RATE_UNKNOWN)
    formula: str  # 예 "6*h*V/L^2"
    inputs: Mapping[str, float]  # {"h": m, "V": m/s, "L": m}
    velocity_source: Literal["condition.test_speed", "time_channel", "none"]


@runtime_checkable
class TestSpec(Protocol):
    name: ClassVar[str]  # "bending"
    version: ClassVar[str]
    matnexus_test_type_keys: ClassVar[tuple[str, ...]]  # ("flexural",)

    def __init__(self, options: Mapping[str, Any]) -> None: ...
    def allowed_geometries(self) -> tuple[str, ...]: ...
    def required_channels(self) -> tuple[tuple[str, ...], ...]: ...  # AND of OR-groups
    def required_dimensions(self) -> tuple[str, ...]: ...
    def validate(
        self, test_input: Mapping[str, Any], runs: Sequence[RunRecord], manifest: Mapping[str, Any]
    ) -> list[Issue]: ...
    def map_run(
        self, run: RunRecord, displacement_channel: str, compliance_m_per_n: float | None
    ) -> TestData: ...  # C는 TestData에 싣기만, x 보정 금지
    def param_space(self, test_input: Mapping[str, Any], manifest: Mapping[str, Any]) -> ParamSpace: ...
    # 비재료 식별 변수(§3 R9). 1차 bending: 빈 ParamSpace
    def param_targets(self) -> Mapping[str, str]: ...  # param → "recipe:<name>" | "alignment:<name>"
    def responses(self, manifest: Mapping[str, Any], x_quantity: str) -> tuple[ResponseDef, ...]: ...
    def analysis_mode(
        self, test_input: Mapping[str, Any], data: Sequence[TestData]
    ) -> Literal["quasi_static", "dynamic"]: ...
    def reference_points(self, manifest: Mapping[str, Any]) -> tuple[ReferencePointRequest, ...]: ...
    def initial_hints(
        self, data: Sequence[TestData], geometry: GeometrySpec, params_si: Mapping[str, float]
    ) -> Mapping[str, float]: ...
    def reference_strain_rate(
        self, data: TestData, geometry: GeometrySpec, params_si: Mapping[str, float]
    ) -> RateInfo: ...
    # 공칭 기준 변형률속도 ε̇_ref (testspec.md §10.6). 계산식은 TestSpec 소관
    def engine_params(
        self, window: Mapping[str, float], mode: Literal["implicit", "explicit"], mesh: MeshResult
    ) -> Mapping[str, float]: ...
    # mo_dend, mo_tend, mo_htop, mo_hbot (SI)만. mo_dtout/mo_dtplt/mo_dtmax는 엔진이 계산 (testspec.md §5 7단계)


# ======================================================================================
# module-boundaries.md §4.1 Backend → Engine 경계 데이터
# 필드는 계약의 코드 블록·주석 그대로다. ResolvedTest/ResolvedMaterials 는 계약에 주석으로만 정의돼 있어
# 주석에 나열된 이름을 그대로 필드로 옮기고 타입은 주석에서 읽을 수 있는 범위로만 좁혔다.
# ======================================================================================
@dataclass(frozen=True)
class ResolvedRecipe:
    recipe_ref: str
    content_hash: str
    store_dir: Path  # 읽기 전용. recipes.store_dir/<recipe_id>/<version>/<verification_id>/ 중 active 시도 폴더(recipe.md §2)
    manifest: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedTest:
    test_key: str
    test_type: str
    recipe: ResolvedRecipe
    geometry: Mapping[str, Any]  # GeometrySpec 원형 dict
    runs: tuple[RunRecord, ...]
    displacement_channel: str
    recipe_params_si: Mapping[str, float]
    user_window: Mapping[str, Any]
    weight: float


@dataclass(frozen=True)
class ResolvedMaterials:
    mode: str
    slots: tuple[Mapping[str, Any], ...]  # SlotInput + 병합 옵션 + reference NeutralMaterial


@dataclass(frozen=True)
class CalibrationSpec:  # 검증·정규화 완료된 요청 (api.md §4.2 + 해석된 기본값)
    job_id: str
    schema_version: str
    tests: tuple[ResolvedTest, ...]
    materials: ResolvedMaterials
    optimizer: tuple[str, Mapping[str, Any]]  # 탄소성 단계
    elastic_optimizer: tuple[str, Mapping[str, Any]]
    solver: tuple[str, Mapping[str, Any]]
    friction: Mapping[str, Any]
    budget: Mapping[str, float]
    seed: int
    request_hash: str
    plasticity_mode: str | None = None  # (1.1) 요청 plasticity_mode (None이면 엔진이 job_options → 설정 순으로 읽음)
    explicit_check: Mapping[str, Any] | None = None  # (1.1) 요청 explicit_check 해석값


class EngineServices(Protocol):
    registry: Registry  # core.registry
    config: Config
    owner_kind: Literal["job", "recipe_verification"]
    owner_id: str  # job_id 또는 verification_id
    cancel: CancelToken
    job_options: Mapping[str, Any]  # (1.1) Backend 통로: {"plasticity_mode", "explicit_check"}

    def workdir(self, *parts: str) -> Path: ...
    # job: paths.work_dir/{job_id}/... / recipe_verification: paths.work_dir/_recipes/{verification_id}/... 생성·반환
    def add_artifact(
        self, path: Path, kind: str, *, slot: str | None = None, test_key: str | None = None
    ) -> str: ...  # artifact_id
    def record_eval(self, item: Mapping[str, Any]) -> None: ...  # api.md HistoryItem 형
    def progress(self, stage: str, **fields: Any) -> None: ...  # JobStatus.progress 갱신
