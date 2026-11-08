"""explicit 재현 검사 (testspec.md §8.1, result.md §4.12, module-boundaries.md §4.6) — 경고 전용, θ 불변.

`run(case, materials, implicit_best, exp, adapter, overrides, workdir, cfg)` → result.md §4.12 `per_case` 원소.
내부에서 `adapter.prepare(..., mode="explicit", overrides=...)` → `run` → `extract`, 지표는 `align_pair` 계열·`curve_error`.

지표 해석(계약 모호점, 가장 가까운 해석):
* 격자·정렬은 시험 곡선 기준(§5). `vs_implicit`: 기준 = implicit 최적점 해석 곡선을 같은 격자에 정렬한 값,
  `vs_experiment`: 기준 = 시험 곡선. e_k = ‖F_chk − F_ref‖/‖F_ref‖ (Ω_k), ΔF_peak = (F_peak,chk − F_peak,ref)/F_peak,ref.
* 케이스에 run이 여럿이면 run별 지표의 산술평균.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from matobserver.core.errors import IncompatibleOverride
from matobserver.core.types import (
    CurveData,
    Extracted,
    NeutralMaterial,
    RunOverrides,
    RunState,
    SimulationCase,
    SolverAdapter,
    TestData,
)

from . import alignment as AL
from .objective import REGIONS


class _Never:
    def is_set(self) -> bool:
        return False


def _cmp(ref: np.ndarray, chk: np.ndarray, region: np.ndarray) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for k in REGIONS:
        m = (region == k) & np.isfinite(ref) & np.isfinite(chk)
        if not np.any(region == k):
            out[k] = 0.0
            continue
        if not np.any(m):
            out[k] = None
            continue
        den = math.sqrt(float(np.sum(ref[m] ** 2)))
        out[k] = math.sqrt(float(np.sum((chk[m] - ref[m]) ** 2))) / den if den > 0 else 0.0
    mr = np.isfinite(ref)
    mc = np.isfinite(chk)
    if np.any(mr) and np.any(mc):
        pr, pc = float(np.max(ref[mr])), float(np.max(chk[mc]))
        out["peak_force_rel_diff"] = (pc - pr) / pr if pr != 0 else None
    else:
        out["peak_force_rel_diff"] = None
    return out


def _mean(vals: Sequence[dict[str, Any]], key: str) -> float | None:
    xs = [v[key] for v in vals if v.get(key) is not None and math.isfinite(v[key])]
    return float(np.mean(xs)) if xs else None


def run(case: SimulationCase, materials: Mapping[str, NeutralMaterial], implicit_best: Extracted,
        exp: Sequence[TestData], adapter: SolverAdapter, overrides: RunOverrides, workdir: Path, cfg: Any, *,
        ncpu: int = 1, timeout_s: float | None = None, cancel: Any = None,
        user_window: Mapping[str, Any] | None = None, has_plastic: bool = True,
        sink: dict[str, Any] | None = None) -> Mapping[str, Any]:
    base: dict[str, Any] = {"test_key": case.test_key, "case_id": case.case_id, "status": "skipped",
                            "run_state": None, "ke_ie_ratio_max": None, "vs_implicit": None, "vs_experiment": None,
                            "within_tolerance": None, "artifact_ids": {}, "skip_reason": None}
    caps = adapter.capabilities()
    if not caps.supports_overrides:
        base["skip_reason"] = "adapter_unsupported"
        return base
    if "explicit" not in caps.analysis_modes or not (((case.manifest or {}).get("deck") or {}).get("controls") or {}).get(
            "explicit"):
        base["skip_reason"] = "no_explicit_controls"
        return base
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        prepared = adapter.prepare(case, materials, workdir, mode="explicit", overrides=overrides)
    except IncompatibleOverride:
        base["skip_reason"] = "mass_scaling"
        return base
    tmo = float(timeout_s if timeout_s is not None else (cfg.get("verify.explicit_check.run_timeout_s")
                                                          or cfg.get("solver.run_timeout_s")))
    status = adapter.run(prepared, ncpu, tmo, cancel or _Never())
    base["run_state"] = status.state.value
    ext = adapter.extract(prepared, status)
    if sink is not None:
        sink["extracted"] = ext
    ke = ext.energy.get("ke_ie_ratio_max")
    base["ke_ie_ratio_max"] = float(ke) if ke is not None else None
    curves = [r for r in case.responses if r.kind == "curve"]
    resp = curves[0] if curves else None
    vs_i, vs_e, shorts = [], [], []
    have_curve = False
    if resp is not None:
        chk = ext.curves.get(resp.response_key)
        imp = implicit_best.curves.get(resp.response_key)
        for d in exp:
            at = AL.prepare_test(d.x, d.y, resp, cfg, compliance_m_per_n=d.compliance_m_per_n, user_window=user_window,
                                 has_plastic=has_plastic)
            if not at.ok:
                continue
            p_chk = AL.align_sim(at, chk, cfg)
            p_imp = AL.align_sim(at, imp, cfg)
            if np.any(np.isfinite(p_chk.f_sim)):
                have_curve = True
            a = _cmp(p_imp.f_sim, p_chk.f_sim, at.region)
            a["shortfall"] = float(p_chk.shortfall)
            b = _cmp(at.f_exp, p_chk.f_sim, at.region)
            b["shortfall"] = float(p_chk.shortfall)
            vs_i.append(a)
            vs_e.append(b)
            shorts.append(p_chk.shortfall)
    if vs_i:
        base["vs_implicit"] = {k: _mean(vs_i, k) for k in ("elastic", "plastic", "peak_force_rel_diff", "shortfall")}
        base["vs_experiment"] = {k: _mean(vs_e, k) for k in ("elastic", "plastic", "peak_force_rel_diff", "shortfall")}
    if status.state != RunState.NORMAL:
        base["status"] = "failed"
        base["within_tolerance"] = False
        return base
    if not have_curve:
        base["status"] = "failed"
        base["within_tolerance"] = False
        return base
    max_e = float(cfg.get("verify.explicit_check.max_region_error"))
    max_p = float(cfg.get("verify.explicit_check.max_peak_rel_diff"))
    vi = base["vs_implicit"] or {}
    errs = [v for v in (vi.get("elastic"), vi.get("plastic")) if v is not None]
    dev = (max(errs) if errs else math.inf) > max_e
    pk = vi.get("peak_force_rel_diff")
    if pk is not None and abs(pk) > max_p:
        dev = True
    base["within_tolerance"] = not dev
    base["status"] = "deviation" if dev else "ok"
    return base
