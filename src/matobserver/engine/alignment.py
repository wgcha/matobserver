"""정렬·재샘플링 8단계 (testspec.md §5) — I/O·레지스트리 의존 없는 순수 모듈(module-boundaries.md §4.6, B-7).

공개 함수:

* `align_pair(exp, sim, response, cfg, *, compliance_m_per_n=None)` → `AlignedPair` (계약 §4.6 시그니처, 선택 키워드 추가)
* `prepare_test(x, y, response, cfg, ...)` → `AlignedTest` (시험측 1–6단계, 잡 시작 시 1회)
* `align_sim(test, sim, cfg)` → `AlignedPair` (해석측 1·3·6단계 + 8단계, 매 평가)
* `output_intervals(...)` (7단계), `compliance_correct(...)` (1단계 c, 시험측에서만 1회)

수치는 SI. `cfg`는 `Config`(점 표기 `get`) 또는 점 표기 키 매핑. 없는 키는 testspec.md 기본값.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from matobserver.core import errors as E
from matobserver.core.types import CurveData, Issue, ResponseDef, ResponseWindow

DEFAULTS: dict[str, Any] = {
    "alignment.unload_tol_rel": 0.005,
    "alignment.dedup_tol_rel": 1e-9,
    "alignment.min_raw_points": 20,
    "alignment.min_loading_fraction": 0.3,
    "alignment.contact_force_ratio": 0.02,
    "alignment.toe_correction": True,
    "alignment.toe_fit_lo": 0.10,
    "alignment.toe_fit_hi": 0.40,
    "alignment.toe_fit_min_points": 5,
    "alignment.failure_cut": True,
    "alignment.failure_drop_ratio": 0.3,
    "alignment.grid_points": 200,
    "alignment.elastic_min_points": 30,
    "alignment.plastic_min_points": 30,
    "alignment.filter.kind": "none",
    "alignment.filter.presample_points": 2000,
    "alignment.filter.window": 11,
    "alignment.filter.cfc_class": 60,
    "alignment.output_oversample": 4,
    "alignment.elastic_dev_tol": 0.015,  # 1.1
    "alignment.elastic_min_raw_points": 5,
    "objective.shortfall_penalty": 1.0,
    "solver.d3plot_frames": 20,
}


def cget(cfg: Any, key: str) -> Any:
    """설정 값 읽기 (Config.get 또는 매핑; 없으면 DEFAULTS)."""
    if cfg is None:
        return DEFAULTS[key]
    try:
        if hasattr(cfg, "get") and not isinstance(cfg, Mapping):
            return cfg.get(key)
        if isinstance(cfg, Mapping):
            if key in cfg:
                return cfg[key]
            return DEFAULTS[key]
    except KeyError:
        pass
    return DEFAULTS[key]


class CurveError(ValueError):
    """시험측 곡선 처리 실패 (CURVE_* Issue 목록 포함)."""

    def __init__(self, issues: Sequence[Issue]) -> None:
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in issues))
        self.issues = tuple(issues)


def _issue(code: str, msg: str, severity: str = "error", **ctx: Any) -> Issue:
    return Issue(code=code, message=msg, severity=severity, context=ctx)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# 1단계 정규화
# --------------------------------------------------------------------------------------
def compliance_correct(x: np.ndarray, y: np.ndarray, compliance_m_per_n: float | None) -> np.ndarray:
    """δ_spec = δ_crosshead − C·F (testspec.md §7). 엔진 정렬 1단계(c)에서만 호출한다."""
    x = np.asarray(x, dtype=float)
    if compliance_m_per_n is None or compliance_m_per_n == 0:
        return x.copy()
    return x - float(compliance_m_per_n) * np.asarray(y, dtype=float)


def _loading_cut(x: np.ndarray, tol_rel: float) -> int:
    """제하 시작 인덱스(그 앞까지가 단조 하중 구간). 없으면 len(x)."""
    if len(x) == 0:
        return 0
    rng = float(np.max(x) - np.min(x)) or 1.0
    cm = np.maximum.accumulate(x)
    drop = np.nonzero(x[1:] < cm[:-1] - tol_rel * rng)[0]
    return int(drop[0] + 1) if drop.size else len(x)


@dataclass(frozen=True, eq=False)
class Normalized:
    x: np.ndarray
    y: np.ndarray
    x_full_range: float
    loading_fraction: float
    issues: tuple[Issue, ...] = ()


def normalize(x: np.ndarray, y: np.ndarray, cfg: Any, *, compliance_m_per_n: float | None = None,
              is_test: bool = True) -> Normalized:
    """1단계 (b)–(e). 시험측(is_test)만 (c) 컴플라이언스 보정."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    issues: list[Issue] = []
    tol_u = float(cget(cfg, "alignment.unload_tol_rel"))
    if is_test and compliance_m_per_n:
        raw_cut = _loading_cut(x, tol_u)
        xc = compliance_correct(x, y, compliance_m_per_n)
        cut_c = _loading_cut(xc, tol_u)
        if cut_c < raw_cut:
            issues.append(_issue(E.COMPLIANCE_NONMONOTONIC, "컴플라이언스 보정 후 하중 구간 변위가 단조가 아님(C 과대 의심)",
                                 "warning"))
        x = xc
    if len(x) == 0:
        return Normalized(x, y, 0.0, 0.0, tuple(issues))
    full = float(np.max(x) - np.min(x))
    cut = _loading_cut(x, tol_u)
    xl, yl = x[:cut], y[:cut]
    # 누적 최대를 갱신하는 점만
    if len(xl):
        prev = np.concatenate([[-np.inf], np.maximum.accumulate(xl)[:-1]])
        keep = xl > prev
        xl, yl = xl[keep], yl[keep]
    # 중복 x 평균
    tol_d = float(cget(cfg, "alignment.dedup_tol_rel")) * (full or 1.0)
    if len(xl) > 1:
        xs, ys = [xl[0]], [[yl[0]]]
        for xi, yi in zip(xl[1:], yl[1:]):
            if abs(xi - xs[-1]) <= tol_d:
                ys[-1].append(yi)
            else:
                xs.append(xi)
                ys.append([yi])
        xl = np.asarray(xs, dtype=float)
        yl = np.asarray([float(np.mean(v)) for v in ys])
    frac = (float(xl[-1] - xl[0]) / full) if (len(xl) > 1 and full > 0) else 0.0
    return Normalized(xl, yl, full, frac, tuple(issues))


# --------------------------------------------------------------------------------------
# 3단계 원점 정렬
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class OriginAligned:
    x: np.ndarray  # 원점 이동 후, (0,0) 앞에 추가
    y: np.ndarray
    x0: float
    k0: float | None  # 맞춤 기울기 (토우 보정 없으면 같은 구간 맞춤)
    F_peak: float
    i_peak: int  # 이동 후 배열 인덱스
    fit_points: int
    issues: tuple[Issue, ...] = ()


def _fit_line(x: np.ndarray, y: np.ndarray, F_peak: float, i_peak: int, lo: float, hi: float) -> tuple[float, float, int]:
    idx = np.arange(len(y))
    m = (y >= lo * F_peak) & (y <= hi * F_peak) & (idx <= i_peak)
    npts = int(np.sum(m))
    if npts < 2:
        return math.nan, math.nan, npts
    k, b = np.polyfit(x[m], y[m], 1)
    return float(k), float(b), npts


def origin_align(x: np.ndarray, y: np.ndarray, cfg: Any, *, quasi_static: bool = True, strict: bool = True) -> OriginAligned:
    """3단계. strict=False(해석측)면 맞춤 실패 시 접촉점 기준으로 대체하고 오류를 내지 않는다."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    issues: list[Issue] = []
    if len(x) == 0:
        if strict:
            issues.append(_issue(E.CURVE_NO_CONTACT, "곡선이 비어 있음"))
        return OriginAligned(x, y, 0.0, None, 0.0, 0, 0, tuple(issues))
    p = int(np.argmax(y))
    Fp = float(y[p])
    ratio = float(cget(cfg, "alignment.contact_force_ratio"))
    above = np.nonzero(y >= ratio * Fp)[0] if Fp > 0 else np.array([], dtype=int)
    if Fp <= 0 or above.size == 0:
        if strict:
            issues.append(_issue(E.CURVE_NO_CONTACT, "접촉 시작 판정 실패(하중이 0 이하)"))
        return OriginAligned(x, y, 0.0, None, max(Fp, 0.0), p, 0, tuple(issues))
    ic = int(above[0])
    lo = float(cget(cfg, "alignment.toe_fit_lo"))
    hi = float(cget(cfg, "alignment.toe_fit_hi"))
    k, b, npts = _fit_line(x, y, Fp, p, lo, hi)
    min_pts = int(cget(cfg, "alignment.toe_fit_min_points"))
    fit_ok = npts >= min_pts and math.isfinite(k) and k > 0
    if not fit_ok and strict:
        issues.append(_issue(E.CURVE_NO_ELASTIC_REGION, f"토우 맞춤 점 {npts} < {min_pts} 또는 기울기 비정상"))
    if quasi_static and bool(cget(cfg, "alignment.toe_correction")) and fit_ok:
        x0 = -b / k
    else:
        x0 = float(x[ic])
    xs = x - x0
    keep = xs > 0
    xs, ys = xs[keep], y[keep]
    xs = np.concatenate([[0.0], xs])
    ys = np.concatenate([[0.0], ys])
    ip = int(np.argmax(ys))
    return OriginAligned(xs, ys, float(x0), (k if fit_ok else None), Fp, ip, npts, tuple(issues))


# --------------------------------------------------------------------------------------
# 6단계 필터
# --------------------------------------------------------------------------------------
def _cfc(t: np.ndarray, y: np.ndarray, cfc_class: float) -> np.ndarray:
    """SAE J211 CFC 2극 버터워스, 정방향+역방향(위상 0). 균일 시간 간격 가정."""
    if len(t) < 4:
        return y.copy()
    T = float(np.mean(np.diff(t)))
    wd = 2 * math.pi * cfc_class * 2.0775
    wa = math.sin(wd * T / 2) / math.cos(wd * T / 2)
    a0 = wa**2 / (1 + math.sqrt(2) * wa + wa**2)
    a1, a2 = 2 * a0, a0
    b1 = -2 * (wa**2 - 1) / (1 + math.sqrt(2) * wa + wa**2)
    b2 = (-1 + math.sqrt(2) * wa - wa**2) / (1 + math.sqrt(2) * wa + wa**2)

    def run(v: np.ndarray) -> np.ndarray:
        o = np.zeros_like(v)
        o[:2] = v[:2]
        for i in range(2, len(v)):
            o[i] = a0 * v[i] + a1 * v[i - 1] + a2 * v[i - 2] + b1 * o[i - 1] + b2 * o[i - 2]
        return o

    return run(run(y)[::-1])[::-1]


def apply_filter(x: np.ndarray, y: np.ndarray, cfg: Any) -> tuple[np.ndarray, np.ndarray]:
    kind = str(cget(cfg, "alignment.filter.kind"))
    if kind == "none" or len(x) < 3:
        return x, y
    n = int(cget(cfg, "alignment.filter.presample_points"))
    xu = np.linspace(float(x[0]), float(x[-1]), n)
    yu = np.interp(xu, x, y)
    if kind == "moving_average":
        w = max(1, int(cget(cfg, "alignment.filter.window")))
        if w > 1:
            pad = w // 2
            yp = np.concatenate([np.full(pad, yu[0]), yu, np.full(pad, yu[-1])])
            yu = np.convolve(yp, np.ones(w) / w, mode="valid")[: len(xu)]
        return xu, yu
    if kind == "cfc":
        return xu, _cfc(xu, yu, float(cget(cfg, "alignment.filter.cfc_class")))
    raise ValueError(f"unknown alignment.filter.kind {kind!r}")


# --------------------------------------------------------------------------------------
# 시험측 1–6단계
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class AlignedTest:
    response_key: str
    axis: str | None
    x: np.ndarray  # 3단계 후 시험 곡선
    y: np.ndarray
    x0: float
    k0: float | None
    F_peak: float
    X_l: float
    X_u: float
    x_fail: float
    x_e: float
    grid: np.ndarray
    region: np.ndarray  # "elastic" | "plastic"
    f_exp: np.ndarray
    n_elastic: int
    n_plastic: int
    elastic_raw_points: int
    issues: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()

    @property
    def grid_dx_min(self) -> float:
        if len(self.grid) < 2:
            return float("inf")
        d = np.diff(self.grid)
        d = d[d > 0]
        return float(np.min(d)) if d.size else float("inf")

    @property
    def ok(self) -> bool:
        return not self.issues and len(self.grid) > 0


def _window_vals(w: ResponseWindow | Mapping[str, Any] | None) -> tuple[float | None, float | None, str, Any]:
    if w is None:
        return None, None, "auto", None
    if isinstance(w, Mapping):
        return w.get("x_min"), w.get("x_max"), w.get("split", "auto"), w.get("region_weights")
    return w.x_min, w.x_max, w.split, w.region_weights


def prepare_test(x: np.ndarray, y: np.ndarray, response: ResponseDef | None, cfg: Any, *,
                 compliance_m_per_n: float | None = None, user_window: Mapping[str, Any] | None = None,
                 has_plastic: bool = True, quasi_static: bool = True) -> AlignedTest:
    """시험측 1–6단계. 오류 Issue가 있으면 `ok=False`(격자 비어 있을 수 있음)."""
    issues: list[Issue] = []
    warns: list[Issue] = []
    norm = normalize(x, y, cfg, compliance_m_per_n=compliance_m_per_n, is_test=True)
    warns.extend(norm.issues)
    min_raw = int(cget(cfg, "alignment.min_raw_points"))
    if len(norm.x) < min_raw:
        issues.append(_issue(E.CURVE_TOO_FEW_POINTS, f"정규화 후 점 {len(norm.x)} < {min_raw}"))
    if norm.loading_fraction < float(cget(cfg, "alignment.min_loading_fraction")):
        issues.append(_issue(E.CURVE_NOT_MONOTONIC,
                             f"단조 하중 구간 비율 {norm.loading_fraction:.3g} < {cget(cfg, 'alignment.min_loading_fraction')}"))
    oa = origin_align(norm.x, norm.y, cfg, quasi_static=quasi_static, strict=True)
    issues.extend(oa.issues)
    rkey = response.response_key if response is not None else "curve"
    axis = response.axis if response is not None else None
    empty = np.array([])
    if issues:
        return AlignedTest(rkey, axis, oa.x, oa.y, oa.x0, oa.k0, oa.F_peak, 0.0, 0.0, 0.0, 0.0, empty,
                           np.array([], dtype=object), empty, 0, 0, 0, tuple(issues), tuple(warns))
    xs, ys = oa.x, oa.y
    Fp = float(ys[oa.i_peak])
    # 4단계 비교 구간
    x_fail = float(xs[-1])
    if bool(cget(cfg, "alignment.failure_cut")):
        thr = (1.0 - float(cget(cfg, "alignment.failure_drop_ratio"))) * Fp
        after = np.nonzero(ys[oa.i_peak + 1:] < thr)[0]
        if after.size:
            q = oa.i_peak + 1 + int(after[0])
            x_fail = float(xs[q - 1])
    wxmin, wxmax, split, _rw = _window_vals(response.window if response is not None else None)
    uw = user_window or {}
    lows = [0.0] + [float(v) for v in (uw.get("x_min"), wxmin) if v is not None]
    highs = [float(xs[-1]), x_fail] + [float(v) for v in (uw.get("x_max"), wxmax) if v is not None]
    X_l, X_u = max(lows), min(highs)
    if not X_u > X_l:
        issues.append(_issue(E.CURVE_NOT_MONOTONIC, f"비교 구간이 비어 있음 [{X_l}, {X_u}]"))
        return AlignedTest(rkey, axis, xs, ys, oa.x0, oa.k0, Fp, X_l, X_u, x_fail, X_u, empty,
                           np.array([], dtype=object), empty, 0, 0, 0, tuple(issues), tuple(warns))
    # 5.1 구간 분할
    k0 = oa.k0
    if split == "none" or not has_plastic or k0 is None:
        x_e = X_u
    else:
        hi = float(cget(cfg, "alignment.toe_fit_hi"))
        dev = float(cget(cfg, "alignment.elastic_dev_tol"))
        reach = np.nonzero(ys >= hi * Fp)[0]
        x_hi = float(xs[reach[0]]) if reach.size else float(xs[-1])
        cand = np.nonzero((xs > x_hi) & (ys < (1.0 - dev) * k0 * xs))[0]
        x_e = float(xs[cand[0]]) if cand.size else X_u
    x_e = min(max(x_e, X_l), X_u)
    n_el_raw = int(np.sum((xs >= X_l) & (xs <= x_e)))
    if n_el_raw < int(cget(cfg, "alignment.elastic_min_raw_points")):
        issues.append(_issue(E.CURVE_NO_ELASTIC_REGION, f"탄성 구간 원본 점 {n_el_raw} < "
                                                         f"{cget(cfg, 'alignment.elastic_min_raw_points')}"))
    # 5단계 격자
    N = int(cget(cfg, "alignment.grid_points"))
    n1 = max(int(cget(cfg, "alignment.elastic_min_points")), int(round(N * (x_e - X_l) / (X_u - X_l))))
    plastic_empty = x_e >= X_u
    n2 = 0 if plastic_empty else max(N - n1, int(cget(cfg, "alignment.plastic_min_points")))
    g1 = np.linspace(X_l, x_e, n1)
    g2 = np.linspace(x_e, X_u, n2 + 1)[1:] if n2 > 0 else np.array([])
    grid = np.concatenate([g1, g2])
    region = np.array(["elastic"] * len(g1) + ["plastic"] * len(g2), dtype=object)
    # 6단계 필터 + 보간 (시험 곡선은 격자 전체를 덮는다: X_u ≤ x_exp_max)
    fx, fy = apply_filter(xs, ys, cfg)
    f_exp = np.interp(grid, fx, fy)
    return AlignedTest(rkey, axis, xs, ys, oa.x0, k0, Fp, X_l, X_u, x_fail, x_e, grid, region, f_exp,
                       len(g1), len(g2), n_el_raw, tuple(issues), tuple(warns))


# --------------------------------------------------------------------------------------
# 해석측 + 8단계
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class AlignedPair:
    x: np.ndarray  # 격자
    f_exp: np.ndarray
    f_sim: np.ndarray  # 해석 범위 밖 격자점은 NaN (비교 제외)
    region: np.ndarray  # "elastic" | "plastic"
    shortfall: float
    in_range: np.ndarray = field(default_factory=lambda: np.array([], dtype=bool))
    X_l: float = 0.0
    X_u: float = 0.0
    x_e: float = 0.0
    x_sim_max: float = 0.0
    k0_exp: float | None = None
    k0_sim: float | None = None
    sim_x: np.ndarray = field(default_factory=lambda: np.array([]))  # 정렬된 해석 곡선 원본(3단계 후)
    sim_y: np.ndarray = field(default_factory=lambda: np.array([]))


def align_sim(test: AlignedTest, sim: CurveData | None, cfg: Any, *, quasi_static: bool = True) -> AlignedPair:
    grid = test.grid
    nanv = np.full(len(grid), np.nan)
    if sim is None or len(np.asarray(sim.x)) < 2:
        return AlignedPair(grid, test.f_exp, nanv, test.region, 1.0, np.zeros(len(grid), dtype=bool),
                           test.X_l, test.X_u, test.x_e, 0.0, test.k0, None)
    norm = normalize(sim.x, sim.y, cfg, is_test=False)
    oa = origin_align(norm.x, norm.y, cfg, quasi_static=quasi_static, strict=False)
    if len(oa.x) < 2:
        return AlignedPair(grid, test.f_exp, nanv, test.region, 1.0, np.zeros(len(grid), dtype=bool),
                           test.X_l, test.X_u, test.x_e, 0.0, test.k0, None)
    fx, fy = apply_filter(oa.x, oa.y, cfg)
    x_sim_max = float(oa.x[-1])
    inr = (grid >= float(fx[0])) & (grid <= float(fx[-1]))
    f_sim = np.where(inr, np.interp(grid, fx, fy), np.nan)
    span = test.X_u - test.X_l
    s = max(0.0, (test.X_u - x_sim_max) / span) if span > 0 else 0.0
    return AlignedPair(grid, test.f_exp, f_sim, test.region, float(min(s, 1.0)), inr, test.X_l, test.X_u, test.x_e,
                       x_sim_max, test.k0, oa.k0, oa.x, oa.y)


def align_pair(exp: CurveData, sim: CurveData, response: ResponseDef, cfg: Any, *,
               compliance_m_per_n: float | None = None, user_window: Mapping[str, Any] | None = None,
               has_plastic: bool = True) -> AlignedPair:
    """module-boundaries.md §4.6 공개 함수: 시험 곡선 exp와 해석 곡선 sim을 8단계 규칙으로 정렬한다.

    컴플라이언스는 시험측 1단계(c)에서만 1회 적용한다. 시험 곡선 처리 실패면 `CurveError`.
    """
    test = prepare_test(exp.x, exp.y, response, cfg, compliance_m_per_n=compliance_m_per_n,
                        user_window=user_window, has_plastic=has_plastic)
    if not test.ok:
        raise CurveError(test.issues)
    return align_sim(test, sim, cfg)


# --------------------------------------------------------------------------------------
# 7단계 출력 간격
# --------------------------------------------------------------------------------------
def output_intervals(dx_min: float, mo_dend: float, mo_tend: float, cfg: Any, *, axis: str = "displacement") -> dict[str, float]:
    """mo_dtout, mo_dtplt, mo_dtmax (SI s). 변위 기준축: dt_out = Δx_min / (v·k), v = mo_dend/mo_tend."""
    k = float(cget(cfg, "alignment.output_oversample"))
    if axis == "time":
        dt_out = dx_min / k
    else:
        v = mo_dend / mo_tend
        dt_out = dx_min / (v * k)
    dt_out = min(dt_out, mo_tend)
    frames = max(1, int(cget(cfg, "solver.d3plot_frames")))
    return {"mo_dtout": float(dt_out), "mo_dtplt": float(mo_tend / frames), "mo_dtmax": float(dt_out)}
