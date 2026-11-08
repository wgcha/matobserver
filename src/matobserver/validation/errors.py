"""검증 실패 예외와 detail 정렬 (api.md §7.1·§4.7)."""

from __future__ import annotations

from typing import Any, Mapping

from ..core import errors as E
from ..core.errors import MatObserverError

#: 코드 -> 검증 순서 단계 (api.md §4.7 ①–⑩). details[] 는 단계 순으로 정렬된다.
_STEP_BY_CODE: dict[str, int] = {}
for _c in (E.SCHEMA_INVALID, E.SCHEMA_VERSION_UNSUPPORTED):
    _STEP_BY_CODE[_c] = 1
for _c in (E.TEST_TYPE_UNKNOWN, E.OPTIMIZER_UNKNOWN, E.SOLVER_UNKNOWN, E.MATERIAL_MODEL_UNKNOWN):
    _STEP_BY_CODE[_c] = 2
for _c in (E.RECIPE_NOT_FOUND, E.RECIPE_NOT_ACTIVE, E.RECIPE_RETIRED, E.RECIPE_TEST_TYPE_MISMATCH,
           E.RECIPE_PARAM_UNKNOWN, E.RECIPE_PARAM_OUT_OF_RANGE):
    _STEP_BY_CODE[_c] = 3
for _c in (E.SLOT_UNKNOWN, E.SLOT_UNASSIGNED, E.SLOT_NO_IDENTIFY, E.MODE_A_MULTI_SLOT, E.MODE_B_PLASTIC_UNSUPPORTED,
           E.FIXED_SLOT_REFERENCE_MISSING, E.POISSON_RATIO_REQUIRED):
    _STEP_BY_CODE[_c] = 4
for _c in (E.TEST_TYPE_UNMAPPED, E.RUN_TEST_TYPE_MISMATCH, E.TEST_VARIANT_UNSUPPORTED, E.RUN_NOT_FOUND, E.RUN_UNREADABLE,
           E.CHANNEL_MISSING, E.DISPLACEMENT_CHANNEL_UNAVAILABLE, E.CHANNEL_UNIT_INVALID, E.INLINE_RUN_INVALID,
           E.SPECIMEN_DIMENSION_MISSING):
    _STEP_BY_CODE[_c] = 5
for _c in (E.CURVE_TOO_FEW_POINTS, E.CURVE_NOT_MONOTONIC, E.CURVE_NO_CONTACT, E.CURVE_NO_ELASTIC_REGION):
    _STEP_BY_CODE[_c] = 6
for _c in (E.GEOMETRY_KIND_NOT_ALLOWED, E.GEOMETRY_DIMENSION_MISSING, E.GEOMETRY_DIMENSION_INVALID, E.FILE_NOT_FOUND,
           E.STP_UNREADABLE, E.STP_BODY_SLOT_MISMATCH, E.STP_CHECK_FAILED):
    _STEP_BY_CODE[_c] = 7
for _c in (E.PARAM_UNKNOWN, E.PARAM_RANGE_INVALID, E.BUDGET_EXCEEDS_LIMIT):
    _STEP_BY_CODE[_c] = 8
for _c in (E.MATERIAL_SOLVER_INCOMPATIBLE, E.SOLVER_ANALYSIS_UNSUPPORTED, E.FRICTION_IDENTIFY_UNSUPPORTED,
           E.EXPLICIT_CHECK_UNAVAILABLE, E.EXPLICIT_CHECK_SECTION_INVALID):
    _STEP_BY_CODE[_c] = 9
_STEP_BY_CODE[E.IDENTIFIABILITY_UNDERDETERMINED] = 10


def step_of(code: str) -> int:
    """알 수 없는 코드(플러그인이 새로 낸 코드)는 9단계(조합)로 본다."""
    return _STEP_BY_CODE.get(code, 9)


class ValidationFailed(MatObserverError):
    """잡 생성 검증 실패 -> 400 VALIDATION_FAILED, `details[]` 에 사유 전부."""

    code = E.VALIDATION_FAILED

    def __init__(self, details: list[dict[str, Any]]) -> None:
        super().__init__(f"요청 검증 실패 {len(details)}건")
        self.details = details


def make_detail(code: str, message: str, *, field: str | None = None, **ctx: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"code": code, "message": message}
    if field:
        d["field"] = field
    for k in ("test_key", "run_id", "slot", "param"):
        if ctx.get(k) is not None:
            d[k] = ctx[k]
    return d


def sort_details(details: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """단계 순(안정 정렬)으로 정렬."""
    return [dict(d) for d in sorted(details, key=lambda d: step_of(d["code"]))]
