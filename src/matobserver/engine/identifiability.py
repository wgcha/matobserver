"""식별성 사전 점검·사후 진단 (testspec.md §9.2·§9.3, result.md §4.5). 수치 부분은 순수 함수.

사후 진단의 상대 표준편차는 격자점 잔차 독립 가정에 따른 **하한 추정**이다(`relative_stdev_is_lower_bound: true`).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np

from matobserver.core import errors as E
from matobserver.core.hashing import sha256_json


def _layer(l: Any) -> list[Any]:
    if isinstance(l, Mapping):
        return [str(l.get("slot")), float(l.get("thickness", 0.0))]
    a, b = l
    return [str(a), float(b)]


def config_signature(test: Mapping[str, Any]) -> str:
    """시험 구성 해시: 레시피·노출 파라미터·형상·stack_axis (testspec.md §9.2 n_cfg)."""
    g = dict(test.get("geometry") or {})
    return sha256_json({
        "recipe": test.get("recipe_ref"),
        "params": {k: float(v) for k, v in sorted((test.get("params_si") or {}).items())},
        "geometry": {"kind": g.get("kind"), "dimensions": {k: float(v) for k, v in sorted((g.get("dimensions") or {}).items())
                                                           if isinstance(v, (int, float))},
                     "layers": [_layer(l) for l in (g.get("layers") or [])],
                     "stp": str(g.get("stp_path") or g.get("file_id") or "")},
        "stack_axis": g.get("stack_axis", "z"),
    })


def precheck(n_elastic: int, n_plastic: int, config_sigs: Sequence[str], n_points_plastic: int, cfg: Any) -> dict[str, Any]:
    """result.md §4.5 precheck. verdict: ok | warn | underdetermined."""
    n_cfg = len(set(config_sigs))
    codes: list[str] = []
    verdict = "ok"
    if n_elastic > n_cfg:
        codes.append(E.IDENTIFIABILITY_UNDERDETERMINED)
        verdict = "underdetermined"
    mpp = float(cfg.get("identifiability.min_points_per_param")) if cfg is not None else 5.0
    if n_plastic > 0 and n_plastic > n_points_plastic / max(mpp, 1e-12):
        codes.append(E.IDENTIFIABILITY_LOW_DATA)
        if verdict == "ok":
            verdict = "warn"
    return {"n_params": {"elastic": int(n_elastic), "plastic": int(n_plastic)}, "n_configs": int(n_cfg),
            "n_points_plastic": int(n_points_plastic), "verdict": verdict, "codes": codes}


def posterior(names: Sequence[str], S: np.ndarray, r: np.ndarray, cfg: Any, *, stage: str,
              evaluations: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """S: 상대 야코비안 (n_res × n_p) = ∂r/∂ln θ. r: θ*의 잔차. → (post_fit 원소, flags, recommendations)."""
    S = np.asarray(S, dtype=float)
    r = np.asarray(r, dtype=float)
    n_res, n_p = S.shape
    sens = np.linalg.norm(S, axis=0)
    F = S.T @ S
    dof = max(n_res - n_p, 1)
    sigma2 = float(r @ r) / dof
    try:
        cond = float(np.linalg.cond(F)) if n_p else 1.0
    except np.linalg.LinAlgError:  # pragma: no cover
        cond = math.inf
    Finv = np.linalg.pinv(F) if n_p else np.zeros((0, 0))
    cov = sigma2 * Finv
    d = np.sqrt(np.maximum(np.diag(cov), 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = cov / np.outer(d, d)
    corr = np.where(np.isfinite(corr), corr, 0.0)
    np.fill_diagonal(corr, 1.0)
    corr = np.clip(corr, -1.0, 1.0)
    corr_thr = float(cfg.get("identifiability.corr_threshold"))
    min_s = float(cfg.get("identifiability.min_sensitivity"))
    max_c = float(cfg.get("identifiability.max_condition"))
    flags: list[dict[str, Any]] = []
    recs: list[dict[str, Any]] = []
    for i in range(n_p):
        for j in range(i + 1, n_p):
            if abs(corr[i, j]) > corr_thr:
                flags.append({"code": E.HIGH_CORRELATION, "stage": stage, "params": [names[i], names[j]],
                              "value": float(corr[i, j])})
                low = names[i] if sens[i] < sens[j] else names[j]
                if not any(rc["param"] == low and rc["reason"] == E.HIGH_CORRELATION for rc in recs):
                    recs.append({"action": "fix_param", "param": low, "reason": E.HIGH_CORRELATION})
    for i in range(n_p):
        if sens[i] < min_s:
            flags.append({"code": E.INSENSITIVE_PARAM, "stage": stage, "params": [names[i]], "value": float(sens[i])})
            recs.append({"action": "fix_param", "param": names[i], "reason": E.INSENSITIVE_PARAM})
    if math.isfinite(cond) and cond > max_c or not math.isfinite(cond):
        flags.append({"code": E.ILL_CONDITIONED, "stage": stage, "params": list(names),
                      "value": float(cond) if math.isfinite(cond) else 1e308})
    entry = {
        "stage": stage,
        "params": list(names),
        "normalized_sensitivity": {n: float(s) for n, s in zip(names, sens)},
        "correlation": [[float(v) for v in row] for row in corr],
        "relative_stdev": {n: float(v) for n, v in zip(names, d)},
        "relative_stdev_is_lower_bound": True,
        "condition_number": float(cond) if math.isfinite(cond) else 1e308,
        "evaluations": int(evaluations),
    }
    return entry, flags, recs
