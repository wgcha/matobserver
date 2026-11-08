"""잡 생성 검증 파이프라인 (api.md §4.7): 단계 순서·정규화·스냅샷·경고."""

from __future__ import annotations

import copy
import json

import pytest

from matobserver.core.types import CalibrationSpec
from matobserver.validation.errors import ValidationFailed, step_of
from matobserver.validation.pipeline import parse_json_body, run_json_files, validate_request
from matobserver.validation.spec import build_spec, run_from_json, run_to_json

from be_support import RECIPE_REF, RUN_ID, calibration_body, inline_run, mn_body


def validate(e, body, job_id="cal_unit"):
    return validate_request(copy.deepcopy(body), e.ctx.validation, job_id)


def failed_codes(e, body):
    with pytest.raises(ValidationFailed) as ei:
        validate(e, body)
    return [d["code"] for d in ei.value.details]


def as_tests_array(body, n=2, **per_test):
    """약식 본문을 tests[] 형태로 바꾼다."""
    b = copy.deepcopy(body)
    t = {k: b.pop(k) for k in ("test_type", "recipe", "geometry", "inline_runs", "test_run_ids") if k in b}
    b["tests"] = []
    for i in range(n):
        ti = copy.deepcopy(t)
        ti["test_key"] = f"k{i + 1}"
        ti.update(per_test.get(f"k{i + 1}", {}))
        if ti.get("inline_runs") and n > 1:
            ti["inline_runs"][0]["run_key"] = f"run{i + 1}"
        b["tests"].append(ti)
    return b


# ---------------------------------------------------------------- 정규화·기본값
def test_defaults_are_filled_from_config(active_env):
    e = active_env
    v = validate(e, {k: val for k, val in calibration_body().items() if k != "budget"})
    s = v.snapshot
    cfg = e.config
    assert s["budget"] == {"max_evaluations": cfg.get("budget.default_max_evaluations"),
                           "max_wall_time_s": float(cfg.get("budget.default_max_wall_time_s")),
                           "run_timeout_s": float(cfg.get("solver.run_timeout_s"))}
    assert s["seed"] == cfg.get("engine.default_seed") and s["solver"]["name"] == cfg.get("engine.default_solver")
    assert s["optimizer"]["name"] == cfg.get("engine.plastic_stage.optimizer")
    assert s["elastic_optimizer"]["name"] == cfg.get("engine.elastic_stage.optimizer")
    assert s["plasticity_mode"] == cfg.get("engine.default_plasticity_mode") and s["friction"] == {"mode": "fixed"}
    assert s["explicit_check"] == {"enabled": False}
    assert v.request["seed"] == s["seed"] and v.request_hash == s["request_hash"]


def test_request_hash_is_stable_and_sensitive(active_env):
    e = active_env
    a, b = validate(e, calibration_body()), validate(e, calibration_body())
    assert a.request_hash == b.request_hash
    assert validate(e, calibration_body(seed=2)).request_hash != a.request_hash
    # 기본값을 명시해도 같은 정규화 요청
    explicit = calibration_body(seed=e.config.get("engine.default_seed"), solver="lsdyna", plasticity_mode="coeff")
    assert validate(e, explicit).request_hash == a.request_hash


def test_alias_resolution_follows_plasticity_mode(active_env):
    e = active_env
    assert validate(e, calibration_body()).snapshot["materials"]["slots"][0]["material_model"] == "mat024_coeff"
    v = validate(e, calibration_body(plasticity_mode="table"))
    assert v.snapshot["materials"]["slots"][0]["material_model"] == "mat024_table" and v.snapshot["plasticity_mode"] == "table"
    assert validate(e, calibration_body(materials={"mode": "A", "slots": [{
        "slot": "body", "role": "identify", "material_model": "mat001",
        "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}]})).snapshot["materials"]["slots"][0]["material_model"] == "mat001"


def test_plugin_options_are_merged_with_registry_defaults(active_env):
    e = active_env
    v = validate(e, calibration_body(plasticity_mode="table", materials={"mode": "A", "slots": [{
        "slot": "body", "role": "identify", "material_model": "mat024", "options": {"table_nodes": 6},
        "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}]}))
    opts = v.snapshot["materials"]["slots"][0]["options"]
    assert opts["table_nodes"] == 6 and opts["node_spacing"] == "geometric"  # 레지스트리 defaults 병합


def test_optimizer_and_solver_accept_name_or_object(active_env):
    e = active_env
    v = validate(e, calibration_body(optimizer={"name": "random", "options": {"batch": 3}}, elastic_optimizer="lsq",
                                     solver={"name": "surrogate_beam", "options": {"fibers": 100}}))
    assert v.snapshot["optimizer"] == {"name": "random", "options": {"batch": 3}}
    assert v.snapshot["elastic_optimizer"] == {"name": "lsq", "options": {}}
    assert v.snapshot["solver"] == {"name": "surrogate_beam", "options": {"fibers": 100}}


def test_friction_fixed_value_needs_recipe_fric_param(active_env):
    e = active_env
    v = validate(e, calibration_body(friction={"mode": "fixed", "value": 0.2}))
    assert v.snapshot["friction"] == {"mode": "fixed", "value": 0.2}


# ---------------------------------------------------------------- 단계 순서·수집
def test_details_sorted_by_step_order(active_env):
    e = active_env
    with pytest.raises(ValidationFailed) as ei:
        validate(e, calibration_body(solver="nope", recipe="missing@1", test_type="bending"))
    steps = [step_of(d["code"]) for d in ei.value.details]
    assert steps == sorted(steps) and {"SOLVER_UNKNOWN", "RECIPE_NOT_FOUND"} <= {d["code"] for d in ei.value.details}


def test_steps_2_to_4_errors_skip_step_6_onward(active_env):
    e = active_env
    codes = failed_codes(e, calibration_body(solver="nope", budget={"max_evaluations": 10**7}))
    assert "SOLVER_UNKNOWN" in codes and "BUDGET_EXCEEDS_LIMIT" not in codes  # ⑧ 은 ②–⑤ 오류가 없을 때만


def test_budget_and_params_checked_together_when_data_is_fine(active_env):
    e = active_env
    codes = failed_codes(e, calibration_body(budget={"max_evaluations": 10**7}, friction={"mode": "identify", "lower": 0, "upper": 1}))
    assert {"BUDGET_EXCEEDS_LIMIT", "FRICTION_IDENTIFY_UNSUPPORTED"} <= set(codes)


def test_many_errors_in_one_request_all_reported(active_env):
    e = active_env
    r = inline_run()
    del r["channels"]["force"]
    codes = failed_codes(e, calibration_body(inline_runs=[r], optimizer="nope", materials={"mode": "A", "slots": [{
        "slot": "other", "role": "identify", "material_model": "mat024", "reference": {"elastic": {"density": 1.0}}}]}))
    assert {"OPTIMIZER_UNKNOWN", "SLOT_UNKNOWN", "SLOT_UNASSIGNED", "POISSON_RATIO_REQUIRED", "CHANNEL_MISSING"} <= set(codes)


def test_details_are_deduplicated(active_env):
    e = active_env
    codes = failed_codes(e, mn_body(test_run_ids=[RUN_ID, RUN_ID], recipe_params={"zzz": 1.0}))
    assert codes.count("RECIPE_PARAM_UNKNOWN") == 1


# ---------------------------------------------------------------- 약식 / tests[]
def test_shorthand_equals_tests_array_with_t1(active_env):
    e = active_env
    a = validate(e, calibration_body())
    b = validate(e, as_tests_array(calibration_body(), n=1, k1={"test_key": "t1"}))
    assert a.request_hash == b.request_hash and a.tests == b.tests == [{"test_key": "t1", "test_type": "bending", "recipe_ref": RECIPE_REF}]


def test_shorthand_error_fields_have_no_tests_prefix_and_array_ones_do(active_env):
    e = active_env
    with pytest.raises(ValidationFailed) as ei:
        validate(e, calibration_body(recipe="no-such@1"))
    assert ei.value.details[0]["field"] == "recipe"
    with pytest.raises(ValidationFailed) as ei:
        validate(e, as_tests_array(calibration_body(), n=2, k2={"recipe": "no-such@1"}))
    d = ei.value.details[0]
    assert d["field"] == "tests[1].recipe" and d["test_key"] == "k2"


def test_two_tests_one_job_with_weights(active_env):
    e = active_env
    v = validate(e, as_tests_array(calibration_body(), n=2, k2={"weight": 2.5, "user_window": {"x_min": 0.0001, "x_max": 0.002}}))
    assert [t["test_key"] for t in v.snapshot["tests"]] == ["k1", "k2"]
    assert [t["weight"] for t in v.snapshot["tests"]] == [1.0, 2.5]
    assert v.snapshot["tests"][1]["user_window"] == {"x_min": 0.0001, "x_max": 0.002}
    assert len(v.runs) == 1 or len(v.runs) == 2  # 데이터가 같으므로 같은 run 내용
    spec = v.spec
    assert isinstance(spec, CalibrationSpec) and [t.weight for t in spec.tests] == [1.0, 2.5]
    assert dict(spec.tests[1].user_window) == {"x_min": 0.0001, "x_max": 0.002}


def test_test_key_rules(active_env):
    e = active_env
    b = as_tests_array(calibration_body(), n=2, k2={"test_key": "k1"})
    assert "SCHEMA_INVALID" in failed_codes(e, b)
    b = as_tests_array(calibration_body(), n=1, k1={"test_key": "Bad Key!"})
    assert "SCHEMA_INVALID" in failed_codes(e, b)


def test_tests_count_limit_from_config(make_env):
    e = make_env(config_over={"api.max_tests_per_job": 1})
    e.activate_recipe()
    assert "SCHEMA_INVALID" in failed_codes(e, as_tests_array(calibration_body(), n=2))


def test_run_count_limit_from_config(make_env):
    e = make_env(config_over={"api.max_runs_per_test": 1})
    e.activate_recipe()
    b = calibration_body()
    b["inline_runs"] = [inline_run("a"), inline_run("b")]
    assert "SCHEMA_INVALID" in failed_codes(e, b)


def test_slot_count_limit_from_config(make_env):
    e = make_env(config_over={"api.max_slots": 1})
    e.activate_recipe()
    b = calibration_body()
    b["materials"] = {"mode": "B", "slots": [b["materials"]["slots"][0], {**b["materials"]["slots"][0], "slot": "x"}]}
    assert "SCHEMA_INVALID" in failed_codes(e, b)


def test_run_ids_and_inline_are_exclusive_and_required(active_env):
    e = active_env
    assert "SCHEMA_INVALID" in failed_codes(e, calibration_body(test_run_ids=[RUN_ID]))
    b = calibration_body()
    del b["inline_runs"]
    assert "SCHEMA_INVALID" in failed_codes(e, b)


def test_non_uuid_run_id_is_schema_invalid(active_env):
    assert "SCHEMA_INVALID" in failed_codes(active_env, mn_body(test_run_ids=["not-a-uuid"]))


@pytest.mark.parametrize("patch", [
    {"seed": -1}, {"seed": 1.5}, {"budget": {"max_evaluations": 0}}, {"budget": {"max_evaluations": 2.5}},
    {"name": "x" * 201}, {"weight": 0}, {"displacement_channel": "bogus"}, {"plasticity_mode": "other"},
    {"materials": {"mode": "C", "slots": []}}, {"budget": {"max_evaluations": "20"}},
])
def test_schema_type_rules(active_env, patch):
    assert "SCHEMA_INVALID" in failed_codes(active_env, calibration_body(**patch))


def test_geometry_kind_is_an_extensible_enum(active_env):
    assert "GEOMETRY_KIND_NOT_ALLOWED" in failed_codes(active_env, calibration_body(geometry={"kind": "weird"}))


def test_numbers_must_be_numbers_not_strings_or_bools(active_env):
    r = inline_run()
    r["specimen_dimensions"]["width"] = True
    assert failed_codes(active_env, calibration_body(inline_runs=[r]))


def test_schema_version_forms(active_env):
    e = active_env
    for v in ("1.0",):
        validate(e, calibration_body(schema_version=v))
    assert "SCHEMA_VERSION_UNSUPPORTED" in failed_codes(e, calibration_body(schema_version="1.1"))
    assert "SCHEMA_VERSION_UNSUPPORTED" in failed_codes(e, calibration_body(schema_version="0.9"))
    assert "SCHEMA_INVALID" in failed_codes(e, calibration_body(schema_version="one"))
    b = calibration_body()
    del b["schema_version"]
    assert "SCHEMA_INVALID" in failed_codes(e, b)


# ---------------------------------------------------------------- 시험 데이터·치수
def test_dimension_priority_and_snapshot_geometry(active_env):
    e = active_env
    b = calibration_body(geometry={"kind": "box", "dimensions": {"thickness": 0.0042}}, recipe_params={"span": 0.05})
    v = validate(e, b)
    g = v.snapshot["tests"][0]["geometry"]
    assert g["dimensions"]["thickness"] == 0.0042 and g["dimensions"]["width"] == 0.010  # 요청 > run 치수
    assert g["dimension_overrides"] == {"thickness": 0.0042}
    assert v.snapshot["tests"][0]["recipe_params_si"] == {"span": 0.05}


def test_matnexus_dimension_outside_recipe_range_is_rejected(active_env):
    e = active_env
    r = inline_run()
    r["specimen_dimensions"].update(span=0.5, total_length=1.0)  # span 범위 [0.010, 0.300] 밖
    assert "RECIPE_PARAM_OUT_OF_RANGE" in failed_codes(e, calibration_body(inline_runs=[r]))
    # recipe_params 로 덮어쓰면 통과 (우선순위: recipe_params > 시편 치수)
    validate(e, calibration_body(inline_runs=[r], recipe_params={"span": 0.1}))


def test_layered_box_layer_sum_checked(active_env):
    e = active_env
    ref = {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}
    slots = [{"slot": "body", "role": "identify", "material_model": "mat001", "reference": ref}]
    g = {"kind": "layered_box", "layers": [{"slot": "body", "thickness": 0.002}, {"slot": "body", "thickness": 0.0021}]}
    assert "GEOMETRY_DIMENSION_INVALID" in failed_codes(e, calibration_body(geometry=g, materials={"mode": "A", "slots": slots}))
    good = {**g, "layers": [{"slot": "body", "thickness": 0.002}, {"slot": "body", "thickness": 0.002}]}
    assert validate(e, calibration_body(geometry=good, materials={"mode": "A", "slots": slots})).snapshot["tests"][0]["geometry"]["kind"] == "layered_box"


# ---------------------------------------------------------------- 경고
def test_warnings_density_compliance_and_recipe(active_env):
    e = active_env
    b = calibration_body()
    del b["materials"]["slots"][0]["reference"]["elastic"]["density"]
    v = validate(e, b)
    assert [w["code"] for w in v.warnings] == ["DENSITY_DEFAULTED"] and v.warnings[0]["slot"] == "body"
    # 픽스처 레시피에는 C 가 있으므로 COMPLIANCE_MISSING 은 없다. 장비가 다르면 경고.
    r = inline_run()
    r["instrument"] = "UTM-77"
    assert "COMPLIANCE_INSTRUMENT_MISMATCH" not in [w["code"] for w in validate(e, calibration_body(inline_runs=[r])).warnings]  # 레시피에 instruments 목록이 없다


def test_compliance_missing_warning_when_recipe_has_none(env):
    from be_support import recipe_zip

    text_edit = lambda t: t.replace("compliance: {value_m_per_n: 1.8e-7, method: rigid_block, calibrated_at: \"2026-10-01\"}\n", "")
    env.activate_recipe(recipe_zip(edit_manifest=text_edit))
    v = validate(env, calibration_body())
    assert "COMPLIANCE_MISSING" in [w["code"] for w in v.warnings]
    assert [w["test_key"] for w in v.warnings if w["code"] == "COMPLIANCE_MISSING"] == ["t1"]
    # 처짐 채널을 쓰면 컴플라이언스 경고가 없다 (displacement_channel=deflection 은 run 에 deflection 이 있어야 한다)
    r = inline_run()
    r["channels"]["deflection"] = copy.deepcopy(r["channels"]["displacement"])
    v2 = validate(env, calibration_body(inline_runs=[r], displacement_channel="deflection"))
    assert "COMPLIANCE_MISSING" not in [w["code"] for w in v2.warnings]


# ---------------------------------------------------------------- 재료 슬롯
def test_identify_slot_without_E_gets_placeholder_and_fixed_requires_E(active_env):
    e = active_env
    v = validate(e, calibration_body())
    ref = v.spec.materials.slots[0]["reference"]
    lo, hi = e.config.get("material.default_E_range_pa")
    assert lo < ref.elastic.youngs_modulus < hi
    fixed = {"slot": "body", "role": "fixed", "material_model": "mat001",
             "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}
    other = {"slot": "x", "role": "identify", "material_model": "mat001", "reference": {"elastic": {"poisson_ratio": 0.3}}}
    b = calibration_body(materials={"mode": "B", "slots": [fixed, other]})
    assert "FIXED_SLOT_REFERENCE_MISSING" in failed_codes(e, b)


def test_mode_b_requires_fixed_plastic_params(active_env):
    e = active_env
    base = {"slot": "body", "role": "identify", "material_model": "mat024", "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}
    assert "MODE_B_PLASTIC_UNSUPPORTED" in failed_codes(e, calibration_body(materials={"mode": "B", "slots": [base]}))
    fixed_all = {**base, "params": {n: {"fixed_value": v} for n, v in (("K", 5.0e8), ("eps0", 0.002), ("n", 0.2))}}
    v = validate(e, calibration_body(materials={"mode": "B", "slots": [fixed_all]}))  # 소성 변수를 모두 고정하면 통과
    assert v.snapshot["materials"]["mode"] == "B"


def test_param_override_forms_and_fixed_value(active_env):
    e = active_env
    ok = {"E": {"lower": 1e9, "upper": 4e11, "initial": 7e10}}
    v = validate(e, calibration_body(materials={"mode": "A", "slots": [{
        "slot": "body", "role": "identify", "material_model": "mat024", "params": ok,
        "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}]}))
    assert v.snapshot["materials"]["slots"][0]["params"] == ok
    for bad in ({"E": {"lower": 1e9, "upper": 1e9}}, {"E": {"lower": 1e9, "upper": 2e9, "initial": 5e9}}, {"E": {"lower": -1.0, "upper": 2e9}}):
        b = calibration_body(materials={"mode": "A", "slots": [{
            "slot": "body", "role": "identify", "material_model": "mat024", "params": bad,
            "reference": {"elastic": {"poisson_ratio": 0.3, "density": 2700.0}}}]})
        assert "PARAM_RANGE_INVALID" in failed_codes(e, b), bad


# ---------------------------------------------------------------- explicit_check 해석
def test_explicit_check_resolution_from_request_and_config(active_env, make_env):
    e = active_env
    v = validate(e, calibration_body(explicit_check={"enabled": True, "loading_velocity": 0.2, "max_ke_ratio": 0.01,
                                                     "section": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}}))
    ec = v.snapshot["explicit_check"]
    assert ec["enabled"] is True and ec["requested"] is True and ec["tests"] == ["t1"]
    assert ec["loading_velocity_m_s"] == 0.2 and ec["max_ke_ratio"] == 0.01 and ec["section"]["elform"] == 1
    assert validate(e, calibration_body(explicit_check={"enabled": False})).snapshot["explicit_check"] == {"enabled": False}
    # 설정 기본 켜짐 + 건너뛰기 조건 -> 경고 EXPLICIT_CHECK_SKIPPED (400 아님)
    e2 = make_env(config_over={"verify.explicit_check.enabled": True})
    e2.activate_recipe()
    v2 = validate(e2, calibration_body())
    assert v2.snapshot["explicit_check"]["enabled"] is True and v2.snapshot["explicit_check"]["requested"] is False


def test_explicit_check_unknown_test_key_is_schema_invalid(active_env):
    assert "SCHEMA_INVALID" in failed_codes(active_env, calibration_body(explicit_check={"enabled": True, "tests": ["zz"]}))


# ---------------------------------------------------------------- 스냅샷 <-> CalibrationSpec
def test_snapshot_roundtrip_builds_identical_spec(active_env):
    e = active_env
    v = validate(e, as_tests_array(calibration_body(), n=2))
    files = run_json_files(v.runs)
    runs = {r["run_id"]: run_from_json(json.loads(json.dumps(r))) for r in files.values()}
    spec2 = build_spec(json.loads(json.dumps(v.snapshot)), job_id="cal_unit", runs=runs,
                       recipe_resolver=lambda ref: e.ctx.recipes.resolved(e.ctx.recipes.get_best(*_split(ref))), stp_paths={})
    assert spec2.request_hash == v.spec.request_hash and spec2.seed == v.spec.seed
    assert [t.test_key for t in spec2.tests] == [t.test_key for t in v.spec.tests]
    for a, b in zip(spec2.tests, v.spec.tests):
        assert a.runs[0].fetched_hash == b.runs[0].fetched_hash and dict(a.geometry) == dict(b.geometry)
        assert a.recipe.content_hash == b.recipe.content_hash


def _split(ref):
    rid, _, ver = ref.partition("@")
    return rid, int(ver)


def test_run_json_roundtrip_preserves_arrays(active_env):
    v = validate(active_env, calibration_body())
    (rec,) = v.runs.values()
    again = run_from_json(json.loads(json.dumps(run_to_json(rec))))
    assert again.fetched_hash == rec.fetched_hash and again.channels["force"].tolist() == rec.channels["force"].tolist()
    assert list(run_json_files(v.runs)) == ["inline_spec01.json"]  # ':' 는 파일 이름에서 '_'


def test_parse_json_body_rejects_nan_and_garbage():
    with pytest.raises(ValidationFailed):
        parse_json_body(b"{bad")
    with pytest.raises(ValidationFailed):
        parse_json_body(b'{"a": NaN}')
    with pytest.raises(ValidationFailed):
        parse_json_body(b'{"a": Infinity}')
    with pytest.raises(ValidationFailed):
        parse_json_body(b"\xff\xfe")
    assert parse_json_body('{"a": "한글"}'.encode()) == {"a": "한글"}


def test_material_id_from_request_or_first_run(active_env):
    e = active_env
    assert validate(e, mn_body()).material_id == "3c3c3c3c-0000-4000-8000-000000000001"
    forced = "9d9d9d9d-0000-4000-8000-000000000009"
    assert validate(e, mn_body(material_ref={"matnexus_material_id": forced})).material_id == forced
    assert validate(e, calibration_body()).material_id is None
