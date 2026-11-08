"""최적화기 공통 기반 (plugins.md §4.1).

* 정규화 공간: 변수마다 [0, 1] 단위 좌표. `scale=log`면 log 공간에서 선형 매핑(정규화는 최적화기 내부 책임).
* 제약(`monotonic_nondecreasing`, `linear_le`)은 물리 공간에서 검사하고, 위반점은 실현 가능한 기준점 쪽으로
  이분 탐색해 되돌린다(`repair`). 1차 재료 모델은 제약 없이 변수 정의로 단조성을 보장하지만 계약상 처리한다.
* 결정성(O4·O5): 난수는 `numpy.random.Generator(PCG64(seed))` 하나만 쓰고, `state()`에 비트 생성기 상태를 넣는다.
  JSON 왕복 후에도 같은 수열이 나오도록 모든 상태를 파이썬 기본형(float·int·list·dict)으로만 저장한다.
* `initial_points`는 첫 ask들에서 **그대로**(값 변환 없이) 순서대로 나온다(O2).
"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Mapping, Sequence

import numpy as np

from matobserver.core.types import Constraint, EvalOutcome, ParamSpace, ParamSpec, StopSignal

_FEAS_TOL = 1e-12


def space_to_json(space: ParamSpace) -> dict[str, Any]:
    return {
        "params": [asdict(p) for p in space.params],
        "constraints": [
            {"kind": c.kind, "params": list(c.params), "coeffs": [float(v) for v in c.coeffs], "bound": float(c.bound)}
            for c in space.constraints
        ],
    }


def space_from_json(data: Mapping[str, Any]) -> ParamSpace:
    params = tuple(ParamSpec(**dict(p)) for p in data["params"])
    cons = tuple(
        Constraint(kind=c["kind"], params=tuple(c["params"]), coeffs=tuple(c.get("coeffs", ())), bound=c.get("bound", 0.0))
        for c in data.get("constraints", [])
    )
    return ParamSpace(params=params, constraints=cons)


class SpaceCodec:
    """ParamSpace ↔ 단위 좌표 변환과 제약 검사."""

    def __init__(self, space: ParamSpace) -> None:
        self.space = space
        self.names: tuple[str, ...] = space.names()
        self.d = len(self.names)
        lo, hi, log = [], [], []
        for p in space.params:
            if not (math.isfinite(p.lower) and math.isfinite(p.upper)) or not p.lower < p.upper:
                raise ValueError(f"parameter {p.name!r}: need finite lower < upper, got [{p.lower}, {p.upper}]")
            if p.scale == "log":
                if p.lower <= 0:
                    raise ValueError(f"parameter {p.name!r}: log scale needs lower > 0")
                lo.append(math.log(p.lower))
                hi.append(math.log(p.upper))
                log.append(True)
            else:
                lo.append(float(p.lower))
                hi.append(float(p.upper))
                log.append(False)
        self._lo = np.array(lo, dtype=float)
        self._hi = np.array(hi, dtype=float)
        self._log = np.array(log, dtype=bool)
        self._phys_lo = np.array([p.lower for p in space.params], dtype=float)
        self._phys_hi = np.array([p.upper for p in space.params], dtype=float)
        self._index = {n: i for i, n in enumerate(self.names)}

    # ---- 변환 ----
    def to_unit(self, theta: Mapping[str, float]) -> np.ndarray:
        v = np.array([float(theta[n]) for n in self.names], dtype=float)
        v = np.clip(v, self._phys_lo, self._phys_hi)
        t = np.where(self._log, np.log(np.maximum(v, 1e-300)), v)
        u = (t - self._lo) / (self._hi - self._lo)
        return np.clip(u, 0.0, 1.0)

    def from_unit(self, u: np.ndarray) -> dict[str, float]:
        u = np.clip(np.asarray(u, dtype=float), 0.0, 1.0)
        t = self._lo + u * (self._hi - self._lo)
        v = t.copy()
        v[self._log] = np.exp(t[self._log])
        v = np.clip(v, self._phys_lo, self._phys_hi)  # exp 반올림으로 경계를 넘지 않게
        return {n: float(v[i]) for i, n in enumerate(self.names)}

    def initial_unit(self) -> np.ndarray:
        """ParamSpec.initial(없으면 범위 중앙, log면 기하평균 = 단위 중앙)."""
        u = np.full(self.d, 0.5)
        for i, p in enumerate(self.space.params):
            if p.initial is not None:
                u[i] = self.to_unit({**{n: self.space.params[j].lower for j, n in enumerate(self.names)}, p.name: p.initial})[i]
        return u

    # ---- 제약 ----
    def in_bounds(self, theta: Mapping[str, float]) -> bool:
        for p in self.space.params:
            v = theta[p.name]
            if not (p.lower <= v <= p.upper):
                return False
        return True

    def feasible_theta(self, theta: Mapping[str, float]) -> bool:
        if not self.in_bounds(theta):
            return False
        for c in self.space.constraints:
            vals = [theta[n] for n in c.params]
            if c.kind == "monotonic_nondecreasing":
                if any(vals[i + 1] < vals[i] - _FEAS_TOL * max(1.0, abs(vals[i])) for i in range(len(vals) - 1)):
                    return False
            elif c.kind == "linear_le":
                s = sum(float(a) * float(b) for a, b in zip(c.coeffs, vals))
                scale = max(1.0, abs(c.bound), sum(abs(float(a) * float(b)) for a, b in zip(c.coeffs, vals)))
                if s > c.bound + _FEAS_TOL * scale:
                    return False
            else:  # pragma: no cover - 계약 밖 kind
                raise ValueError(f"unknown constraint kind {c.kind!r}")
        return True

    def feasible(self, u: np.ndarray) -> bool:
        return self.feasible_theta(self.from_unit(u))

    @property
    def has_constraints(self) -> bool:
        return bool(self.space.constraints)

    def repair(self, u: np.ndarray, anchor: np.ndarray | None) -> np.ndarray:
        """u가 제약 위반이면 실현 가능한 anchor 쪽으로 이분 탐색해 경계 근처의 실현 가능점을 돌려준다."""
        u = np.clip(np.asarray(u, dtype=float), 0.0, 1.0)
        if not self.has_constraints or self.feasible(u):
            return u
        if anchor is None or not self.feasible(anchor):
            return u  # 호출자가 다른 점을 고른다
        lo, hi = 0.0, 1.0  # anchor + t·(u−anchor), t=0 실현 가능
        for _ in range(50):
            mid = 0.5 * (lo + hi)
            if self.feasible(anchor + mid * (u - anchor)):
                lo = mid
            else:
                hi = mid
        return anchor + lo * (u - anchor)

    def sample(self, rng: np.random.Generator, n: int, anchor: np.ndarray | None = None) -> np.ndarray:
        """균일 무작위 실현 가능점 n개 (제약이 있으면 기각 표본 + 실패 시 repair)."""
        out = np.empty((n, self.d))
        for k in range(n):
            u = rng.random(self.d)
            tries = 0
            while self.has_constraints and not self.feasible(u) and tries < 200:
                u = rng.random(self.d)
                tries += 1
            if self.has_constraints and not self.feasible(u):
                u = self.repair(u, anchor)
            out[k] = u
        return out


def lhs(rng: np.random.Generator, n: int, d: int) -> np.ndarray:
    """라틴 하이퍼큐브 n×d (결정적: rng에서만 난수)."""
    if n <= 0:
        return np.empty((0, d))
    cut = np.linspace(0.0, 1.0, n + 1)
    u = rng.random((n, d))
    pts = cut[:n, None] + u * (cut[1:, None] - cut[:n, None])
    for j in range(d):
        pts[:, j] = pts[rng.permutation(n), j]
    return pts


def rng_from_seed(seed: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(int(seed) & ((1 << 63) - 1)))


def rng_state(rng: np.random.Generator) -> dict[str, Any]:
    st = rng.bit_generator.state
    return {"bit_generator": st["bit_generator"], "state": {k: int(v) for k, v in st["state"].items()},
            "has_uint32": int(st["has_uint32"]), "uinteger": int(st["uinteger"])}


def rng_restore(state: Mapping[str, Any]) -> np.random.Generator:
    bg = np.random.PCG64()
    bg.state = {"bit_generator": state["bit_generator"], "state": dict(state["state"]),
                "has_uint32": state["has_uint32"], "uinteger": state["uinteger"]}
    return np.random.Generator(bg)


class BaseOptimizer:
    """ask/tell 공통 부분: 초기점 대기열, 이력, best, 상태 직렬화 골격."""

    name = "base"
    version = "0.0.0"
    max_batch = 1

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self._space: ParamSpace | None = None
        self._codec: SpaceCodec | None = None
        self._seed = 0
        self._rng = rng_from_seed(0)
        self._pending_initial: list[dict[str, float]] = []
        self._X: list[list[float]] = []  # 단위 좌표
        self._T: list[dict[str, float]] = []  # 물리값 (best 반환용, tell에 들어온 값 그대로)
        self._y: list[float] = []
        self._pen: list[bool] = []
        self._comp: list[dict[str, float]] = []

    # ---- 계약 메서드 ----
    def setup(self, space: ParamSpace, seed: int, initial_points: Sequence[Mapping[str, float]]) -> None:
        self._space = space
        self._codec = SpaceCodec(space)
        self._seed = int(seed)
        self._rng = rng_from_seed(seed)
        self._pending_initial = []
        for p in initial_points:
            th = {n: float(p[n]) for n in self._codec.names}
            self._pending_initial.append(th)
        self._X, self._T, self._y, self._pen, self._comp = [], [], [], [], []
        self._on_setup()

    def ask(self, n: int = 1) -> list[dict[str, float]]:
        if self._codec is None:
            raise RuntimeError("setup() must be called before ask()")
        if not isinstance(n, int) or n < 1:
            raise ValueError(f"n must be a positive int, got {n!r}")
        if n > self.max_batch:
            raise ValueError(f"{self.name}: n={n} exceeds max_batch={self.max_batch}")
        out: list[dict[str, float]] = []
        for _ in range(n):
            if self._pending_initial:
                out.append(dict(self._pending_initial.pop(0)))
            else:
                u = self._propose()
                out.append(self._codec.from_unit(u))
        return out

    def tell(self, thetas: Sequence[Mapping[str, float]], outcomes: Sequence[EvalOutcome]) -> None:
        if self._codec is None:
            raise RuntimeError("setup() must be called before tell()")
        if len(thetas) != len(outcomes):
            raise ValueError("thetas and outcomes must have the same length")
        for th, oc in zip(thetas, outcomes):
            J = float(oc.J)
            if not math.isfinite(J):
                J = 1e300
            th_c = {n: float(th[n]) for n in self._codec.names}
            u = self._codec.to_unit(th_c)
            self._X.append([float(v) for v in u])
            self._T.append(th_c)
            self._y.append(J)
            self._pen.append(bool(oc.penalized))
            comp = {}
            for k, v in (oc.components or {}).items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                comp[str(k)] = fv if math.isfinite(fv) else 0.0
            self._comp.append(comp)
            self._on_tell(len(self._y) - 1)

    def suggest_stop(self) -> StopSignal:
        return StopSignal(stop=False, reason="none")

    def best(self) -> tuple[dict[str, float], float] | None:
        if not self._y:
            return None
        i = int(np.argmin(np.asarray(self._y)))
        return dict(self._T[i]), float(self._y[i])

    def state(self) -> dict[str, Any]:
        st = {
            "name": self.name,
            "version": self.version,
            "options": _jsonable(self.options),
            "space": space_to_json(self._space) if self._space is not None else None,
            "seed": self._seed,
            "rng": rng_state(self._rng),
            "pending_initial": [dict(p) for p in self._pending_initial],
            "X": [list(x) for x in self._X],
            "T": [dict(t) for t in self._T],
            "y": list(self._y),
            "pen": list(self._pen),
            "comp": [dict(c) for c in self._comp],
            "impl": self._state_impl(),
        }
        return st

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("space") is not None:
            self._space = space_from_json(state["space"])
            self._codec = SpaceCodec(self._space)
        self._seed = int(state.get("seed", 0))
        self._rng = rng_restore(state["rng"])
        self._pending_initial = [dict(p) for p in state.get("pending_initial", [])]
        self._X = [list(map(float, x)) for x in state.get("X", [])]
        self._T = [dict(t) for t in state.get("T", [])]
        self._y = [float(v) for v in state.get("y", [])]
        self._pen = [bool(v) for v in state.get("pen", [])]
        self._comp = [dict(c) for c in state.get("comp", [])]
        self._restore_impl(state.get("impl", {}))

    # ---- 하위 클래스 훅 ----
    def _on_setup(self) -> None:
        pass

    def _on_tell(self, index: int) -> None:
        pass

    def _propose(self) -> np.ndarray:
        raise NotImplementedError

    def _state_impl(self) -> dict[str, Any]:
        return {}

    def _restore_impl(self, data: Mapping[str, Any]) -> None:
        pass

    # ---- 보조 ----
    @property
    def codec(self) -> SpaceCodec:
        assert self._codec is not None
        return self._codec

    def _best_index(self) -> int | None:
        if not self._y:
            return None
        return int(np.argmin(np.asarray(self._y)))

    def _random_feasible(self) -> np.ndarray:
        anchor = None
        bi = self._best_index()
        if bi is not None:
            anchor = np.asarray(self._X[bi])
        elif self.codec.feasible(self.codec.initial_unit()):
            anchor = self.codec.initial_unit()
        return self.codec.sample(self._rng, 1, anchor)[0]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj
