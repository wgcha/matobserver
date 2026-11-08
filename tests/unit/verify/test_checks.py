"""S-8 레시피 dry-run 검사 코드별 PASS/FAIL 판정 (recipe.md §7.2) + S-9 check_reference_curve (§4.6)."""

from __future__ import annotations

import numpy as np
import pytest

from matobserver.core import errors as E
from matobserver.core.config import Config
from matobserver.core.types import (
    CurveData,
    Extracted,
    ExtractorRef,
    ResponseDef,
    ResponseWindow,
    RunState,
    RunStatus,
)
from matobserver.verify import recipe_checks as rc

CFG = Config()
RESP = ResponseDef("force_disp", "curve", "force", "displacement",
                   {"x": ExtractorRef("nodout_disp", {}), "y": ExtractorRef("rcforc_resultant", {})}, 1.0,
                   ResponseWindow(None, None, "auto", None))


def _status(state=RunState.NORMAL, fallback=False):
    return RunStatus(state, "explicit" if fallback else "implicit", fallback, 1.0, "Normal termination", "R13", 0, ())


def _curve(k=1000.0, n=50, xmax=0.004, f0=0.0, plastic_at=None):
    x = np.linspace(0, xmax, n)
    y = k * x
    if plastic_at is not None:
        y = np.where(x <= plastic_at, k * x, k * plastic_at + 0.1 * k * (x - plastic_at))
    y = y.copy()
    y[1] += f0
    return CurveData(x, y, "displacement", "force")


def _ex(curve=None, energy=None):
    return Extracted({"force_disp": curve} if curve is not None else {}, {}, {}, energy or {})


def test_termination_pass_fail_and_fallback_warn():
    assert rc.check_termination(_status()) == [{"code": E.VERIFY_TERMINATION, "result": "pass",
                                                "detail": "Normal termination (implicit)"}]
    out = rc.check_termination(_status(fallback=True))
    assert [(c["code"], c["result"]) for c in out] == [(E.VERIFY_TERMINATION, "pass"), (E.VERIFY_FALLBACK_USED, "warn")]
    for st in (RunState.ERROR, RunState.TIMEOUT, RunState.DIVERGED, RunState.LICENSE_TIMEOUT):
        assert rc.check_termination(_status(st))[0]["result"] == "fail"


def test_response_pass_and_fail_modes():
    assert rc.check_response(_ex(_curve()), [RESP], CFG)["result"] == "pass"
    assert rc.check_response(_ex(_curve(n=10)), [RESP], CFG)["result"] == "fail"  # < min_points(20)
    assert rc.check_response(_ex(None), [RESP], CFG)["result"] == "fail"
    assert rc.check_response(_ex(_curve(k=-5.0)), [RESP], CFG)["result"] == "fail"  # F_peak ≤ 0
    x = np.concatenate([[0.0], np.full(30, 0.001)])
    assert rc.check_response(_ex(CurveData(x, x, "displacement", "force")), [RESP], CFG)["result"] == "fail"


def test_units_parametric_and_fixed_ranges():
    k_th = 1000.0
    assert rc.check_units(_curve(k=1070.0), k_th, "parametric", CFG)["result"] == "pass"
    c = rc.check_units(_curve(k=1600.0), k_th, "parametric", CFG)
    assert c["result"] == "fail" and c["value"] == pytest.approx(1.6) and c["threshold"] == 1.5
    assert rc.check_units(_curve(k=1600.0), k_th, "fixed", CFG)["result"] == "pass"
    assert rc.check_units(_curve(k=1000.0 * 1000), k_th, "fixed", CFG)["result"] == "fail"  # 자릿수 오류
    assert rc.check_units(_curve(k=1000.0 / 15), k_th, "fixed", CFG)["result"] == "fail"
    assert rc.check_units(None, k_th, "fixed", CFG)["result"] == "skipped"


def test_beam_stiffness_formula():
    assert rc.beam_stiffness(2e9, 0.01, 0.004, 0.064) == pytest.approx(4 * 2e9 * 0.01 * 0.004 ** 3 / 0.064 ** 3)


def test_penetration_pass_and_two_fail_modes():
    assert rc.check_penetration(_curve(n=200), None, CFG)["result"] == "pass"
    assert rc.check_penetration(_curve(n=200), 5e-7, CFG)["result"] == "pass"
    assert rc.check_penetration(_curve(n=200), 3.5e-6, CFG)["result"] == "fail"
    assert rc.check_penetration(_curve(n=200, f0=0.5), None, CFG)["result"] == "fail"  # 첫 출력 0.52 N / 4 N > 1%


def test_mesh_convergence_pass_fail_skip():
    a, b = _curve(k=1000.0, plastic_at=0.002), _curve(k=1010.0, plastic_at=0.002)
    assert rc.check_mesh_convergence([(2.0, a), (1.0, b)], ["initial_slope", "force_at_end"], CFG)["result"] == "pass"
    c = _curve(k=1100.0, plastic_at=0.002)
    out = rc.check_mesh_convergence([(2.0, a), (1.0, c)], ["initial_slope", "force_at_end"], CFG)
    assert out["result"] == "fail" and out["value"] > 0.02
    assert rc.check_mesh_convergence([(1.0, a)], ["initial_slope"], CFG)["result"] == "skipped"


def test_warn_checks():
    assert rc.warn_check(E.WARN_MASS_SCALING, [])["result"] == "pass"
    assert rc.warn_check(E.WARN_DAMPING, ["x.k: *DAMPING_GLOBAL"]) == {"code": E.WARN_DAMPING, "result": "warn",
                                                                      "detail": "x.k: *DAMPING_GLOBAL"}
    assert rc.check_ke_ratio(_ex(energy={"ke_ie_ratio_max": 0.2}), "explicit", CFG)["result"] == "warn"
    assert rc.check_ke_ratio(_ex(energy={"ke_ie_ratio_max": 0.01}), "explicit", CFG)["result"] == "pass"
    assert rc.check_ke_ratio(_ex(), "implicit", CFG)["result"] == "skipped"
    assert rc.check_no_fallback({"solver": {"analysis": {"primary": "implicit", "fallback": "none"}}})["result"] == "warn"
    assert rc.check_no_fallback({"solver": {"analysis": {"primary": "implicit", "fallback": "explicit"}}})["result"] == "pass"
    assert rc.check_compliance({"compliance": None})["result"] == "warn"
    assert rc.check_compliance({"compliance": {"value_m_per_n": 1e-7}})["result"] == "pass"


def test_report_status():
    p, w, f = {"result": "pass"}, {"result": "warn"}, {"result": "fail"}
    assert rc.report_status([p], False) == "passed"
    assert rc.report_status([p, w], False) == "passed_with_warnings"
    assert rc.report_status([p, w], True) == "failed"
    assert rc.report_status([p, f], False) == "failed"


# ---------------------------------------------------------------- check_reference_curve
def _write_csv(path, curve, scale=1.0):
    path.write_text("x_m,force_n\n" + "".join(f"{float(x)!r},{float(y) * scale!r}\n" for x, y in zip(curve.x, curve.y)))


def test_reference_curve_judgement_with_stub_engine(tmp_path, monkeypatch):
    """판정 규칙(J ≤ reference_curve_tol, Ω 전체 split=none)을 엔진 대역으로 확인."""
    seen = {}

    def align_pair(exp, sim, response, cfg):
        seen["split"] = response.window.split
        return (exp, sim)

    def curve_error(pair, weights, cfg):
        exp, sim = pair
        return float(np.linalg.norm(np.interp(exp.x, sim.x, sim.y) - exp.y) / np.linalg.norm(exp.y))

    monkeypatch.setattr(rc, "_engine_funcs", lambda: (align_pair, curve_error))
    c = _curve(plastic_at=0.002)
    _write_csv(tmp_path / "ok.csv", c, 1.02)
    _write_csv(tmp_path / "bad.csv", c, 1.2)
    ok = rc.check_reference_curve(c, tmp_path / "ok.csv", RESP, CFG)
    assert ok["code"] == E.VERIFY_REFERENCE_CURVE and ok["result"] == "pass" and seen["split"] == "none"
    assert ok["value"] == pytest.approx(0.02 / 1.02, rel=1e-6) and ok["threshold"] == 0.05
    assert rc.check_reference_curve(c, tmp_path / "bad.csv", RESP, CFG)["result"] == "fail"
    assert rc.check_reference_curve(c, tmp_path / "missing.csv", RESP, CFG)["result"] == "fail"


def test_reference_curve_with_engine_functions(tmp_path):
    pytest.importorskip("matobserver.engine.alignment")
    pytest.importorskip("matobserver.engine.objective")
    c = _curve(k=1.7e4, n=200, plastic_at=0.002)
    _write_csv(tmp_path / "same.csv", c, 1.0)
    _write_csv(tmp_path / "off.csv", c, 1.3)
    same = rc.check_reference_curve(c, tmp_path / "same.csv", RESP, CFG)
    assert same["result"] == "pass" and same["value"] <= 1e-9
    assert rc.check_reference_curve(c, tmp_path / "off.csv", RESP, CFG)["result"] == "fail"
