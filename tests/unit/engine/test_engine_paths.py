"""엔진 경로: 재현성(O-8), 실패 페널티(O-9), 결과 스키마(O-10), 탄성 전용 카드(O-11), 비재료 변수(O-12),
왕복 검사(O-13), explicit 재현 검사(O-14), 기준 변형률속도(O-15), 취소·실패 코드·단계 진행."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import numpy as np
import pytest

import mo_optim_doubles as D
from matobserver.core import errors as E
from matobserver.core.config import Config
from matobserver.core.errors import EngineFailure
from matobserver.core.types import RunState
from matobserver.engine import CalibrationCancelled, run_calibration
from matobserver.engine import roundtrip as RT
from matobserver.engine.result_schema import RESULT_SCHEMA
from matobserver.solvers.surrogate.adapter import SurrogateBeamAdapter

W, H, L, E_TRUE = 0.010, 0.004, 0.064, 2.3e9
TIME_KEYS = {"created_at", "finished_at", "started_at", "wall_time_s", "elapsed_s"}


def _spec(tmp: Path, *, model="mat024_coeff", solver=("surrogate_beam", {}), optimizer=("bo", {}), n_eval=30,
          n_runs=2, test_speed=3.3e-5, test_type=None, manifest_kw=None, seed=20261108):
    runs = []
    for i in range(n_runs):
        x, F = D.synthetic_curve({"body": (E_TRUE, D.swift_table(1e8, 0.02, 0.2))}, width=W, thickness=H, span=L,
                                 d_max=0.008, compliance=1.8e-7, noise=0.003, seed=i + 1)
        runs.append(D.make_run(f"r{i}", x, F, width=W * (1 + 0.002 * i), thickness=H, span=L, test_speed=test_speed))
    t = D.make_test("t1", runs, D.make_manifest(**(manifest_kw or {})), {"kind": "box", "dimensions": {"total_length": 0.08}},
                    store_dir=tmp)
    if test_type:
        t = replace(t, test_type=test_type)
    return D.make_spec([t], [{"slot": "body", "role": "identify", "material_model": model, "reference": D.reference()}],
                       optimizer=optimizer, solver=solver, max_evaluations=n_eval, seed=seed)


def _svc(tmp: Path, cfg=None, sub="a"):
    return D.FakeServices(tmp / sub, D.make_registry(D.double_entries()), Config(cfg or {}))


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in TIME_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def test_o8_reproducible(tmp_path):
    r1 = run_calibration(_spec(tmp_path), _svc(tmp_path, sub="a"))
    r2 = run_calibration(_spec(tmp_path), _svc(tmp_path, sub="b"))
    assert json.dumps(_strip(r1), sort_keys=True) == json.dumps(_strip(r2), sort_keys=True)
    t1, t2 = r1["metadata"]["traceability"], r2["metadata"]["traceability"]
    assert t1["input_hash"] == t2["input_hash"] and t1["input_hash"].startswith("sha256:")
    r3 = run_calibration(_spec(tmp_path, seed=7), _svc(tmp_path, sub="c"))
    assert r3["metadata"]["traceability"]["input_hash"] != t1["input_hash"]


def test_o9_failure_penalty(tmp_path):
    svc = _svc(tmp_path)
    spec = _spec(tmp_path, solver=("flaky_beam", {"fail_rate": 0.3, "seed": 4}), n_eval=40)
    res = run_calibration(spec, svc)
    pen = [h for h in svc.evals if h["status"] == "penalized"]
    failed = [h for h in svc.evals if "error" in h["run_status"].values()]
    assert failed and all(h["J"] == pytest.approx(10.0) for h in failed if h["stage"] != "diagnostics")
    assert len(pen) >= len([h for h in failed if h["stage"] != "diagnostics"])
    assert res["metadata"]["termination"]["evaluations_failed"] > 0
    assert res["metadata"]["termination"]["evaluations"] == 40  # 루프 유지(예산 소진까지)


def test_all_initial_failed(tmp_path):
    with pytest.raises(EngineFailure) as ei:
        run_calibration(_spec(tmp_path, solver=("flaky_beam", {"fail_rate": 1.0})), _svc(tmp_path))
    assert ei.value.code == E.ALL_INITIAL_RUNS_FAILED


def test_o10_result_schema(tmp_path):
    res = run_calibration(_spec(tmp_path), _svc(tmp_path, {"verify.explicit_check.enabled": True}))
    jsonschema.validate(res, RESULT_SCHEMA)
    json.dumps(res, allow_nan=False)
    m = res["metadata"]
    codes = {a["code"] for a in m["assumptions"]}
    assert {"J2_ISOTROPIC_HARDENING", "RATE_INDEPENDENT", "ISOTROPIC_EQUIVALENT", "RIGID_JIG",
            "COMPLIANCE_FROM_RECIPE"} <= codes
    assert m["compliance"][0]["applied"] is True and m["uncertainty"]["method"] == "per_specimen_scaling"
    st = m["uncertainty"]["statistics"]["body.E"]
    assert st["n"] == 2 and abs(st["mean"] / E_TRUE - 1) < 0.03
    kinds = {a["kind"] for a in res["artifacts"]}
    assert {"card_blocks_json", "curve_compare", "curve_raw", "history", "convergence_plot_data", "deck_best", "mesh",
            "roundtrip", "identifiability_json", "explicit_check", "explicit_check_curves"} <= kinds
    assert m["tests"][0]["cases"][0]["case_id"] == "t1-c1" and len(m["tests"][0]["cases"]) == 1  # 치수 0.2% 차 → 1케이스
    assert m["traceability"]["plugins"]["optimizer"] == {"lsq": "1.0.0", "bo": "1.0.0"}


def test_o11_elastic_stage_card_has_no_table(tmp_path):
    D.SPY_LOG.clear()
    svc = _svc(tmp_path)
    run_calibration(_spec(tmp_path, solver=("spy_beam", {}), n_eval=12), svc)
    stages = [h["stage"] for h in svc.evals]
    n_el = stages.count("elastic")
    assert n_el >= 1
    assert all(e["materials"]["body"].table is None for e in D.SPY_LOG[:n_el])
    plastic_idx = stages.index("plastic")
    assert D.SPY_LOG[plastic_idx]["materials"]["body"].table is not None


def test_o12_non_material_param(tmp_path):
    D.SPY_LOG.clear()
    svc = _svc(tmp_path)
    res = run_calibration(_spec(tmp_path, model="mat001", solver=("spy_beam", {}), test_type="bending_mu", n_eval=15), svc)
    assert "_test.t1.mu" in svc.evals[0]["theta"]
    for h, e in zip(svc.evals, D.SPY_LOG):
        assert e["case"].params_si["fric"] == pytest.approx(h["theta"]["_test.t1.mu"])
    idp = res["metadata"]["tests"][0]["identified_params"]
    assert set(idp) == {"mu"} and idp["mu"]["unit"] == "1"


def test_o13_roundtrip_check(tmp_path):
    ad = SurrogateBeamAdapter({})
    from matobserver.core.types import ElasticBlock, NeutralMaterial, TableRow

    m = NeutralMaterial(ElasticBlock(2e9, 0.3, 1000.0), tuple(TableRow(a, b) for a, b in D.swift_table(1e8, 0.02, 0.2,
                                                                                                         0.1, 20)))
    r = RT.check("body", m, ad, tmp_path / "rt", 0.05, 0.01)
    assert set(r) >= {"slot", "passed", "max_rel_err", "tolerance", "artifact_id"}
    assert r["passed"] and r["max_rel_err"] <= 1e-9 and r["tolerance"] == 0.01

    class Bad(SurrogateBeamAdapter):
        def single_element_uniaxial(self, material, strain_max, workdir, timeout_s):
            c = super().single_element_uniaxial(material, strain_max, workdir, timeout_s)
            return replace(c, y=c.y * 1.05)

    assert RT.check("body", m, Bad({}), tmp_path / "rt2", 0.05, 0.01)["passed"] is False


@pytest.mark.parametrize("opts,status,code", [
    ({}, "ok", None),
    ({"explicit_scale": 1.2}, "deviation", E.EXPLICIT_CHECK_DEVIATION),
    ({"explicit_fail": True}, "failed", E.EXPLICIT_CHECK_FAILED),
    ({"no_overrides": True}, "skipped", E.EXPLICIT_CHECK_SKIPPED),
    ({"explicit_ke": 0.2}, "ok", E.EXPLICIT_CHECK_KE_HIGH),
])
def test_o14_explicit_check(tmp_path, opts, status, code):
    D.ExplicitBiasAdapter.ORDER.clear()
    res = run_calibration(_spec(tmp_path, solver=("xc_beam", opts), n_eval=12, n_runs=1),
                          _svc(tmp_path, {"verify.explicit_check.enabled": True}))  # 잡 성공(경고만)
    xc = res["metadata"]["explicit_check"]
    assert xc["enabled"] and xc["settings"]["elform"] == 1 and xc["settings"]["hourglass"] == {"ihq": 6, "qm": 0.1}
    pc = xc["per_case"][0]
    assert pc["status"] == status
    codes = {w["code"] for w in res["metadata"]["warnings"]}
    if code:
        assert code in codes
    if status == "ok" and not code:
        assert not codes & {E.EXPLICIT_CHECK_DEVIATION, E.EXPLICIT_CHECK_FAILED, E.EXPLICIT_CHECK_SKIPPED}
        assert pc["vs_implicit"]["elastic"] == pytest.approx(0.0, abs=1e-3)  # 출력 간격 차이만
        assert pc["within_tolerance"] is True
    if status == "deviation":
        assert pc["vs_implicit"]["peak_force_rel_diff"] == pytest.approx(0.2, rel=1e-3)
    if status == "skipped":
        assert pc["skip_reason"] == "adapter_unsupported"
    if status != "skipped":
        order = D.ExplicitBiasAdapter.ORDER
        assert "explicit" in order and "roundtrip" in order and order.index("roundtrip") < order.index("explicit")
    assert res["metadata"]["explicit_check"] != {"enabled": False}


def test_explicit_check_disabled_and_no_controls(tmp_path):
    res = run_calibration(_spec(tmp_path, n_eval=8, n_runs=1), _svc(tmp_path))
    assert res["metadata"]["explicit_check"] == {"enabled": False}
    res2 = run_calibration(_spec(tmp_path, n_eval=8, n_runs=1, manifest_kw={"explicit_controls": False}),
                           _svc(tmp_path, {"verify.explicit_check.enabled": True}, sub="b"))
    assert res2["metadata"]["explicit_check"]["per_case"][0]["skip_reason"] == "no_explicit_controls"


def test_o15_rate(tmp_path):
    res = run_calibration(_spec(tmp_path, n_eval=8), _svc(tmp_path))
    rate = res["metadata"]["rate"]
    v = 6 * H * 3.3e-5 / L**2
    assert [r["value"] for r in rate["reference_strain_rate_1_s"]["per_run"]] == [pytest.approx(v), pytest.approx(v)]
    assert rate["reference_strain_rate_1_s"]["per_test"][0]["median"] == pytest.approx(v)
    assert rate["rate_dependence_identified"] is False and rate["advisory"]["code"] == E.RATE_DEPENDENCE_NOT_IDENTIFIED
    assert E.RATE_DEPENDENCE_NOT_IDENTIFIED in {w["code"] for w in res["metadata"]["warnings"]}
    res2 = run_calibration(_spec(tmp_path, n_eval=8, test_speed=None), _svc(tmp_path, sub="b"))
    assert res2["metadata"]["rate"]["reference_strain_rate_1_s"]["per_run"][0]["value"] is None
    assert E.REFERENCE_RATE_UNKNOWN in {w["code"] for w in res2["metadata"]["warnings"]}


def test_cancel(tmp_path):
    svc = _svc(tmp_path)
    svc.cancel = D.FlagCancel()
    svc.cancel.flag = True
    with pytest.raises(CalibrationCancelled):
        run_calibration(_spec(tmp_path, n_eval=8), svc)


def test_compliance_missing_and_density_default(tmp_path):
    spec = _spec(tmp_path, n_eval=6, n_runs=1, manifest_kw={"compliance": None})
    slots = (dict(spec.materials.slots[0], reference={"elastic": {"poisson_ratio": 0.38}}),)
    spec = replace(spec, materials=replace(spec.materials, slots=slots))
    res = run_calibration(spec, _svc(tmp_path))
    codes = {w["code"] for w in res["metadata"]["warnings"]}
    assert {E.COMPLIANCE_MISSING, E.DENSITY_DEFAULTED} <= codes
    assert res["metadata"]["compliance"][0] | {} == res["metadata"]["compliance"][0]
    assert res["metadata"]["compliance"][0]["reason"] == "missing"
    assert res["slots"][0]["fixed_params"]["density"]["source"] == "config.default"


def test_progress_and_history_shape(tmp_path):
    svc = _svc(tmp_path)
    run_calibration(_spec(tmp_path, n_eval=10), svc)
    stages = [s for s, _ in svc.progress_log]
    for st in ("preprocess", "elastic", "plastic", "diagnostics", "finalize"):
        assert st in stages
    assert stages.index("elastic") < stages.index("plastic") < stages.index("finalize")
    h = svc.evals[0]
    assert set(h) >= {"eval_index", "stage", "theta", "J", "J_components", "status", "run_status", "mode_used",
                      "wall_time_s", "is_best", "started_at"}
    assert [e["eval_index"] for e in svc.evals] == list(range(len(svc.evals)))
    assert sum(1 for e in svc.evals if e["stage"] in ("elastic", "plastic")) == 10
