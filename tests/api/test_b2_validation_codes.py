"""B-2: api.md §7.4 의 400 검증 사유 코드마다 최소 1개 요청 (코드 일치).

* 표(`CASES`)는 코드 -> 요청을 만드는 함수다. 마지막 시험이 api.md §7.4 표에서 코드를 읽어 빠짐없이 덮는지 확인한다.
* 플러그인·엔진이 내는 코드(CURVE_*, IDENTIFIABILITY_*, STP_* 등)는 실제 플러그인(STP) 또는 엔진 preflight 대역으로 만든다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from matobserver.core.types import Issue

from be_support import (FIX, RECIPE_REF, RUN_ID_BAD, RUN_ID_MISSING, build_env, calibration_body, error_codes,
                        inline_run, mn_body, recipe_zip)

REPO = FIX.parents[2]


def slot(**over):
    s = {"slot": "body", "role": "identify", "material_model": "mat024", "reference": {"elastic": {"poisson_ratio": 0.33, "density": 2700.0}}}
    s.update(over)
    return s


def mats(*slots, mode="A"):
    return {"mode": mode, "slots": list(slots)}


def run_with(**over):
    r = inline_run()
    r.update(over)
    return r


def post(e, **over):
    return e.post_job(calibration_body(**over))


def upload_stp(e, name):
    data = (FIX / "stp" / name).read_bytes()
    r = e.client.post("/v1/files", files={"file": (name, data, "application/step")}, data={"purpose": "geometry_stp"}, headers=e.svc)
    assert r.status_code == 201, r.text
    return r.json()["file_id"]


# ---------------------------------------------------------------- 스키마
def c_schema_invalid(e):
    return e.post_job({"schema_version": "1.0", "tests": [], "materials": mats(slot())})


def c_schema_invalid_both(e):
    b = calibration_body()
    b["tests"] = [{"test_key": "t1", "test_type": "bending", "recipe": RECIPE_REF, "geometry": {"kind": "box"}, "inline_runs": [inline_run()]}]
    return e.post_job(b)


def c_schema_invalid_extra_field(e):
    return post(e, bogus=1)


def c_schema_version(e):
    return post(e, schema_version="2.0")


# ---------------------------------------------------------------- 레지스트리
def c_test_type_unknown(e):
    return post(e, test_type="tension")


def c_optimizer_unknown(e):
    return post(e, optimizer="nope")


def c_solver_unknown(e):
    return post(e, solver="nope")


def c_material_model_unknown(e):
    return post(e, materials=mats(slot(material_model="mat999")))


# ---------------------------------------------------------------- 시험 종류
def c_test_type_unmapped(e):
    return post(e, inline_runs=[run_with(test_type_key="tensile")])


def c_run_test_type_mismatch(e):
    return post(e, inline_runs=[run_with(test_type_key="compressive")])


def c_test_variant_unsupported(e):
    r = inline_run()
    r["specimen_dimensions"]["load_span"] = 0.02
    return post(e, inline_runs=[r])


# ---------------------------------------------------------------- 레시피
def c_recipe_not_found(e):
    return post(e, recipe="no-such-recipe@1")


def c_recipe_not_active(e):
    assert e.add_recipe(recipe_zip(version=2)).status_code == 202  # 검증 전(verifying)
    return post(e, recipe="bending3p-param@2")


def c_recipe_retired(e):
    e.activate_recipe(recipe_zip(version=3))
    assert e.client.post("/v1/admin/recipes/bending3p-param/versions/3/retire", json={"reason": "r"}, headers=e.admin).status_code == 200
    return post(e, recipe="bending3p-param@3")


def c_recipe_test_type_mismatch(e):
    return post(e, test_type="compression", inline_runs=[run_with(test_type_key="compressive")])


def c_recipe_param_unknown(e):
    return post(e, recipe_params={"zzz": 1.0})


def c_recipe_param_out_of_range(e):
    return post(e, recipe_params={"span": 5.0})


# ---------------------------------------------------------------- 슬롯
def c_slot_unknown(e):
    return post(e, materials=mats(slot(slot="other")))


def c_slot_unassigned(e):
    return post(e, materials=mats(slot(slot="other")))


def c_slot_no_identify(e):
    ref = {"elastic": {"youngs_modulus": 7e10, "poisson_ratio": 0.33, "density": 2700.0}}
    return post(e, materials=mats(slot(role="fixed", reference=ref)))


def c_mode_a_multi_slot(e):
    return post(e, materials=mats(slot(), slot(slot="other")))


def c_mode_b_plastic_unsupported(e):
    return post(e, materials=mats(slot(), mode="B"))


def c_fixed_slot_reference_missing(e):
    s = slot(role="fixed")
    s.pop("reference")
    return post(e, materials=mats(s, slot(slot="x")))


def c_poisson_required(e):
    return post(e, materials=mats(slot(reference={"elastic": {"density": 2700.0}})))


# ---------------------------------------------------------------- 시험 데이터
def c_run_not_found(e):
    return e.post_job(mn_body(test_run_ids=[RUN_ID_MISSING]))


def c_run_unreadable(e):
    return e.post_job(mn_body(test_run_ids=[RUN_ID_BAD]))


def c_channel_missing(e):
    r = inline_run()
    del r["channels"]["force"]
    return post(e, inline_runs=[r])


def c_displacement_channel_unavailable(e):
    return post(e, displacement_channel="deflection")


def c_channel_unit_invalid(e):
    r = inline_run()
    r["channels"]["displacement"]["unit"] = "mm"
    return post(e, inline_runs=[r])


def c_inline_run_invalid(e):
    r = inline_run()
    r["channels"]["force"]["values"] = r["channels"]["force"]["values"][:-1]
    return post(e, inline_runs=[r])


def c_specimen_dimension_missing(e):
    r = inline_run()
    del r["specimen_dimensions"]["span"]
    return post(e, inline_runs=[r])


# ---------------------------------------------------------------- 곡선
def c_curve_too_few_points(e):
    r = inline_run()
    for ch in r["channels"].values():
        ch["values"] = ch["values"][:10]
    return post(e, inline_runs=[r])


def _preflight(code):
    def fn(spec, services):
        return [Issue(code, "엔진 사전 점검 대역", "error", field="tests[0].inline_runs[0]", context={"test_key": "t1"})]

    return fn


def c_curve_not_monotonic(e):
    e.ctx.hooks.preflight = _preflight("CURVE_NOT_MONOTONIC")
    return post(e)


def c_curve_no_contact(e):
    e.ctx.hooks.preflight = _preflight("CURVE_NO_CONTACT")
    return post(e)


def c_curve_no_elastic_region(e):
    e.ctx.hooks.preflight = _preflight("CURVE_NO_ELASTIC_REGION")
    return post(e)


def c_identifiability(e):
    e.ctx.hooks.preflight = _preflight("IDENTIFIABILITY_UNDERDETERMINED")
    return post(e)


# ---------------------------------------------------------------- 형상
def c_geometry_kind_not_allowed(e):
    return post(e, geometry={"kind": "cylinder", "diameter": 0.01, "height": 0.05})


def c_geometry_dimension_missing(e):
    r = inline_run()
    del r["specimen_dimensions"]["total_length"]
    return post(e, inline_runs=[r])


def c_geometry_dimension_invalid(e):
    return post(e, geometry={"kind": "box", "dimensions": {"width": -0.01}})


def c_file_not_found(e):
    return post(e, geometry={"kind": "stp", "file_id": "fil_00000000000000000000000000"})


def c_stp_unreadable(e):
    return post(e, geometry={"kind": "stp", "file_id": upload_stp(e, "corrupt.stp")})


def c_stp_body_slot_mismatch(e):
    fid = upload_stp(e, "two_body.stp")
    return post(e, geometry={"kind": "stp", "file_id": fid, "body_slots": [{"body": 7, "slot": "body"}]})


def c_stp_check_failed(e):
    return post(e, geometry={"kind": "stp", "file_id": upload_stp(e, "thin.stp")})


# ---------------------------------------------------------------- 파라미터·조합·예산
def c_param_unknown(e):
    return post(e, materials=mats(slot(params={"zzz": {"lower": 1.0, "upper": 2.0}})))


def c_param_range_invalid(e):
    return post(e, materials=mats(slot(params={"E": {"lower": 2e11, "upper": 1e9}})))


def c_material_solver_incompatible(e):
    return post(e, materials=mats(slot(material_model="lsdyna_only_mat")), solver="surrogate_beam")


def c_solver_analysis_unsupported(e):
    return post(e, solver="implicit_only")


def c_friction_identify(e):
    return post(e, friction={"mode": "identify", "lower": 0.0, "upper": 0.5})


def c_budget_exceeds_limit(e):
    return post(e, budget={"max_evaluations": 10**6})


def c_explicit_check_unavailable(e):
    return post(e, solver="implicit_only", explicit_check={"enabled": True})


def c_explicit_check_section_invalid(e):
    return post(e, explicit_check={"enabled": True, "section": {"elform": 99}})


CASES = {
    "SCHEMA_INVALID": c_schema_invalid,
    "SCHEMA_VERSION_UNSUPPORTED": c_schema_version,
    "TEST_TYPE_UNKNOWN": c_test_type_unknown,
    "OPTIMIZER_UNKNOWN": c_optimizer_unknown,
    "SOLVER_UNKNOWN": c_solver_unknown,
    "MATERIAL_MODEL_UNKNOWN": c_material_model_unknown,
    "TEST_TYPE_UNMAPPED": c_test_type_unmapped,
    "RUN_TEST_TYPE_MISMATCH": c_run_test_type_mismatch,
    "TEST_VARIANT_UNSUPPORTED": c_test_variant_unsupported,
    "RECIPE_NOT_FOUND": c_recipe_not_found,
    "RECIPE_NOT_ACTIVE": c_recipe_not_active,
    "RECIPE_RETIRED": c_recipe_retired,
    "RECIPE_TEST_TYPE_MISMATCH": c_recipe_test_type_mismatch,
    "RECIPE_PARAM_UNKNOWN": c_recipe_param_unknown,
    "RECIPE_PARAM_OUT_OF_RANGE": c_recipe_param_out_of_range,
    "SLOT_UNKNOWN": c_slot_unknown,
    "SLOT_UNASSIGNED": c_slot_unassigned,
    "SLOT_NO_IDENTIFY": c_slot_no_identify,
    "MODE_A_MULTI_SLOT": c_mode_a_multi_slot,
    "MODE_B_PLASTIC_UNSUPPORTED": c_mode_b_plastic_unsupported,
    "FIXED_SLOT_REFERENCE_MISSING": c_fixed_slot_reference_missing,
    "POISSON_RATIO_REQUIRED": c_poisson_required,
    "RUN_NOT_FOUND": c_run_not_found,
    "RUN_UNREADABLE": c_run_unreadable,
    "CHANNEL_MISSING": c_channel_missing,
    "DISPLACEMENT_CHANNEL_UNAVAILABLE": c_displacement_channel_unavailable,
    "CHANNEL_UNIT_INVALID": c_channel_unit_invalid,
    "INLINE_RUN_INVALID": c_inline_run_invalid,
    "SPECIMEN_DIMENSION_MISSING": c_specimen_dimension_missing,
    "CURVE_TOO_FEW_POINTS": c_curve_too_few_points,
    "CURVE_NOT_MONOTONIC": c_curve_not_monotonic,
    "CURVE_NO_CONTACT": c_curve_no_contact,
    "CURVE_NO_ELASTIC_REGION": c_curve_no_elastic_region,
    "GEOMETRY_KIND_NOT_ALLOWED": c_geometry_kind_not_allowed,
    "GEOMETRY_DIMENSION_MISSING": c_geometry_dimension_missing,
    "GEOMETRY_DIMENSION_INVALID": c_geometry_dimension_invalid,
    "FILE_NOT_FOUND": c_file_not_found,
    "STP_UNREADABLE": c_stp_unreadable,
    "STP_BODY_SLOT_MISMATCH": c_stp_body_slot_mismatch,
    "STP_CHECK_FAILED": c_stp_check_failed,
    "PARAM_UNKNOWN": c_param_unknown,
    "PARAM_RANGE_INVALID": c_param_range_invalid,
    "MATERIAL_SOLVER_INCOMPATIBLE": c_material_solver_incompatible,
    "SOLVER_ANALYSIS_UNSUPPORTED": c_solver_analysis_unsupported,
    "FRICTION_IDENTIFY_UNSUPPORTED": c_friction_identify,
    "BUDGET_EXCEEDS_LIMIT": c_budget_exceeds_limit,
    "EXPLICIT_CHECK_UNAVAILABLE": c_explicit_check_unavailable,
    "EXPLICIT_CHECK_SECTION_INVALID": c_explicit_check_section_invalid,
    "IDENTIFIABILITY_UNDERDETERMINED": c_identifiability,
}

#: 같은 코드를 다른 경로로 내는 추가 요청
EXTRA = [
    ("SCHEMA_INVALID", c_schema_invalid_both),
    ("SCHEMA_INVALID", c_schema_invalid_extra_field),
]


@pytest.fixture
def e2(tmp_path):
    e = build_env(tmp_path, extra_plugins=True)
    e.activate_recipe()
    yield e
    e.client.close()
    from matobserver.recipes.repository import unlock_tree

    if (tmp_path / "data" / "recipes").exists():
        unlock_tree(tmp_path / "data" / "recipes")


@pytest.mark.parametrize("code", sorted(CASES))
def test_validation_code(code, e2):
    r = CASES[code](e2)
    assert r.status_code == 400, f"{code}: {r.status_code} {r.text}"
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_FAILED"
    codes = error_codes(r)
    assert code in codes, f"{code} 기대, 실제 {codes}: {r.text}"
    for d in err["details"]:
        assert d["code"] and d["message"]


@pytest.mark.parametrize("code,fn", EXTRA, ids=[f.__name__ for _, f in EXTRA])
def test_validation_code_extra(code, fn, e2):
    r = fn(e2)
    assert r.status_code == 400 and code in error_codes(r), r.text


def test_all_codes_from_api_md_are_covered():
    text = (REPO / "docs" / "contracts" / "api.md").read_text(encoding="utf-8")
    sec = text.split("### 7.4", 1)[1].split("\n## 8.", 1)[0]
    codes = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", sec))
    codes -= {"VALIDATION_FAILED", "MAJOR", "MINOR"}
    missing = sorted(codes - set(CASES))
    assert not missing, f"api.md §7.4 의 코드 중 시험이 없는 것: {missing}"
    assert not (set(CASES) - codes), f"표에 없는 코드: {sorted(set(CASES) - codes)}"


def test_details_are_collected_together(e2):
    """오류는 한꺼번에 모아 details[] 로 돌려준다 (api.md §4.7)."""
    r = post(e2, optimizer="nope", solver="nope", budget={"max_evaluations": 10**6})
    assert r.status_code == 400
    assert {"OPTIMIZER_UNKNOWN", "SOLVER_UNKNOWN"} <= set(error_codes(r))


def test_step5_failure_skips_later_steps(e2):
    """⑤ 시험 데이터 단계에 오류가 있으면 ⑥ 이후(곡선 품질 등)는 건너뛴다."""
    r = e2.post_job(mn_body(test_run_ids=[RUN_ID_MISSING], budget={"max_evaluations": 10**6}))
    assert r.status_code == 400
    codes = error_codes(r)
    assert "RUN_NOT_FOUND" in codes and "BUDGET_EXCEEDS_LIMIT" not in codes


def test_details_sorted_by_step_and_carry_context(e2):
    r = e2.post_job(mn_body(test_run_ids=[RUN_ID_MISSING]))
    d = r.json()["error"]["details"][0]
    assert d["code"] == "RUN_NOT_FOUND" and d["test_key"] == "t1" and d["run_id"] == RUN_ID_MISSING and d["field"] == "test_run_ids[0]"


def test_preflight_not_called_when_earlier_steps_fail(e2):
    calls = []
    e2.ctx.hooks.preflight = lambda spec, svc: calls.append(1) or []
    assert post(e2, optimizer="nope").status_code == 400
    assert not calls
    assert post(e2).status_code == 201 and len(calls) == 1


def test_preflight_warning_goes_to_warnings(e2):
    e2.ctx.hooks.preflight = lambda spec, svc: [Issue("CURVE_SOFT_NOTE", "참고", "warning", context={"test_key": "t1"})]
    r = post(e2)
    assert r.status_code == 201
    assert any(w["code"] == "CURVE_SOFT_NOTE" and w["test_key"] == "t1" for w in r.json()["warnings"])


def test_no_job_is_created_on_400(e2):
    assert post(e2, optimizer="nope").status_code == 400
    assert e2.get("/v1/calibrations").json()["total"] == 0
    work = Path(e2.config.get("paths.work_dir"))
    assert not work.exists() or not any(work.iterdir())

