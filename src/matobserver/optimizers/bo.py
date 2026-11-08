"""`bo` 최적화기: 가우스 과정(Matérn 5/2, ARD) + 기대 개선(EI) — plugins.md §4.1, plan 「BO 기본」.

* 초기 설계: `initial_points`(TestSpec 해석식 초기값)를 그대로 먼저 내고, `n_initial`(기본 max(4, 2d))까지 LHS로 채운다.
* 대리 모형: 단위 좌표, 목표값 y = log(J + J_floor) 표준화. 페널티 점(`penalized`)은 유효 점 최댓값으로 자른다
  (실패 페널티가 GP를 왜곡하지 않게). 하이퍼파라미터(길이 척도 d개, 잡음)는 고정 시작점 L-BFGS-B로 주변우도 최대화
  — 난수 없이 결정적.
* 획득 함수 최대화: 무작위 후보(시드 난수) + 상위 점 주변 가우스 섭동 + 상위 후보 국소 L-BFGS-B. 제약은 후보 기각.
* 결정성(O4·O5): 난수는 `_rng` 하나, 상태는 JSON 직렬화. 같은 seed·같은 tell 수열 → 같은 ask 수열.

옵션: `n_initial`, `acquisition`(`ei`만), `kernel`(`matern52`만), `xi`(0.01), `n_candidates`(2048), `n_local`(512),
`j_floor_rel`(1e-6), `max_gp_points`(150), `ei_stop_tol`(1e-10, 연속 3회면 `converged` 제안).
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Mapping

import numpy as np

from matobserver.core.types import StopSignal

from ._common import BaseOptimizer, lhs

try:  # scipy 는 국소 최적화용(없으면 후보 탐색만)
    from scipy.optimize import minimize as _minimize
except Exception:  # pragma: no cover
    _minimize = None

_SQ5 = math.sqrt(5.0)


def _matern52(A: np.ndarray, B: np.ndarray, ls: np.ndarray) -> np.ndarray:
    D = (A[:, None, :] - B[None, :, :]) / ls
    r = np.sqrt(np.maximum(np.sum(D * D, axis=-1), 0.0))
    return (1.0 + _SQ5 * r + 5.0 / 3.0 * r * r) * np.exp(-_SQ5 * r)


def _norm_pdf(z: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


try:
    from scipy.special import ndtr as _ndtr
except Exception:  # pragma: no cover
    _ndtr = None


def _norm_cdf(z: np.ndarray) -> np.ndarray:
    if _ndtr is not None:
        return _ndtr(z)
    from math import erf  # pragma: no cover

    return np.vectorize(lambda t: 0.5 * (1.0 + erf(t / math.sqrt(2.0))), otypes=[float])(z)  # pragma: no cover


class _GP:
    def __init__(self, X: np.ndarray, y: np.ndarray) -> None:
        self.X = X
        self.ym = float(np.mean(y))
        self.ys = float(np.std(y)) or 1.0
        self.y = (y - self.ym) / self.ys
        d = X.shape[1]
        self.log_ls = np.full(d, math.log(0.3))
        self.log_noise = math.log(1e-4)
        self._fit()

    def _nll(self, params: np.ndarray) -> float:
        d = self.X.shape[1]
        ls = np.exp(params[:d])
        noise = math.exp(params[d])
        K = _matern52(self.X, self.X, ls) + (noise + 1e-10) * np.eye(len(self.X))
        try:
            L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            return 1e25
        a = np.linalg.solve(L.T, np.linalg.solve(L, self.y))
        return float(0.5 * self.y @ a + np.sum(np.log(np.diag(L))))

    def _fit(self) -> None:
        d = self.X.shape[1]
        best_p = np.concatenate([self.log_ls, [self.log_noise]])
        best_v = self._nll(best_p)
        bounds = [(math.log(0.02), math.log(5.0))] * d + [(math.log(1e-8), math.log(0.5))]
        starts = [np.concatenate([np.full(d, math.log(v)), [math.log(1e-4)]]) for v in (0.1, 0.3, 1.0)]
        for x0 in starts:
            v0 = self._nll(x0)
            if v0 < best_v:
                best_p, best_v = x0, v0
            if _minimize is not None:
                try:
                    res = _minimize(self._nll, x0, method="L-BFGS-B", bounds=bounds, options={"maxiter": 60})
                    if np.all(np.isfinite(res.x)) and res.fun < best_v:
                        best_p, best_v = np.asarray(res.x), float(res.fun)
                except Exception:  # pragma: no cover
                    pass
        self.log_ls = best_p[:d]
        self.log_noise = float(best_p[d])
        ls = np.exp(self.log_ls)
        K = _matern52(self.X, self.X, ls) + (math.exp(self.log_noise) + 1e-10) * np.eye(len(self.X))
        jitter = 1e-10
        while True:
            try:
                self.L = np.linalg.cholesky(K + jitter * np.eye(len(K)))
                break
            except np.linalg.LinAlgError:
                jitter *= 10
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, self.y))
        self.ls = ls

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Ks = _matern52(Xs, self.X, self.ls)
        mu = Ks @ self.alpha
        v = np.linalg.solve(self.L, Ks.T)
        var = np.maximum(1.0 - np.sum(v * v, axis=0), 1e-12)
        return mu, np.sqrt(var)


class BayesianOptimizer(BaseOptimizer):
    name: ClassVar[str] = "bo"
    version: ClassVar[str] = "1.0.0"
    max_batch: ClassVar[int] = 1

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        o = self.options
        if o.get("acquisition", "ei") != "ei":
            raise ValueError("bo: only acquisition='ei' is supported")
        if o.get("kernel", "matern52") != "matern52":
            raise ValueError("bo: only kernel='matern52' is supported")
        self.xi = float(o.get("xi", 0.01))
        self.n_candidates = int(o.get("n_candidates", 2048))
        self.n_local = int(o.get("n_local", 512))
        self.j_floor_rel = float(o.get("j_floor_rel", 1e-6))
        self.max_gp_points = int(o.get("max_gp_points", 150))
        self.ei_stop_tol = float(o.get("ei_stop_tol", 1e-10))
        self.y_transform = str(o.get("y_transform", "log"))
        self.tr_init = float(o.get("tr_length_init", 0.8))
        self.tr_min = float(o.get("tr_length_min", 2.0 ** -7))
        self.tr_max = float(o.get("tr_length_max", 1.6))
        self.tr_succ_tol = int(o.get("tr_success_tol", 3))
        self._design: list[list[float]] = []
        self._low_ei = 0
        self._tr = {"L": self.tr_init, "succ": 0, "fail": 0}
        self._asks_since_tell = 0

    def _n_initial(self) -> int:
        d = self.codec.d
        return int(self.options.get("n_initial") or max(4, 2 * d))

    def _on_setup(self) -> None:
        n_init = self._n_initial()
        n_lhs = max(0, n_init - len(self._pending_initial))
        pts = lhs(self._rng, n_lhs, self.codec.d)
        design = []
        anchor = self.codec.initial_unit()
        for p in pts:
            if self.codec.has_constraints and not self.codec.feasible(p):
                p = self.codec.repair(p, anchor if self.codec.feasible(anchor) else None)
                if not self.codec.feasible(p):
                    p = self.codec.sample(self._rng, 1, None)[0]
            design.append([float(v) for v in p])
        self._design = design
        self._low_ei = 0
        self._tr = {"L": self.tr_init, "succ": 0, "fail": 0}
        self._asks_since_tell = 0

    def _on_tell(self, index: int) -> None:
        self._asks_since_tell = 0
        # 신뢰 영역(TuRBO 방식) 갱신: 초기 설계 이후에만
        if index < self._n_initial() or index == 0:
            return
        prev = min(self._y[:index])
        J = self._y[index]
        tr = self._tr
        if J < prev - 1e-3 * abs(prev):
            tr["succ"] += 1
            tr["fail"] = 0
        else:
            tr["fail"] += 1
            tr["succ"] = 0
        fail_tol = int(self.options.get("tr_failure_tol") or max(4, self.codec.d))
        if tr["succ"] >= self.tr_succ_tol:
            tr["L"] = min(2.0 * tr["L"], self.tr_max)
            tr["succ"] = 0
        elif tr["fail"] >= fail_tol:
            tr["L"] = max(tr["L"] / 2.0, self.tr_min)
            tr["fail"] = 0

    def _propose(self) -> np.ndarray:
        if self._design:
            return np.asarray(self._design.pop(0))
        if len(self._y) < 2:
            return self._random_feasible()
        return self._acquire()

    def _training(self) -> tuple[np.ndarray, np.ndarray]:
        X = np.asarray(self._X, dtype=float)
        J = np.asarray(self._y, dtype=float)
        pen = np.asarray(self._pen, dtype=bool)
        valid = ~pen
        if np.any(valid):
            cap = float(np.max(J[valid]))
            J = np.where(pen, np.maximum(cap, np.minimum(J, cap)), J)
        if self.y_transform == "none":
            y = J.copy()
        else:
            floor = max(self.j_floor_rel * float(np.median(np.abs(J))), 1e-300)
            y = np.log(np.maximum(J, 0.0) + floor)
        if len(X) > self.max_gp_points:  # 최적 상위 + 최근 점으로 축소 (결정적)
            order = np.argsort(y, kind="stable")
            keep = set(order[: self.max_gp_points // 2].tolist())
            keep |= set(range(len(X) - (self.max_gp_points - len(keep)), len(X)))
            idx = np.array(sorted(keep))
            X, y = X[idx], y[idx]
        return X, y

    def _acquire(self) -> np.ndarray:
        cache = getattr(self, "_gp_cache", None)
        repeat = self._asks_since_tell > 0  # tell 없이 반복 ask (상태에 저장되는 결정적 플래그)
        self._asks_since_tell += 1
        if repeat and cache is not None and cache[0] == len(self._y):  # 같은 데이터 → 같은 GP(결정적)
            gp = cache[1]
        else:
            X, y = self._training()
            gp = _GP(X, y)
            self._gp_cache = (len(self._y), gp)
        ybest = float(np.min(gp.y))
        d = self.codec.d

        def neg_ei(Z: np.ndarray) -> np.ndarray:
            mu, sd = gp.predict(np.atleast_2d(Z))
            imp = ybest - mu - self.xi
            z = imp / sd
            ei = imp * _norm_cdf(z) + sd * _norm_pdf(z)
            return -np.maximum(ei, 0.0)

        bi = int(np.argmin(np.asarray(self._y)))
        center = np.asarray(self._X[bi])
        w = gp.ls / float(np.exp(np.mean(np.log(gp.ls))))
        half = 0.5 * self._tr["L"] * w
        lo = np.clip(center - half, 0.0, 1.0)
        hi = np.clip(center + half, 0.0, 1.0)
        nc = self.n_candidates // 8 if repeat else self.n_candidates
        cand = [lo + (hi - lo) * self._rng.random((nc, d))]
        per = max(1, self.n_local // 2)
        for s in (0.25, 0.05):
            cand.append(np.clip(center + s * half * self._rng.standard_normal((per, d)), lo, hi))
        C = np.vstack(cand)
        bounds_loc = list(zip(lo.tolist(), hi.tolist()))
        if self.codec.has_constraints:
            mask = np.array([self.codec.feasible(c) for c in C])
            C = C[mask] if np.any(mask) else self.codec.sample(self._rng, 16, None)
        vals = neg_ei(C)
        top = np.argsort(vals, kind="stable")[:3]
        best_u, best_v = C[top[0]].copy(), float(vals[top[0]])
        if _minimize is not None and not repeat:
            for t in top:
                try:
                    res = _minimize(lambda z: float(neg_ei(z)[0]), C[t], method="L-BFGS-B",
                                    bounds=bounds_loc, options={"maxiter": 50})
                    z = np.clip(np.asarray(res.x), lo, hi)
                    v = float(neg_ei(z)[0])
                    if v < best_v and (not self.codec.has_constraints or self.codec.feasible(z)):
                        best_u, best_v = z, v
                except Exception:  # pragma: no cover
                    pass
        Xa = np.asarray(self._X)
        if Xa.size and float(np.min(np.max(np.abs(Xa - best_u), axis=1))) < 1e-9:
            best_u = self._random_feasible()  # 이미 평가한 점 반복 방지
        self._low_ei = self._low_ei + 1 if -best_v < self.ei_stop_tol else 0
        return best_u

    def suggest_stop(self) -> StopSignal:
        if (self._low_ei >= 3 and self._tr["L"] <= self.tr_min) and not self._design and not self._pending_initial:
            return StopSignal(stop=True, reason="converged")
        return StopSignal(stop=False, reason="none")

    def _state_impl(self) -> dict[str, Any]:
        return {"design": [list(p) for p in self._design], "low_ei": self._low_ei, "tr": dict(self._tr),
                "asks_since_tell": self._asks_since_tell}

    def _restore_impl(self, data: Mapping[str, Any]) -> None:
        self._design = [list(map(float, p)) for p in data.get("design", [])]
        self._low_ei = int(data.get("low_ei", 0))
        self._asks_since_tell = int(data.get("asks_since_tell", 0))
        self._gp_cache = None
        tr = data.get("tr") or {}
        self._tr = {"L": float(tr.get("L", self.tr_init)), "succ": int(tr.get("succ", 0)), "fail": int(tr.get("fail", 0))}
