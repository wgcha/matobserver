"""G2 가상 역식별 — (1.2) 노이즈 0% 개별 기준은 이 파일, 노이즈 0.5% 분포 기준(시드 1–13)은 test_g2_distribution.py.
 (module-boundaries.md §5.3 O-4·O-5·O-6·O-7) — surrogate_beam 가상 곡선, LS-DYNA 없음.

설정 기본값(계약 1.1 확정: `alignment.elastic_dev_tol` = 0.01, `first_node_strain` = 0.002, 탄소성 기본 lsq)으로 엄격 기준
(테이블 3%, J 0.01)을 판정한다. 1.0 값 0.05와 1.1 초안값 0.015의 거동은 명시적 설정의 기록 테스트로 남긴다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import mo_optim_doubles as D
from matobserver.core import errors as E
from matobserver.core.config import Config
from matobserver.engine import preflight, run_calibration

W, H, L, E_TRUE = 0.010, 0.004, 0.064, 2.3e9
SWIFT = (1.0e8, 0.02, 0.2)



def _single(tmp: Path, model: str, *, noise: float, options=None, optimizer=("", {}), n_eval=120, cfg=None,
            plastic=True, n_runs=1, params=None):
    mats = {"body": (E_TRUE, D.swift_table(*SWIFT) if plastic else None)}
    runs = []
    for i in range(n_runs):
        x, F = D.synthetic_curve(mats, width=W, thickness=H, span=L, d_max=0.008, compliance=1.8e-7, noise=noise, seed=i + 1)
        runs.append(D.make_run(f"r{i}", x, F, width=W, thickness=H, span=L))
    t = D.make_test("t1", runs, D.make_manifest(), {"kind": "box", "dimensions": {"total_length": 0.08}}, store_dir=tmp)
    slot = {"slot": "body", "role": "identify", "material_model": model, "reference": D.reference(),
            "options": options or {}}
    if params:
        slot["params"] = params
    spec = D.make_spec([t], [slot], optimizer=optimizer, max_evaluations=n_eval)
    svc = D.FakeServices(tmp, D.make_registry(), Config(dict(cfg or {})))
    return run_calibration(spec, svc), svc


def _table_err(slot, valid=True):
    tb = slot["card_blocks"]["table"]
    ep = np.array([r["plastic_strain"] for r in tb])
    sg = np.array([r["true_stress"] for r in tb])
    m = ep <= slot["valid_range"]["max_plastic_strain"] * (1 + 1e-9) if valid else np.ones(len(ep), bool)
    K, e0, n = SWIFT
    return float(np.max(np.abs(sg[m] / (K * (e0 + ep[m]) ** n) - 1)))


@pytest.mark.parametrize("noise", [0.0])
def test_o4_elastic_mode_a(tmp_path, noise):
    res, _ = _single(tmp_path, "mat001", noise=noise, plastic=False)
    s = res["slots"][0]
    err = s["card_blocks"]["elastic"]["youngs_modulus"] / E_TRUE - 1
    assert abs(err) <= 0.01, err
    assert s["card_blocks"]["table"] is None and s["valid_range"] is None
    assert res["metadata"]["termination"]["per_stage"]["elastic"]["evaluations"] <= 20


@pytest.mark.parametrize("noise", [0.0])
@pytest.mark.parametrize("opt,tab_tol", [("", 0.03), ("bo", None)])
def test_o5_coeff_swift(tmp_path, noise, opt, tab_tol):
    """"" = 설정 기본 최적화기(1.1: lsq). 정렬 설정은 기본값."""
    res, _ = _single(tmp_path, "mat024_coeff", noise=noise, optimizer=(opt, {}))
    s = res["slots"][0]
    J = res["metadata"]["residuals"]["J"]
    assert J <= (0.01 if noise == 0 else 0.012), J  # O-5: 노이즈 0% J ≤ 0.01 (노이즈 시 1.2·J참값 판정은 verifier 조건 테스트)
    if tab_tol is not None:  # 1.1: 계수 비유일 → 판정은 테이블 기준 (BO는 계수 강상관으로 3–11%, 판정 대상 아님)
        assert _table_err(s) <= tab_tol
    assert s["hardening"]["law"] == "swift" and s["valid_range"]["max_plastic_strain"] > 0.03
    assert abs(s["card_blocks"]["elastic"]["youngs_modulus"] / E_TRUE - 1) <= 0.01
    # 결과 테이블은 εp ∈ [0, εp_valid] 50점 (result.md §3.1)
    tb = s["card_blocks"]["table"]
    assert len(tb) == 50 and tb[-1]["plastic_strain"] == pytest.approx(s["valid_range"]["max_plastic_strain"])


@pytest.mark.parametrize("noise", [0.0])
def test_o5_table(tmp_path, noise):
    """설정 기본값(elastic_dev_tol 0.01, first_node_strain 0.002, lsq)에서 테이블 3%."""
    res, _ = _single(tmp_path, "mat024_table", noise=noise, n_eval=200, options={"table_nodes": 5, "max_strain": 0.04})
    s = res["slots"][0]
    assert _table_err(s) <= 0.03
    assert res["metadata"]["residuals"]["J"] <= (0.01 if noise == 0 else 0.012)
    stresses = [r["true_stress"] for r in s["card_blocks"]["table"]]
    assert all(b >= a for a, b in zip(stresses, stresses[1:]))


def test_o5_exact_E_table_recovery(tmp_path):
    """E가 정확하면(fixed_value) 기본 정렬 설정에서도 테이블 7노드 복원 ≤ 1%."""
    res, _ = _single(tmp_path, "mat024_table", noise=0.0, optimizer=("lsq", {}), n_eval=120,
                     options={"table_nodes": 7, "max_strain": 0.06, "smoothing_weight": 0.0},
                     params={"E": {"fixed_value": E_TRUE}})  # 정칙화 없이 순수 식별성 확인
    assert _table_err(res["slots"][0]) <= 0.01
    assert res["metadata"]["residuals"]["J"] <= 0.001


def test_record_v11_draft_tolerance(tmp_path):
    """1.1 초안값 기록: elastic_dev_tol=0.015(명시). 정칙화 전 3.3–3.5%(0.01 확정 근거), 정칙화 후 ≤ 4% 범위 기록."""
    res, _ = _single(tmp_path, "mat024_table", noise=0.0, n_eval=200, options={"table_nodes": 5, "max_strain": 0.04},
                     cfg={"alignment.elastic_dev_tol": 0.015})
    assert _table_err(res["slots"][0]) <= 0.04


def test_elastic_bias_v10_tolerance(tmp_path):
    """1.0 기본 elastic_dev_tol=0.05: 점진 항복 재료에서 탄성 단계 E 편향 −2%~−0.5% (1.0 값 기록)."""
    res, _ = _single(tmp_path, "mat024_coeff", noise=0.0, optimizer=("random", {}), n_eval=20,
                     cfg={"identifiability.post_fit": False, "alignment.elastic_dev_tol": 0.05})
    err = res["slots"][0]["card_blocks"]["elastic"]["youngs_modulus"] / E_TRUE - 1
    assert -0.02 <= err < -0.005


def _mode_b(tmp: Path, *, both=True, noise=0.005):
    Ea, Eb = 3.0e9, 0.8e9
    cfgs = [("tz", "z", "quarter", [("b", 0.0012), ("a", 0.0016), ("b", 0.0012)]),
            ("ty", "y", "none", [("a", 0.006), ("b", 0.004)])]
    tests = []
    for key, ax, sym, lw in (cfgs if both else cfgs[:1]):
        x, F = D.synthetic_curve({"a": (Ea, None), "b": (Eb, None)}, width=W, thickness=H, span=L, d_max=0.004, layers=lw,
                                 stack_axis=ax, compliance=1.8e-7, noise=noise, seed=len(tests) + 1)
        man = D.make_manifest(slots=("a", "b"), symmetry=sym, layered_regions=True)
        geo = {"kind": "layered_box", "dimensions": {"total_length": 0.08}, "stack_axis": ax,
               "layers": [{"slot": s, "thickness": t} for s, t in lw]}
        tests.append(D.make_test(key, [D.make_run(key + "r", x, F, width=W, thickness=H, span=L)], man, geo, store_dir=tmp))
    slots = [{"slot": s, "role": "identify", "material_model": "mat001", "reference": D.reference()} for s in ("a", "b")]
    return D.make_spec(tests, slots, mode="B"), {"a": Ea, "b": Eb}


def test_o6_mode_b_two_slots_two_tests(tmp_path):
    spec, truth = _mode_b(tmp_path)
    svc = D.FakeServices(tmp_path, D.make_registry(), Config())
    assert [i for i in preflight(spec, svc) if i.severity == "error"] == []
    res = run_calibration(spec, svc)
    for s in res["slots"]:
        assert abs(s["card_blocks"]["elastic"]["youngs_modulus"] / truth[s["slot"]] - 1) <= 0.02
    assert res["metadata"]["uncertainty"]["notes"] == ["MODE_B_SCALING_NA"]
    assert res["metadata"]["identifiability"]["precheck"]["verdict"] == "ok"
    assert {c["stack_axis"] for c in res["metadata"]["equivalence"]["configurations"]} == {"z", "y"}


def test_o7_underdetermined_precheck(tmp_path):
    spec, _ = _mode_b(tmp_path, both=False)
    svc = D.FakeServices(tmp_path, D.make_registry(), Config())
    iss = preflight(spec, svc)
    assert [(i.code, i.severity) for i in iss if i.code == E.IDENTIFIABILITY_UNDERDETERMINED] == [
        (E.IDENTIFIABILITY_UNDERDETERMINED, "error")]
    warn_svc = D.FakeServices(tmp_path, D.make_registry(), Config({"identifiability.precheck_mode": "warn"}))
    iss2 = preflight(spec, warn_svc)
    assert [i.severity for i in iss2 if i.code == E.IDENTIFIABILITY_UNDERDETERMINED] == ["warning"]
    res = run_calibration(spec, warn_svc)  # warn 모드: 잡은 돌고 결과에 verdict 기록
    assert res["metadata"]["identifiability"]["precheck"]["verdict"] == "underdetermined"


def test_o7_high_correlation_table_nodes(tmp_path):
    res, _ = _single(tmp_path, "mat024_table", noise=0.0, optimizer=("lsq", {}), n_eval=60,
                     options={"table_nodes": 8, "max_strain": 0.2})
    flags = {f["code"] for f in res["metadata"]["identifiability"]["flags"]}
    assert E.HIGH_CORRELATION in flags
    codes = {w["code"] for w in res["metadata"]["warnings"]}
    assert E.HIGH_CORRELATION in codes and E.TABLE_NODES_BEYOND_VALID_RANGE in codes
    pf = res["metadata"]["identifiability"]["post_fit"]
    assert pf and all(p["relative_stdev_is_lower_bound"] is True for p in pf)
    assert res["metadata"]["identifiability"]["recommendations"]


def test_low_data_warning_precheck(tmp_path):
    res, _ = _single(tmp_path, "mat024_table", noise=0.0, optimizer=("random", {}), n_eval=15,
                     options={"table_nodes": 40, "max_strain": 0.06}, cfg={"identifiability.post_fit": False})
    assert E.IDENTIFIABILITY_LOW_DATA in res["metadata"]["identifiability"]["precheck"]["codes"]
    assert E.IDENTIFIABILITY_LOW_DATA in {w["code"] for w in res["metadata"]["warnings"]}


