"""`lsq` 최적화기: 차분 기반 국소 최소제곱(신뢰 영역) — plugins.md §4.1, P-5(탄성 단계 기본).

두 동작 방식(ask/tell 하나의 계약 안에서):

* **잔차 모드(Gauss–Newton / Levenberg–Marquardt)**: `EvalOutcome.components`에 `residual:` 접두 키(엔진이 넣는 잔차 벡터,
  키 사전순 = 벡터 순서)가 있으면 전진 차분 야코비안으로 LM 단계를 밟는다. 반복당 평가 d+1회.
* **스칼라 모드**: 잔차가 없으면 단위 좌표의 국소 2차 모형(d ≤ `full_quadratic_max_d`면 완전 2차, 그 이상은 대각)을
  J와 J² 둘에 맞춰 신뢰 영역 Newton 후보 1–2개를 평가한다(J가 이차 함수이든 노름(√이차)이든 빠르게 수렴).

옵션: `fd_step_rel`(0.01, 단위 좌표 차분 간격), `max_iter`(50), `ftol`(1e-6, 상대 개선 수렴 판정),
`xtol`(1e-7), `initial_radius`(0.25), `full_quadratic_max_d`(3), `residual_prefix`("residual:").
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Mapping

import numpy as np

from matobserver.core.types import StopSignal

from ._common import BaseOptimizer

_MATCH_TOL = 1e-9


class LeastSquares(BaseOptimizer):
    name: ClassVar[str] = "lsq"
    version: ClassVar[str] = "1.0.0"
    max_batch: ClassVar[int] = 1

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        o = self.options
        self.h = float(o.get("fd_step_rel", 0.01))
        self.max_iter = int(o.get("max_iter", 50))
        self.ftol = float(o.get("ftol", 1e-6))
        self.xtol = float(o.get("xtol", 1e-7))
        self.radius0 = float(o.get("initial_radius", 0.25))
        self.full_qd = int(o.get("full_quadratic_max_d", 3))
        self.prefix = str(o.get("residual_prefix", "residual:"))
        self.damping_floor = float(o.get("damping_floor", 1e-4))
        self._reset_impl()

    def _reset_impl(self) -> None:
        self._phase = "start"  # start | center | stencil | trial | done
        self._plan: list[dict[str, Any]] = []
        self._center: dict[str, Any] | None = None  # {"u","J","f","r"}
        self._radius = self.radius0
        self._mu = 1e-3
        self._mode: str | None = None  # "gn" | "scalar"
        self._model: dict[str, Any] | None = None
        self._iter = 0
        self._small_steps = 0
        self._stop_reason = "none"

    def _on_setup(self) -> None:
        self._reset_impl()

    # ---- ask ----
    def _propose(self) -> np.ndarray:
        for _ in range(3):
            for e in self._plan:
                if not e["asked"]:
                    e["asked"] = True
                    return np.asarray(e["u"], dtype=float)
            if self._plan and all(e["J"] is not None for e in self._plan):
                self._advance()
                continue
            if not self._plan and self._phase in ("start", "center", "stencil", "trial"):
                self._advance()
                continue
            break
        # 계획점이 모두 결과 대기 중이거나 종료됨 → 탐색용 무작위 실현 가능점(이력에만 반영)
        return self._random_feasible()

    # ---- tell ----
    def _on_tell(self, index: int) -> None:
        u = np.asarray(self._X[index])
        th = self._T[index]
        J = self._y[index]
        r = self._residuals(index)
        target = None
        for e in self._plan:
            if e["asked"] and e["J"] is None and e["theta"] == th:
                target = e
                break
        if target is None:
            for e in self._plan:
                if e["asked"] and e["J"] is None and np.max(np.abs(np.asarray(e["u"]) - u)) <= _MATCH_TOL:
                    target = e
                    break
        if target is not None:
            target["J"] = J
            target["r"] = r
        if self._plan and all(e["J"] is not None for e in self._plan):
            self._advance()

    def _residuals(self, index: int) -> list[float] | None:
        comp = self._comp[index]
        keys = sorted(k for k in comp if k.startswith(self.prefix))
        if not keys:
            return None
        return [comp[k] for k in keys]

    # ---- 상태 기계 ----
    def _entry(self, u: np.ndarray) -> dict[str, Any]:
        u = np.clip(np.asarray(u, dtype=float), 0.0, 1.0)
        if self.codec.has_constraints and self._center is not None:
            u = self.codec.repair(u, np.asarray(self._center["u"]))
        theta = self.codec.from_unit(u)
        return {"u": [float(v) for v in self.codec.to_unit(theta)], "theta": theta, "asked": False, "J": None, "r": None}

    def _advance(self) -> None:
        if self._phase == "done":
            self._plan = []
            return
        if self._phase == "start":
            bi = self._best_index()
            if bi is None:
                self._phase = "center"
                self._plan = [self._entry(self.codec.initial_unit())]
                return
            self._set_center_from_history(bi)
            self._plan_stencil()
            return
        if self._phase == "center":
            e = self._plan[0]
            self._center = {"u": e["u"], "J": e["J"], "r": e["r"]}
            self._plan_stencil()
            return
        if self._phase == "stencil":
            if self._build_model():
                self._plan_trial()
            return
        if self._phase == "trial":
            self._evaluate_trial()
            return

    def _set_center_from_history(self, bi: int) -> None:
        self._center = {"u": list(self._X[bi]), "J": self._y[bi], "r": self._residuals(bi)}

    def _objective(self, J: float, r: list[float] | None) -> float:
        if self._mode == "gn" and r is not None:
            return float(np.dot(r, r))
        return J

    # 차분점 계획
    def _plan_stencil(self) -> None:
        assert self._center is not None
        if self._iter >= self.max_iter:
            self._finish("exhausted")
            return
        c = np.asarray(self._center["u"], dtype=float)
        d = self.codec.d
        if self._mode is None:
            self._mode = "gn" if self._center["r"] is not None else "scalar"
        h = self.h
        pts: list[np.ndarray] = []
        if self._mode == "gn":
            for i in range(d):
                s = 1.0 if c[i] + h <= 1.0 else -1.0
                p = c.copy()
                p[i] += s * h
                pts.append(p)
        else:
            sgn = np.where(c + h <= 1.0, 1.0, -1.0)
            for i in range(d):
                if c[i] + h <= 1.0 and c[i] - h >= 0.0:
                    offs = (h, -h)
                elif c[i] + h > 1.0:
                    offs = (-h, -2 * h)
                else:
                    offs = (h, 2 * h)
                for o in offs:
                    p = c.copy()
                    p[i] += o
                    pts.append(p)
            if d <= self.full_qd:
                for i in range(d):
                    for j in range(i + 1, d):
                        p = c.copy()
                        p[i] += sgn[i] * h
                        p[j] += sgn[j] * h
                        pts.append(p)
        self._plan = [self._entry(p) for p in pts]
        self._phase = "stencil"
        if not self._plan:  # d == 0
            self._finish("converged")

    def _build_model(self) -> bool:
        assert self._center is not None
        c = np.asarray(self._center["u"], dtype=float)
        d = self.codec.d
        P = np.array([np.asarray(e["u"]) - c for e in self._plan])
        if self._mode == "gn":
            rc = np.asarray(self._center["r"], dtype=float)
            rs = [e["r"] for e in self._plan]
            if any(r is None or len(r) != len(rc) for r in rs):
                self._mode = "scalar"
                self._plan_stencil()
                return False
            Jac = np.zeros((len(rc), d))
            for k, e in enumerate(self._plan):
                i = int(np.argmax(np.abs(P[k])))
                step = P[k, i]
                if abs(step) < 1e-15:
                    continue
                Jac[:, i] = (np.asarray(e["r"]) - rc) / step
            self._model = {"kind": "gn", "Jac": Jac.tolist(), "rc": rc.tolist()}
            return True
        # 스칼라: J 와 J² 2차 회귀
        Jc = float(self._center["J"])
        ys = np.array([e["J"] for e in self._plan], dtype=float)
        full = d <= self.full_qd
        X = np.vstack([np.zeros(d), P])
        Y = np.concatenate([[Jc], ys])
        feats = _quad_features(X, full)
        models = {}
        for key, vals in (("J", Y), ("J2", Y * Y)):
            coef, *_ = np.linalg.lstsq(feats, vals, rcond=None)
            g, H = _unpack_quad(coef, d, full)
            models[key] = {"g": g.tolist(), "H": H.tolist()}
        self._model = {"kind": "scalar", **models}
        return True

    def _plan_trial(self) -> None:
        if self._phase == "done":
            return
        assert self._center is not None and self._model is not None
        c = np.asarray(self._center["u"], dtype=float)
        cands: list[np.ndarray] = []
        if self._model["kind"] == "gn":
            Jac = np.asarray(self._model["Jac"])
            rc = np.asarray(self._model["rc"])
            A = Jac.T @ Jac
            g = Jac.T @ rc
            if np.linalg.norm(g) <= 1e-300:
                self._finish("converged")
                return
            d = len(g)
            # 활성 집합: 경계에 붙어 있고 기울기가 바깥을 향하는 변수는 이번 단계에서 고정(사영 LM)
            free = np.array([not ((c[i] <= 1e-12 and g[i] > 0) or (c[i] >= 1 - 1e-12 and g[i] < 0)) for i in range(d)])
            if not np.any(free):
                self._finish("converged")
                return
            Af = A[np.ix_(free, free)]
            gf = g[free]
            # 감쇠 하한: 둔감 변수(유효 범위 밖 테이블 노드 등)의 diag≈0 이 단계를 지배해 신뢰 반경에 잘려
            # 유효 방향 이동이 사라지는 것을 막는다(최대 대각의 1e-4 를 하한으로).
            dmax = float(np.max(np.diag(Af))) if Af.size else 1.0
            D = np.maximum(np.diag(Af), self.damping_floor * max(dmax, 1e-300))
            try:
                pf = -np.linalg.solve(Af + self._mu * np.diag(D), gf)
            except np.linalg.LinAlgError:
                pf = -gf / max(float(np.max(D)), 1e-300)
            p = np.zeros(d)
            p[free] = pf
            nrm = float(np.linalg.norm(p))
            if nrm > self._radius:
                p *= self._radius / nrm
            cands.append(np.clip(c + p, 0.0, 1.0))
        else:
            for key in ("J", "J2"):
                g = np.asarray(self._model[key]["g"])
                H = np.asarray(self._model[key]["H"])
                p = _trust_step(g, H, self._radius)
                cands.append(np.clip(c + p, 0.0, 1.0))
        uniq: list[np.ndarray] = []
        for q in cands:
            if np.max(np.abs(q - c)) <= self.xtol:
                continue
            if all(np.max(np.abs(q - o)) > 1e-12 for o in uniq):
                uniq.append(q)
        if not uniq:
            self._finish("converged")
            return
        self._plan = [self._entry(q) for q in uniq]
        self._phase = "trial"

    def _evaluate_trial(self) -> None:
        assert self._center is not None
        best = min(self._plan, key=lambda e: self._objective(e["J"], e["r"]))
        f_new = self._objective(best["J"], best["r"])
        f_old = self._objective(self._center["J"], self._center["r"])
        c = np.asarray(self._center["u"])
        step = float(np.linalg.norm(np.asarray(best["u"]) - c))
        if f_new < f_old:
            rel = (f_old - f_new) / max(abs(f_old), 1e-300)
            self._center = {"u": best["u"], "J": best["J"], "r": best["r"]}
            self._iter += 1
            if self._mode == "gn":
                self._mu = max(self._mu / 3.0, 1e-10)
            self._radius = min(1.0, max(self._radius, 2.0 * step))
            if f_new <= 1e-28:
                self._finish("converged")
                return
            self._small_steps = self._small_steps + 1 if (rel < self.ftol or step < self.xtol) else 0
            if self._small_steps >= 2:
                self._finish("converged")
                return
            self._plan_stencil()
        else:
            if self._mode == "gn":
                self._mu *= 4.0
                self._radius = max(step * 0.5, 1e-12)
            else:
                self._radius = max(min(self._radius, step) * 0.25, 0.0)
            if self._radius < self.xtol or self._mu > 1e10:
                self._finish("converged")
                return
            self._plan_trial()

    def _finish(self, reason: str) -> None:
        self._phase = "done"
        self._plan = []
        self._stop_reason = reason

    def suggest_stop(self):
        if self._phase == "done" and not self._pending_initial:
            return StopSignal(stop=True, reason="converged" if self._stop_reason == "converged" else "exhausted")
        return StopSignal(stop=False, reason="none")

    # ---- 상태 ----
    def _state_impl(self) -> dict[str, Any]:
        return {
            "phase": self._phase, "plan": _plain(self._plan), "center": _plain(self._center),
            "radius": self._radius, "mu": self._mu, "mode": self._mode, "model": _plain(self._model),
            "iter": self._iter, "small_steps": self._small_steps, "stop_reason": self._stop_reason,
        }

    def _restore_impl(self, data: Mapping[str, Any]) -> None:
        self._reset_impl()
        if not data:
            return
        self._phase = data["phase"]
        self._plan = [dict(e, theta=dict(e["theta"])) for e in data["plan"]]
        self._center = dict(data["center"]) if data["center"] is not None else None
        self._radius = float(data["radius"])
        self._mu = float(data["mu"])
        self._mode = data["mode"]
        self._model = data["model"]
        self._iter = int(data["iter"])
        self._small_steps = int(data["small_steps"])
        self._stop_reason = data["stop_reason"]


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def _quad_features(P: np.ndarray, full: bool) -> np.ndarray:
    n, d = P.shape
    cols = [np.ones(n)]
    cols += [P[:, i] for i in range(d)]
    if full:
        for i in range(d):
            for j in range(i, d):
                cols.append(P[:, i] * P[:, j])
    else:
        cols += [P[:, i] ** 2 for i in range(d)]
    return np.column_stack(cols)


def _unpack_quad(coef: np.ndarray, d: int, full: bool) -> tuple[np.ndarray, np.ndarray]:
    g = coef[1 : 1 + d].copy()
    H = np.zeros((d, d))
    k = 1 + d
    if full:
        for i in range(d):
            for j in range(i, d):
                if i == j:
                    H[i, i] = 2.0 * coef[k]
                else:
                    H[i, j] = H[j, i] = coef[k]
                k += 1
    else:
        for i in range(d):
            H[i, i] = 2.0 * coef[k + i]
    return g, H


def _trust_step(g: np.ndarray, H: np.ndarray, radius: float) -> np.ndarray:
    """min gᵀp + ½pᵀHp, ‖p‖ ≤ radius (고유분해 + μ 이분)."""
    d = len(g)
    if d == 0 or not np.all(np.isfinite(g)) or not np.all(np.isfinite(H)):
        return np.zeros(d)
    Hs = 0.5 * (H + H.T)
    lam, Q = np.linalg.eigh(Hs)
    gq = Q.T @ g
    gn = float(np.linalg.norm(g))
    if gn <= 1e-300:
        return np.zeros(d)

    def step(mu: float) -> np.ndarray:
        return -(Q @ (gq / (lam + mu)))

    lam_min = float(lam.min())
    if lam_min > 1e-14 * max(1.0, float(np.abs(lam).max())):
        p = step(0.0)
        if np.linalg.norm(p) <= radius:
            return p
    lo = max(0.0, -lam_min) + 1e-14 * max(1.0, float(np.abs(lam).max()))
    hi = lo + gn / max(radius, 1e-300) + float(np.abs(lam).max()) + 1.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if np.linalg.norm(step(mid)) > radius:
            lo = mid
        else:
            hi = mid
    p = step(hi)
    if not np.all(np.isfinite(p)):
        p = -g / gn * radius
    return p
