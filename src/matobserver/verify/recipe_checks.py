"""레시피 dry-run 검사 판정 함수 (recipe.md §7.2, module-boundaries.md §4.6).

각 함수는 VerificationReport `checks[]` 원소(dict)를 돌려준다:
`{"code", "result": pass|warn|fail|skipped, "detail"?, "value"?, "threshold"?}`.
임계값은 모두 설정 `recipe_verify.*`·`solver.explicit.max_ke_ratio`·`alignment.*`에서 온다.
`check_reference_curve`는 엔진 공개 순수 함수(`engine.alignment.align_pair`, `engine.objective.curve_error`)만 쓴다(B-7).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from ..core import errors as E
from ..core.config import Config
from ..core.types import CurveData, Extracted, ResponseDef, ResponseWindow, RunState, RunStatus

Check = dict[str, Any]


def _check(code: str, result: str, detail: str | None = None, **extra: Any) -> Check:
    out: Check = {"code": code, "result": result}
    if detail:
        out["detail"] = detail
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# ======================================================================================
# 곡선 도우미
# ======================================================================================
def loading_segment(x: np.ndarray, y: np.ndarray, unload_tol_rel: float) -> tuple[np.ndarray, np.ndarray]:
    """정렬 1단계 (d)와 같은 규칙: 누적 최대 x를 갱신하는 점만, 제하(x < max − tol·범위)가 시작되면 자른다."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) == 0:
        return x, y
    span = float(np.max(x) - np.min(x)) or 1.0
    keep_x, keep_y = [x[0]], [y[0]]
    cmax = x[0]
    for xi, yi in zip(x[1:], y[1:]):
        if xi < cmax - unload_tol_rel * span:
            break
        if xi > cmax:
            keep_x.append(xi)
            keep_y.append(yi)
            cmax = xi
    return np.array(keep_x), np.array(keep_y)


def initial_slope(x: np.ndarray, y: np.ndarray, lo: float, hi: float, min_points: int = 2) -> float | None:
    """y ∈ [lo, hi]·F_peak 이고 피크 이전인 점의 최소제곱 기울기(정렬 3단계 토우 맞춤과 같은 구간)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(y) < 2:
        return None
    p = int(np.argmax(y))
    fpk = float(y[p])
    if fpk <= 0:
        return None
    m = (y[: p + 1] >= lo * fpk) & (y[: p + 1] <= hi * fpk)
    if int(m.sum()) < max(min_points, 2):
        return None
    k, _ = np.polyfit(x[: p + 1][m], y[: p + 1][m], 1)
    return float(k)


def read_reference_csv(path: Path) -> CurveData:
    """기대 F-δ CSV (x[m], y[N]). 머리 줄·`#` 주석 허용."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.replace(";", ",").split(",")]
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            continue  # 머리 줄
    a = np.array(rows, dtype=float).reshape(-1, 2)
    return CurveData(x=a[:, 0], y=a[:, 1], x_quantity="displacement", y_quantity="force")


# ======================================================================================
# 판정 함수
# ======================================================================================
def check_termination(status: RunStatus) -> list[Check]:
    if status.state is RunState.NORMAL:
        out = [_check(E.VERIFY_TERMINATION, "pass", f"{status.termination_message or 'Normal termination'} ({status.mode_used})")]
        if status.fallback_used:
            out.append(_check(E.VERIFY_FALLBACK_USED, "warn", "primary implicit failed, explicit fallback succeeded"))
        return out
    return [_check(E.VERIFY_TERMINATION, "fail", f"{status.state.value}: {status.termination_message} ({status.mode_used})")]


def check_response(extracted: Extracted, responses: Sequence[ResponseDef], cfg: Config) -> Check:
    min_points = int(cfg.get("recipe_verify.min_points"))
    tol = float(cfg.get("alignment.unload_tol_rel"))
    problems = []
    for r in responses:
        if r.kind != "curve":
            if r.response_key not in extracted.scalars:
                problems.append(f"{r.response_key}: scalar missing")
            continue
        c = extracted.curves.get(r.response_key)
        if c is None or len(c.x) < min_points:
            problems.append(f"{r.response_key}: {0 if c is None else len(c.x)} points < {min_points}")
            continue
        lx, ly = loading_segment(c.x, c.y, tol)
        if len(lx) < min_points:
            problems.append(f"{r.response_key}: monotonic loading segment has {len(lx)} points < {min_points}")
        elif float(np.max(ly)) <= 0:
            problems.append(f"{r.response_key}: F_peak <= 0")
    if problems:
        return _check(E.VERIFY_RESPONSE, "fail", "; ".join(problems), threshold=min_points)
    return _check(E.VERIFY_RESPONSE, "pass", f"{len(responses)} response(s) extracted", threshold=min_points)


def beam_stiffness(E_pa: float, width_m: float, thickness_m: float, span_m: float) -> float:
    """3점굽힘 보 이론 강성 k_th = 48EI/L³ = 4·E·b·h³/L³ [N/m]."""
    return 4.0 * E_pa * width_m * thickness_m ** 3 / span_m ** 3


def check_units(curve: CurveData | None, k_th: float | None, kind: str, cfg: Config) -> Check:
    if curve is None or k_th is None or k_th <= 0:
        return _check(E.VERIFY_UNITS, "skipped", "no curve or beam-theory stiffness")
    lx, ly = loading_segment(curve.x, curve.y, float(cfg.get("alignment.unload_tol_rel")))
    k_sim = initial_slope(lx, ly, float(cfg.get("alignment.toe_fit_lo")), float(cfg.get("alignment.toe_fit_hi")),
                         int(cfg.get("alignment.toe_fit_min_points")))
    if k_sim is None or k_sim <= 0:
        return _check(E.VERIFY_UNITS, "fail", "initial slope could not be fitted")
    ratio = k_sim / k_th
    if kind == "parametric":
        tol = float(cfg.get("recipe_verify.unit_ratio_tol"))
        lo, hi = 1.0 / tol, tol
    else:
        lo, hi = 0.1, 10.0
    result = "pass" if lo <= ratio <= hi else "fail"
    return _check(E.VERIFY_UNITS, result, f"k_sim/k_th in [{lo:g}, {hi:g}]", value=float(ratio), threshold=float(hi))


def check_penetration(curve: CurveData | None, penetration_m: float | None, cfg: Config) -> Check:
    max_pen = float(cfg.get("recipe_verify.max_initial_penetration_m"))
    max_ratio = float(cfg.get("recipe_verify.max_initial_force_ratio"))
    problems = []
    if penetration_m is not None and penetration_m > max_pen:
        problems.append(f"initial penetration {penetration_m:.3e} m > {max_pen:.3e} m")
    ratio = None
    if curve is not None and len(curve.y) >= 2:
        fpk = float(np.max(curve.y))
        if fpk > 0:
            ratio = abs(float(curve.y[1])) / fpk
            if ratio > max_ratio:
                problems.append(f"first output contact force ratio {ratio:.3g} > {max_ratio:g}")
    detail = "; ".join(problems) or (
        f"max initial penetration {penetration_m:.3e} m" if penetration_m is not None else "no initial penetration reported")
    return _check(E.VERIFY_PENETRATION, "fail" if problems else "pass", detail,
                  value=penetration_m, threshold=max_pen)


def curve_quantities(curve: CurveData, cfg: Config) -> dict[str, float | None]:
    lx, ly = loading_segment(curve.x, curve.y, float(cfg.get("alignment.unload_tol_rel")))
    k = initial_slope(lx, ly, float(cfg.get("alignment.toe_fit_lo")), float(cfg.get("alignment.toe_fit_hi")),
                      int(cfg.get("alignment.toe_fit_min_points")))
    return {"initial_slope": k, "x_max": float(lx[-1]) if len(lx) else None, "_x": lx, "_y": ly}  # type: ignore[dict-item]


def check_mesh_convergence(curves: Sequence[tuple[float, CurveData]], quantities: Sequence[str], cfg: Config) -> Check:
    """size_factor 순서대로 인접 크기 간 `initial_slope`·`force_at_end` 상대 변화 ≤ mesh_convergence_tol.
    force_at_end 는 모든 곡선의 공통 x 상한에서의 힘(선형 보간)."""
    tol = float(cfg.get("recipe_verify.mesh_convergence_tol"))
    if len(curves) < 2:
        return _check(E.VERIFY_MESH_CONVERGENCE, "skipped", "fewer than 2 mesh sizes")
    q = [curve_quantities(c, cfg) for _, c in curves]
    if any(v["x_max"] is None for v in q):
        return _check(E.VERIFY_MESH_CONVERGENCE, "fail", "a mesh-size run has no loading curve")
    x_end = min(float(v["x_max"]) for v in q)  # type: ignore[arg-type]
    vals: dict[str, list[float | None]] = {
        "initial_slope": [v["initial_slope"] for v in q],
        "force_at_end": [float(np.interp(x_end, v["_x"], v["_y"])) for v in q],  # type: ignore[arg-type]
    }
    worst = 0.0
    problems = []
    for name in quantities:
        series = vals.get(name)
        if series is None:
            problems.append(f"unknown quantity {name!r}")
            continue
        for (f0, _), (f1, _), a, b in zip(curves[:-1], curves[1:], series[:-1], series[1:]):
            if a is None or b is None or b == 0:
                problems.append(f"{name}: not computable for size factors {f0:g}/{f1:g}")
                continue
            rel = abs(a - b) / abs(b)
            worst = max(worst, rel)
            if rel > tol:
                problems.append(f"{name}: {rel:.3g} between size factors {f0:g} and {f1:g}")
    return _check(E.VERIFY_MESH_CONVERGENCE, "fail" if problems else "pass", "; ".join(problems) or None,
                  value=float(worst), threshold=tol)


def _engine_funcs() -> tuple[Callable[..., Any], Callable[..., float]]:
    from ..engine.alignment import align_pair  # type: ignore[import-not-found]
    from ..engine.objective import curve_error  # type: ignore[import-not-found]

    return align_pair, curve_error


def check_reference_curve(curve: CurveData, reference_csv: Path, response: ResponseDef, cfg: Config) -> Mapping[str, Any]:
    """기대 곡선 검사 (module-boundaries.md §4.6). 기대 곡선을 시험 곡선 자리(exp)에, dry-run 곡선을 sim 자리에 두고
    정렬 8단계(`align_pair`) 후 Ω 전체(`window.split=none`) J = `curve_error` ≤ `recipe_verify.reference_curve_tol`."""
    tol = float(cfg.get("recipe_verify.reference_curve_tol"))
    try:
        align_pair, curve_error = _engine_funcs()
    except ImportError as e:
        return _check(E.VERIFY_REFERENCE_CURVE, "skipped", f"engine alignment/objective not available: {e}", threshold=tol)
    if not Path(reference_csv).is_file():
        return _check(E.VERIFY_REFERENCE_CURVE, "fail", f"reference curve file not found: {Path(reference_csv).name}",
                      threshold=tol)
    exp = read_reference_csv(reference_csv)
    exp = CurveData(x=exp.x, y=exp.y, x_quantity=curve.x_quantity, y_quantity=curve.y_quantity)
    w = response.window
    whole = dataclasses.replace(response, window=ResponseWindow(x_min=w.x_min, x_max=w.x_max, split="none",
                                                                region_weights=w.region_weights))
    try:
        pair = align_pair(exp, curve, whole, cfg)
        weights = w.region_weights or cfg.get("objective.region_weights")
        J = float(curve_error(pair, weights, cfg))
    except (ValueError, ArithmeticError) as e:  # engine CurveError 는 ValueError
        return _check(E.VERIFY_REFERENCE_CURVE, "fail", f"alignment failed: {e}", threshold=tol)
    return _check(E.VERIFY_REFERENCE_CURVE, "pass" if J <= tol else "fail", None, value=J, threshold=tol)


def warn_check(code: str, findings: Sequence[str]) -> Check:
    return _check(code, "warn", "; ".join(findings)) if findings else _check(code, "pass")


def check_ke_ratio(extracted: Extracted, mode_used: str, cfg: Config) -> Check:
    limit = float(cfg.get("solver.explicit.max_ke_ratio"))
    if mode_used != "explicit":
        return _check(E.WARN_KE_RATIO, "skipped", "implicit run", threshold=limit)
    v = extracted.energy.get("ke_ie_ratio_max")
    if v is None:
        return _check(E.WARN_KE_RATIO, "skipped", "no glstat energy", threshold=limit)
    return _check(E.WARN_KE_RATIO, "warn" if v > limit else "pass", None, value=float(v), threshold=limit)


def check_no_fallback(manifest: Mapping[str, Any]) -> Check:
    a = (manifest.get("solver") or {}).get("analysis") or {}
    if a.get("primary") == "implicit" and a.get("fallback", "none") == "none":
        return _check(E.WARN_NO_FALLBACK, "warn", "primary=implicit with fallback=none")
    return _check(E.WARN_NO_FALLBACK, "pass")


def check_compliance(manifest: Mapping[str, Any]) -> Check:
    c = manifest.get("compliance")
    if c is None or c.get("value_m_per_n") is None:
        return _check(E.WARN_COMPLIANCE_MISSING, "warn", "compliance is null (E reliability low)")
    return _check(E.WARN_COMPLIANCE_MISSING, "pass")


def report_status(checks: Sequence[Mapping[str, Any]], strict: bool) -> str:
    results = {c["result"] for c in checks}
    if "fail" in results:
        return "failed"
    if "warn" in results:
        return "failed" if strict else "passed_with_warnings"
    return "passed"
