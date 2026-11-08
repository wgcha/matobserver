"""목적함수와 다중 시험 결합 (testspec.md §8) — 순수 모듈(module-boundaries.md §4.6).

e_{r,i,k} = ‖F_sim − F_exp‖_{G_k^sim} / ‖F_exp‖_{G_k}            (Ω_k = ∅ 이면 0)
J_{r,i}   = Σ_k w_k·e_{r,i,k} + λ_s·s_r
J_t       = Σ_i w_i·mean_r(J_{r,i}) / Σ_i w_i
J         = Σ_t W_t·J_t / Σ_t W_t
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from .alignment import AlignedPair, cget

REGIONS = ("elastic", "plastic")


def region_errors(pair: AlignedPair) -> dict[str, float]:
    """구간별 정규화 오차 e_k."""
    out: dict[str, float] = {}
    for k in REGIONS:
        m = pair.region == k
        if not np.any(m):
            out[k] = 0.0
            continue
        fe = pair.f_exp[m]
        den = math.sqrt(float(np.sum(fe * fe)))
        fs = pair.f_sim[m]
        ok = np.isfinite(fs)
        num = math.sqrt(float(np.sum((fs[ok] - fe[ok]) ** 2))) if np.any(ok) else 0.0
        out[k] = num / den if den > 0 else 0.0
    return out


def _weights(region_weights: Mapping[str, float] | None, cfg: Any) -> dict[str, float]:
    w = dict(cget(cfg, "objective.region_weights") or {"elastic": 1.0, "plastic": 1.0}) if cfg is not None else {
        "elastic": 1.0, "plastic": 1.0}
    if region_weights:
        w.update({k: float(v) for k, v in region_weights.items()})
    return {k: float(w.get(k, 1.0)) for k in REGIONS}


def curve_error(pair: AlignedPair, region_weights: Mapping[str, float], cfg: Any) -> float:
    """module-boundaries.md §4.6 공개 함수: J_{r,i} = Σ_k w_k e_k + λ_s·s."""
    w = _weights(region_weights, cfg)
    e = region_errors(pair)
    lam = float(cget(cfg, "objective.shortfall_penalty"))
    return float(sum(w[k] * e[k] for k in REGIONS) + lam * pair.shortfall)


def curve_terms(pair: AlignedPair, region_weights: Mapping[str, float], cfg: Any) -> dict[str, float]:
    """J와 구성요소 {J, elastic, plastic, shortfall}."""
    w = _weights(region_weights, cfg)
    e = region_errors(pair)
    lam = float(cget(cfg, "objective.shortfall_penalty"))
    J = float(sum(w[k] * e[k] for k in REGIONS) + lam * pair.shortfall)
    return {"J": J, "elastic": e["elastic"], "plastic": e["plastic"], "shortfall": float(pair.shortfall)}


def scalar_error(s_sim: float, s_exp: float, cfg: Any) -> float:
    floor = float(cget(cfg, "objective.scalar_floor")) if cfg is not None else 1e-12
    return abs(float(s_sim) - float(s_exp)) / max(abs(float(s_exp)), floor)


def residual_vector(pair: AlignedPair, region_weights: Mapping[str, float], cfg: Any, scale: float = 1.0) -> np.ndarray:
    """잔차 r_g = √(scale·w_k)·(F_sim − F_exp)/‖F_exp‖_{Ω_k} (testspec.md §9.3). 해석 범위 밖 격자점은 F_sim = 0.

    w_k = 0 인 구간(탄성 단계의 소성 구간)은 제외해 길이를 단계 안에서 고정한다.
    """
    w = _weights(region_weights, cfg)
    parts = []
    for k in REGIONS:
        if w[k] <= 0:
            continue
        m = pair.region == k
        if not np.any(m):
            continue
        fe = pair.f_exp[m]
        den = math.sqrt(float(np.sum(fe * fe))) or 1.0
        fs = np.where(np.isfinite(pair.f_sim[m]), pair.f_sim[m], 0.0)
        parts.append(math.sqrt(scale * w[k]) * (fs - fe) / den)
    return np.concatenate(parts) if parts else np.array([])


def combine_tests(per_test: Sequence[tuple[float, Mapping[str, tuple[float, float]]]]) -> tuple[float, list[float]]:
    """per_test: [(W_t, {response_key: (w_i, mean_r J_{r,i})})] → (J, [J_t])."""
    Jt_list = []
    num = den = 0.0
    for W, resp in per_test:
        sw = sum(w for w, _ in resp.values())
        Jt = sum(w * j for w, j in resp.values()) / sw if sw > 0 else 0.0
        Jt_list.append(Jt)
        num += W * Jt
        den += W
    return (num / den if den > 0 else 0.0), Jt_list
