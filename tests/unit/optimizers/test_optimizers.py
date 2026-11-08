"""최적화기 단위: lsq 잔차(Gauss–Newton) 모드·경계 활성 집합, bo 신뢰 영역 상태, 제약 repair."""

from __future__ import annotations

import json

import numpy as np

from matobserver.core.types import Constraint, EvalOutcome, ParamSpace, ParamSpec
from matobserver.optimizers._common import SpaceCodec
from matobserver.optimizers.bo import BayesianOptimizer
from matobserver.optimizers.lsq import LeastSquares
from matobserver.optimizers.random_search import RandomSearch

SP = ParamSpace((ParamSpec("E", "Pa", 1e8, 1e10, 5e8, "log", "elastic"),
                 ParamSpec("d", "Pa", 0.0, 1e7, 5e6, "linear", "plastic")))


def _resid(t):
    g = np.linspace(0, 1, 50)
    model = t["E"] / 2.3e9 * g + t["d"] / 1e7 * g**2
    truth = g + 0.0 * g**2  # d 의 참값 0 (하한 경계)
    r = model - truth
    return r, float(np.linalg.norm(r))


def test_lsq_gauss_newton_with_bound():
    o = LeastSquares({})
    o.setup(SP, 0, [{"E": 5e8, "d": 5e6}])
    for _ in range(40):
        if o.suggest_stop().stop:
            break
        th = o.ask(1)[0]
        r, J = _resid(th)
        o.tell([th], [EvalOutcome(J, False, {f"residual:{i:07d}": v for i, v in enumerate(r)})])
    t, J = o.best()
    assert abs(t["E"] / 2.3e9 - 1) < 1e-4 and t["d"] < 1e3 and J < 1e-4
    assert o._mode == "gn"


def test_lsq_1d_scalar_converges_and_stops():
    sp = ParamSpace((ParamSpec("E", "Pa", 1e8, 1e10, 5e8, "log", "elastic"),))
    o = LeastSquares({})
    o.setup(sp, 0, [])
    n = 0
    while not o.suggest_stop().stop and n < 60:
        th = o.ask(1)[0]
        o.tell([th], [EvalOutcome(abs(th["E"] / 2.3e9 - 1), False)])
        n += 1
    assert o.suggest_stop().stop and abs(o.best()[0]["E"] / 2.3e9 - 1) < 1e-3


def test_bo_state_contains_trust_region_and_roundtrips():
    o = BayesianOptimizer({})
    o.setup(SP, 3, [])
    for _ in range(8):
        th = o.ask(1)[0]
        o.tell([th], [EvalOutcome(_resid(th)[1], False)])
    st = json.loads(json.dumps(o.state()))
    assert st["impl"]["tr"]["L"] > 0 and st["space"]["params"][0]["name"] == "E"


def test_constraint_repair_and_sampling():
    sp = ParamSpace((ParamSpec("a", "1", 0, 1, 0.1, "linear", "plastic"), ParamSpec("b", "1", 0, 1, 0.2, "linear", "plastic")),
                    (Constraint("monotonic_nondecreasing", ("a", "b")),))
    c = SpaceCodec(sp)
    rng = np.random.default_rng(0)
    pts = c.sample(rng, 200, c.initial_unit())
    assert all(c.feasible(p) for p in pts)
    fixed = c.repair(np.array([0.9, 0.1]), c.initial_unit())
    assert c.feasible(fixed)
    r = RandomSearch({})
    r.setup(sp, 1, [])
    assert all(t["b"] >= t["a"] for t in r.ask(50))
