"""레시피 매니페스트(`manifest.yaml`) 파싱·스키마 검사 (recipe.md §4).

스키마 위반(필수·타입·열거)은 `RECIPE_MANIFEST_INVALID`. 이름 패턴·범위 역전·`unit_system`·힘 배율 같은 값 규칙은
계약이 별도 코드를 주므로(`RECIPE_PARAM_NAME_INVALID` 등) 여기서는 타입만 보고 `static_check` 가 값을 본다.
알 수 없는 키는 무시한다(MINOR 확장 여유). 원본 매핑은 그대로 보존해 저장·공개한다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from ..core.config import safe_load_yaml

Number = Annotated[float, Field(strict=True)]  # int 허용, 문자열 숫자 거부


class _M(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _Analysis(_M):
    primary: Literal["implicit", "explicit"]
    fallback: Literal["explicit", "none"]


class _Solver(_M):
    name: StrictStr
    min_version: StrictStr | None = None
    analysis: _Analysis


class _Controls(_M):
    implicit: StrictStr | None = None
    explicit: StrictStr | None = None


class _Deck(_M):
    main: StrictStr
    controls: _Controls
    files: list[StrictStr] = Field(default_factory=list)


class _Symmetry(_M):
    type: Literal["none", "half", "quarter"]
    planes: list[StrictStr] = Field(default_factory=list)
    force_scale: Number


class _Element(_M):
    topology: Literal["hex8", "tet4", "tet10", "quad4_axisym"]
    target_size_m: Number
    min_elems_through_thickness: StrictInt
    stp_topology: Literal["tet4", "tet10"] | None = None


class _IdRanges(_M):
    node: Annotated[list[StrictInt], Field(min_length=2, max_length=2)]
    element: Annotated[list[StrictInt], Field(min_length=2, max_length=2)]


class _Specimen(_M):
    geometry_kinds: Annotated[list[StrictStr], Field(min_length=1)]
    element: _Element
    part_set_id: StrictInt
    node_sets: dict[StrictStr, StrictInt] = Field(default_factory=dict)
    id_ranges: _IdRanges
    placement: dict[str, Any] = Field(default_factory=dict)


class _Jig(_M):
    generator: StrictStr
    part_ids: dict[StrictStr, StrictInt]
    mids: dict[StrictStr, StrictInt]
    id_ranges: _IdRanges
    params: dict[StrictStr, Any] = Field(default_factory=dict)
    reference_nodes: list[StrictStr] = Field(default_factory=list)
    rigid: dict[str, Number] | None = None


class _Section(_M):
    keyword: StrictStr
    elform: StrictInt


class _Slot(_M):
    slot: StrictStr
    regions: Annotated[list[StrictStr], Field(min_length=1)]
    part_id: StrictInt
    mid: StrictInt
    secid: StrictInt
    lcid: StrictInt | None = None
    section: _Section


class _Param(_M):
    name: StrictStr
    type: Literal["R", "I"]
    dimension: Literal["length", "velocity", "force", "stress", "density", "time", "dimensionless"]
    unit_si: StrictStr
    default: Number
    range: Annotated[list[Number], Field(min_length=2, max_length=2)]
    matnexus_key: StrictStr | None = None
    description: StrictStr | None = None


class _Response(_M):
    response_key: StrictStr
    test_channel: StrictStr
    axis: Literal["displacement", "deflection", "time"] | None = None
    extractor: dict[str, Any]
    alt_axes: dict[str, Any] | None = None
    weight: Number | None = None
    window: dict[str, Any] | None = None


class _Compliance(_M):
    value_m_per_n: Number | None = None
    method: StrictStr | None = None
    calibrated_at: StrictStr | None = None
    instruments: list[StrictStr] | None = None
    note: StrictStr | None = None


class _MeshConv(_M):
    size_factors: Annotated[list[Number], Field(min_length=1)]
    quantities: list[StrictStr]


class _Verification(_M):
    reference_card: StrictStr
    reference_dimensions: dict[str, Number]
    reference_params: dict[str, Number] | None = None
    reference_window_m: Number
    mesh_convergence: _MeshConv
    reference_curve: StrictStr | None = None


class Manifest(_M):
    schema_version: StrictStr
    recipe_id: StrictStr
    version: StrictInt = Field(ge=1)
    kind: Literal["parametric", "fixed"]
    test_type: StrictStr
    title: StrictStr
    description: StrictStr | None = None
    standards: list[StrictStr] = Field(default_factory=list)
    unit_system: StrictStr
    solver: _Solver
    deck: _Deck
    symmetry: _Symmetry
    specimen: _Specimen
    jig: _Jig | None = None
    material_slots: Annotated[list[_Slot], Field(min_length=1)]
    parameters: list[_Param]
    responses: Annotated[list[_Response], Field(min_length=1)]
    compliance: _Compliance | None = None
    verification: _Verification
    owner: StrictStr | None = None


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for p in loc:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def parse_manifest_text(text: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """YAML 텍스트 -> (원본 매핑, 오류 목록 `[{field, message}]`). 오류가 있으면 매핑은 None(YAML 자체가 깨졌을 때) 또는 원본."""
    try:
        raw = safe_load_yaml(text)
    except yaml.YAMLError as e:
        return None, [{"field": "", "message": f"manifest.yaml 이 올바른 YAML 이 아닙니다: {str(e).splitlines()[0] if str(e) else e}"}]
    if not isinstance(raw, Mapping):
        return None, [{"field": "", "message": "manifest.yaml 최상위는 매핑이어야 합니다."}]
    return dict(raw), validate_manifest(raw)


def validate_manifest(raw: Mapping[str, Any]) -> list[dict[str, str]]:
    try:
        Manifest.model_validate(dict(raw))
    except ValidationError as e:
        return [{"field": _loc(err["loc"]), "message": f"{err['msg']}"} for err in e.errors()]
    return []
