"""정렬 8단계 수치 규칙(O-2), 컴플라이언스 1회 적용(O-3), 공개 함수 형식(O-13), 목적함수(testspec.md §8)."""

from __future__ import annotations

import inspect

import numpy as np
import pytest

import mo_optim_doubles as D
from matobserver.core import errors as E
from matobserver.core import types as T
from matobserver.core.config import Config
from matobserver.engine import alignment as AL
from matobserver.engine import objective as OBJ
from matobserver.engine import roundtrip, explicit_check

CFG = Config()
RESP = T.ResponseDef("force_disp", "curve", "force", "displacement", {}, 1.0, T.ResponseWindow(None, None, "auto", None))


def _bilinear(n=400, k=1000.0, xy=0.002, k2=100.0, xmax=0.006, toe=0.0):
    x = np.linspace(0, xmax, n)
    y = np.where(x < xy, k * x, k * xy + k2 * (x - xy))
    return x + toe, y


def test_step1_unload_dedup_nan():
    x, y = _bilinear()
    x = np.concatenate([x, [x[-1], x[-1] - 1e-3, x[-1] - 2e-3]])
    y = np.concatenate([y, [y[-1], y[-1] * 0.5, 0.0]])
    x[10] = np.nan
    n = AL.normalize(x, y, CFG)
    assert np.all(np.diff(n.x) > 0) and np.all(np.isfinite(n.y))
    assert len(n.x) == 399  # NaN 1점 제거, 끝 중복 x·제하 구간 절단
    # 작은 역행(허용 오차 이하)은 누적 최대 갱신 점만 남기고 계속
    x2 = np.linspace(0, 1, 100); x2[50] = x2[49] - 1e-4
    assert len(AL.normalize(x2, x2, CFG).x) == 99


def test_step1_too_few_and_not_monotonic():
    x, y = _bilinear(n=10)
    assert E.CURVE_TOO_FEW_POINTS in {i.code for i in AL.prepare_test(x, y, RESP, CFG).issues}
    x, y = _bilinear(n=100)
    x = np.concatenate([x, x[::-1][1:] * 0.0 + np.linspace(x[-1], -0.05, 99)])
    y = np.concatenate([y, np.full(99, y[-1])])
    assert E.CURVE_NOT_MONOTONIC in {i.code for i in AL.prepare_test(x, y, RESP, CFG).issues}


def test_step3_toe_correction_and_contact():
    x, y = _bilinear(toe=0.0003)
    yt = np.where(x < 0.0003, 0.0, y)
    at = AL.prepare_test(x, yt, RESP, CFG)
    assert at.ok and at.x0 == pytest.approx(0.0003, rel=1e-9)
    assert at.x[0] == 0.0 and at.y[0] == 0.0
    assert at.k0 == pytest.approx(1000.0, rel=1e-9)
    assert E.CURVE_NO_CONTACT in {i.code for i in AL.prepare_test(x, np.zeros_like(x), RESP, CFG).issues}
    no_toe = AL.prepare_test(x, yt, RESP, Config({"alignment.toe_correction": False}))
    ic = int(np.argmax(yt >= 0.02 * yt.max()))
    assert no_toe.x0 == pytest.approx(x[ic])


def test_step3_no_elastic_region():
    x = np.linspace(0, 1, 50)
    y = np.where(x < 0.5, 0.0, 1.0)  # 10–40% 구간 점 없음
    assert E.CURVE_NO_ELASTIC_REGION in {i.code for i in AL.prepare_test(x, y, RESP, CFG).issues}


def test_step4_failure_cut_and_windows():
    x, y = _bilinear(xmax=0.006)
    y = np.where(x > 0.005, y * 0.5, y)  # 피크 후 50% 급락
    at = AL.prepare_test(x, y, RESP, CFG)
    q = int(np.argmax(at.y[int(np.argmax(at.y)) + 1:] < 0.7 * at.F_peak)) + int(np.argmax(at.y)) + 1
    assert at.x_fail == at.x[q - 1]
    assert at.X_u == pytest.approx(at.x_fail) and at.X_u < 0.005 + 1e-12
    at2 = AL.prepare_test(x, y, RESP, CFG, user_window={"x_min": 0.0005, "x_max": 0.004})
    assert at2.X_l == 0.0005 and at2.X_u == 0.004


def test_step5_split_and_grid_counts():
    x, y = _bilinear()
    at = AL.prepare_test(x, y, RESP, CFG, has_plastic=True)
    N, n1min = 200, 30
    n1 = max(n1min, round(N * (at.x_e - at.X_l) / (at.X_u - at.X_l)))
    assert at.n_elastic == n1 and at.n_plastic == max(N - n1, 30)
    assert len(at.grid) == at.n_elastic + at.n_plastic
    assert at.grid[0] == at.X_l and at.grid[-1] == pytest.approx(at.X_u)
    assert at.grid[at.n_elastic - 1] == at.x_e and at.grid[at.n_elastic] > at.x_e
    # x_e: (1−dev_tol)·k0·x 아래로 처음 떨어지는 x (k0=1000, xy=0.002, k2=100)
    tol = CFG.get("alignment.elastic_dev_tol")
    xe_true = (1000 * 0.002 - 100 * 0.002) / ((1 - tol) * 1000 - 100)
    assert at.x_e >= xe_true and at.x_e - xe_true <= 2 * (x[1] - x[0])
    el = AL.prepare_test(x, y, RESP, CFG, has_plastic=False)
    assert el.n_plastic == 0 and el.x_e == el.X_u and el.n_elastic == 200


def test_step6_filter_and_no_extrapolation():
    x, y = _bilinear()
    at = AL.prepare_test(x, y, RESP, CFG)
    sim = T.CurveData(x[: len(x) // 2], y[: len(x) // 2], "displacement", "force")
    pair = AL.align_sim(at, sim, CFG)
    assert np.all(np.isnan(pair.f_sim[pair.x > pair.x_sim_max + 1e-15]))
    assert pair.shortfall == pytest.approx((at.X_u - pair.x_sim_max) / (at.X_u - at.X_l))
    ma = Config({"alignment.filter.kind": "moving_average"})
    xf, yf = AL.apply_filter(x, y, ma)
    assert len(xf) == 2000 and np.max(np.abs(yf - np.interp(xf, x, y))) < 0.05 * y.max()


def test_step7_output_interval():
    o = AL.output_intervals(1e-5, 0.0063, 1.0, CFG)
    assert o["mo_dtout"] == pytest.approx(1e-5 / (0.0063 * 4)) and o["mo_dtmax"] == o["mo_dtout"]
    assert o["mo_dtplt"] == pytest.approx(1.0 / 20)
    assert AL.output_intervals(1e-4, 1, 0.01, CFG, axis="time")["mo_dtout"] == pytest.approx(2.5e-5)


def test_o3_compliance_once_through_map_run():
    """δ_xhead = δ_spec + C·F → map_run(보정 금지) → 정렬 1단계(c) 1회 → δ_spec 복원 ≤ 1e-12·범위."""
    from matobserver.testspecs.bending import BendingTestSpec

    C = 2.5e-7
    d_spec = np.linspace(0, 0.006, 300)
    F = 1200.0 * np.tanh(d_spec / 0.002) * 1.3
    run = D.make_run("r", d_spec + C * F, F, width=0.01, thickness=0.004, span=0.064)
    td = BendingTestSpec({}).map_run(run, "displacement", C)
    assert np.array_equal(td.x, run.channels["displacement"])
    n = AL.normalize(td.x, td.y, CFG, compliance_m_per_n=td.compliance_m_per_n)
    err = np.max(np.abs(n.x - d_spec))
    assert err <= 1e-12 * (d_spec.max() - d_spec.min())
    double = AL.compliance_correct(n.x, n.y, C)  # 이중 적용이면 C·F 크기 오차
    assert np.max(np.abs(double - d_spec)) > 1e3 * 1e-12 * d_spec.max()


def test_compliance_nonmonotonic_warning():
    x, y = _bilinear()
    at = AL.prepare_test(x, y, RESP, CFG, compliance_m_per_n=2e-3)
    assert E.COMPLIANCE_NONMONOTONIC in {w.code for w in at.warnings}


def test_o13_public_signatures():
    s = inspect.signature(AL.align_pair)
    assert list(s.parameters)[:5] == ["exp", "sim", "response", "cfg", "compliance_m_per_n"]
    assert s.parameters["compliance_m_per_n"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(OBJ.curve_error).parameters) == ["pair", "region_weights", "cfg"]
    assert list(inspect.signature(roundtrip.check).parameters)[:6] == ["slot", "material", "adapter", "workdir",
                                                                        "strain_max", "tol"]
    assert list(inspect.signature(explicit_check.run).parameters)[:8] == [
        "case", "materials", "implicit_best", "exp", "adapter", "overrides", "workdir", "cfg"]
    x, y = _bilinear()
    c = T.CurveData(x, y, "displacement", "force")
    pair = AL.align_pair(c, c, RESP, CFG)
    for f in ("x", "f_exp", "f_sim", "region", "shortfall"):
        assert hasattr(pair, f)
    assert OBJ.curve_error(pair, {}, CFG) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(AL.CurveError):
        AL.align_pair(T.CurveData(x[:5], y[:5], "displacement", "force"), c, RESP, CFG)


def test_objective_formulas():
    x, y = _bilinear()
    at = AL.prepare_test(x, y, RESP, CFG)
    sim = T.CurveData(x, 1.1 * y, "displacement", "force")
    pair = AL.align_sim(at, sim, CFG)
    e = OBJ.region_errors(pair)
    assert e["elastic"] == pytest.approx(0.1, rel=1e-6) and e["plastic"] == pytest.approx(0.1, rel=1e-6)
    assert OBJ.curve_error(pair, {"plastic": 0.0}, CFG) == pytest.approx(0.1, rel=1e-6)
    r = OBJ.residual_vector(pair, {}, CFG)
    assert len(r) == len(pair.x) and np.dot(r, r) == pytest.approx(0.02, rel=1e-6)
    short = AL.align_sim(at, T.CurveData(x[:300], y[:300], "displacement", "force"), CFG)
    assert OBJ.curve_error(short, {}, CFG) == pytest.approx(short.shortfall, rel=1e-6)
    J, Jt = OBJ.combine_tests([(1.0, {"a": (1.0, 0.2), "b": (3.0, 0.1)}), (2.0, {"a": (1.0, 0.4)})])
    assert Jt == pytest.approx([0.125, 0.4]) and J == pytest.approx((0.125 + 0.8) / 3)
    assert OBJ.scalar_error(1.1, 1.0, CFG) == pytest.approx(0.1)
