"""1요소 왕복 검사 (result.md §4.9, module-boundaries.md §4.6).

슬롯의 최종 `card_blocks`(NeutralMaterial)로 `SolverAdapter.single_element_uniaxial`을 돌려 출력 응력–유효소성변형률이
입력 테이블을 재현하는지 본다. `verify/`를 import하지 않는다. artifact 등록은 호출자(engine.run)가 한다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from matobserver.core.types import NeutralMaterial, SolverAdapter


def table_stress(material: NeutralMaterial, eps_p: np.ndarray) -> np.ndarray:
    """입력 테이블 σ(εp): 구간 선형, 마지막 구간 기울기 외삽(MAT_024 기본 동작)."""
    assert material.table is not None
    ep = np.array([r.plastic_strain for r in material.table], dtype=float)
    sg = np.array([r.true_stress for r in material.table], dtype=float)
    out = np.interp(eps_p, ep, sg)
    if len(ep) >= 2:
        slope = (sg[-1] - sg[-2]) / (ep[-1] - ep[-2])
        out = np.where(eps_p > ep[-1], sg[-1] + slope * (eps_p - ep[-1]), out)
    return out


def check(slot: str, material: NeutralMaterial, adapter: SolverAdapter, workdir: Path, strain_max: float,
          tol: float, *, timeout_s: float = 3600.0) -> Mapping[str, Any]:
    """반환 = result.md §4.9 `per_slot` 원소 `{slot, passed, max_rel_err, tolerance, artifact_id}` (artifact_id는 None)."""
    base: dict[str, Any] = {"slot": slot, "passed": False, "max_rel_err": None, "tolerance": float(tol), "artifact_id": None}
    if material.table is None:
        base.update({"passed": True, "max_rel_err": 0.0, "skipped": "elastic_only"})
        return base
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    smax = float(strain_max) if strain_max and strain_max > 0 else float(material.table[-1].plastic_strain)
    try:
        curve = adapter.single_element_uniaxial(material, smax, workdir, timeout_s)
    except Exception as e:
        base["error"] = f"{type(e).__name__}: {e}"
        return base
    x = np.asarray(curve.x, dtype=float)
    y = np.asarray(curve.y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y) & (x >= 0.0) & (x <= smax * (1 + 1e-9))
    if not np.any(ok):
        base["error"] = "single_element_uniaxial returned no plastic points"
        return base
    ref = table_stress(material, x[ok])
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(y[ok] - ref) / np.abs(ref)
    rel = rel[np.isfinite(rel)]
    err = float(np.max(rel)) if rel.size else math.inf
    base.update({"passed": bool(err <= tol), "max_rel_err": err, "strain_max": smax, "points": int(np.sum(ok))})
    return base
