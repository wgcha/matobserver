"""요청·응답 스키마 (api.md §3–§8). 요청은 엄격(`extra="forbid"`), 응답에 모르는 필드는 클라이언트가 무시한다.

수치는 모두 SI. 요청 모델의 숫자는 JSON 숫자만 받는다(문자열 숫자 거부, bool 거부).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

SCHEMA_VERSION = "1.0"
SCHEMA_MAJOR, SCHEMA_MINOR = 1, 0

Number = Annotated[float, Field(strict=True)]  # int 허용(JSON 정수), 문자열·bool 거부

TEST_KEY_RE = r"^[a-z0-9_]{1,16}$"
SLOT_RE = r"^[a-z][a-z0-9_]{0,15}$"
RECIPE_REF_RE = r"^[a-z0-9][a-z0-9-]{2,63}@[1-9][0-9]*$"
RUN_KEY_RE = r"^[A-Za-z0-9_.-]{1,64}$"
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------- 요청: 시험
class InlineChannel(_Req):
    unit: StrictStr
    values: list[Number]


class InlineRun(_Req):
    run_key: StrictStr = Field(pattern=RUN_KEY_RE)
    test_type_key: StrictStr
    channels: dict[StrictStr, InlineChannel]
    specimen_dimensions: dict[StrictStr, Number] | None = None
    conditions: dict[StrictStr, Any] | None = None
    instrument: StrictStr | None = None
    specimen_ref: StrictStr | None = None


class Layer(_Req):
    slot: StrictStr = Field(pattern=SLOT_RE)
    thickness: Number


class Placement(_Req):
    mode: Literal["auto", "explicit"] = "auto"
    translate: Annotated[list[Number], Field(min_length=3, max_length=3)] | None = None
    rotate_deg: Annotated[list[Number], Field(min_length=3, max_length=3)] | None = None


class BodySlot(_Req):
    body: StrictInt | StrictStr
    slot: StrictStr = Field(pattern=SLOT_RE)


class GeometryInput(_Req):
    kind: StrictStr
    dimensions: dict[StrictStr, Number] | None = None
    layers: list[Layer] | None = None
    stack_axis: Literal["z", "y"] | None = None
    file_id: StrictStr | None = None
    placement: Placement | None = None
    body_slots: list[BodySlot] | None = None
    diameter: Number | None = None  # 2차 예약
    top_diameter: Number | None = None
    bottom_diameter: Number | None = None
    height: Number | None = None


class UserWindow(_Req):
    x_min: Number
    x_max: Number


class TestInput(_Req):
    __test__ = False  # pytest 수집 방지

    test_key: StrictStr | None = Field(default=None, pattern=TEST_KEY_RE)
    test_type: StrictStr
    recipe: StrictStr = Field(pattern=RECIPE_REF_RE)
    geometry: GeometryInput
    test_run_ids: list[StrictStr] | None = None
    inline_runs: list[InlineRun] | None = None
    displacement_channel: Literal["auto", "displacement", "deflection"] = "auto"
    recipe_params: dict[StrictStr, Number] | None = None
    user_window: UserWindow | None = None
    weight: Number = Field(default=1.0, gt=0)


#: 단일 시험 약식으로 최상위에 둘 수 있는 TestInput 필드 (test_key 제외: 약식은 자동 t1)
SHORTHAND_FIELDS = ("test_type", "recipe", "geometry", "test_run_ids", "inline_runs", "displacement_channel",
                    "recipe_params", "user_window", "weight")


# ---------------------------------------------------------------------- 요청: 재료
class ElasticIn(_Req):
    youngs_modulus: Number | None = None
    poisson_ratio: Number | None = None
    density: Number | None = None


class TableRowIn(_Req):
    plastic_strain: Number
    true_stress: Number


class CardBlocksIn(_Req):
    elastic: ElasticIn | None = None
    table: list[TableRowIn] | None = None
    rate_table: dict[StrictStr, Any] | None = None


class ParamOverride(_Req):
    lower: Number | None = None
    upper: Number | None = None
    initial: Number | None = None
    fixed_value: Number | None = None


class SlotInput(_Req):
    slot: StrictStr = Field(pattern=SLOT_RE)
    role: Literal["identify", "fixed"]
    material_model: StrictStr
    options: dict[StrictStr, Any] | None = None
    reference: CardBlocksIn | None = None
    params: dict[StrictStr, ParamOverride] | None = None
    member_position: StrictInt | None = Field(default=None, ge=0)


class MaterialsInput(_Req):
    mode: Literal["A", "B"]
    slots: list[SlotInput]


# ---------------------------------------------------------------------- 요청: 부가
class NamedOptions(_Req):
    name: StrictStr
    options: dict[StrictStr, Any] | None = None


PluginRef = Union[StrictStr, NamedOptions]


class FrictionInput(_Req):
    mode: Literal["fixed", "identify"]
    value: Number | None = Field(default=None, ge=0)
    lower: Number | None = None
    upper: Number | None = None


class BudgetInput(_Req):
    max_evaluations: StrictInt | None = Field(default=None, ge=1)
    max_wall_time_s: Number | None = Field(default=None, gt=0)
    run_timeout_s: Number | None = Field(default=None, gt=0)


class Hourglass(_Req):
    ihq: StrictInt | None = None
    qm: Number | None = None


class SectionOverride(_Req):
    elform: StrictInt | None = None
    hourglass: Hourglass | None = None


class ExplicitSection(SectionOverride):
    slots: dict[StrictStr, SectionOverride] | None = None


class ExplicitCheckInput(_Req):
    enabled: StrictBool
    tests: list[StrictStr] | None = None
    section: ExplicitSection | None = None
    loading_velocity: Number | None = Field(default=None, gt=0)
    max_ke_ratio: Number | None = None


class MaterialRef(_Req):
    matnexus_material_id: StrictStr = Field(pattern=UUID_RE)


class CalibrationRequest(_Req):
    schema_version: StrictStr
    name: StrictStr | None = Field(default=None, max_length=200)
    material_ref: MaterialRef | None = None
    tests: list[TestInput]
    materials: MaterialsInput
    plasticity_mode: Literal["coeff", "table"] | None = None
    optimizer: PluginRef | None = None
    elastic_optimizer: PluginRef | None = None
    solver: PluginRef | None = None
    friction: FrictionInput | None = None
    budget: BudgetInput | None = None
    seed: StrictInt | None = Field(default=None, ge=0)
    explicit_check: ExplicitCheckInput | None = None


class RetireRequest(_Req):
    schema_version: StrictStr | None = None
    reason: StrictStr = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------- 응답 (문서화·계약 테스트용 형태)
class Warning_(BaseModel):
    code: str
    message: str
    test_key: str | None = None


class JobCreated(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    status: str
    created_at: str
    warnings: list[dict[str, Any]] = []


class FileOut(BaseModel):
    schema_version: str = SCHEMA_VERSION
    file_id: str
    purpose: str
    filename: str
    bytes: int
    sha256: str
    created_at: str
