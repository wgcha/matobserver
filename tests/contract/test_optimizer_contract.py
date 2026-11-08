"""Optimizer 계약 테스트 O1–O8 (plugins.md §5.1) — 레지스트리 enabled 구현마다 매개변수화. 소유: Impl-Optim."""

from __future__ import annotations

import json
import math

import numpy as np

import pytest

from matobserver.core.types import Constraint, EvalOutcome, ParamSpace, ParamSpec

PLUGIN_TYPE = "optimizer"


def _space(constrained: bool = False) -> ParamSpace:
    ps = (ParamSpec("a", "1", -1.0, 2.0, None, "linear", "plastic"),
          ParamSpec("b", "Pa", 1e6, 1e12, None, "log", "plastic"))
    cons = (Constraint("linear_le", ("a",), (1.0,), 1.5),) if constrained else ()
    return ParamSpace(ps, cons)


def _quad(t) -> float:
    return (t["a"] - 0.7) ** 2 + 0.5 * (math.log10(t["b"]) - 8.2) ** 2


def _ok(space: ParamSpace, t) -> bool:
    for p in space.params:
        if not (p.lower <= t[p.name] <= p.upper):
            return False
    for c in space.constraints:
        if sum(k * t[n] for k, n in zip(c.coeffs, c.params)) > c.bound + 1e-12:
            return False
    return True


def test_o1_bounds_and_constraints(make_plugin):
    sp = _space(constrained=True)
    opt = make_plugin({})
    opt.setup(sp, 1, [{"a": 0.0, "b": 1e9}])
    pts = []
    for _ in range(40):  # 최적화 경로(tell 포함)
        th = opt.ask(1)
        pts += th
        opt.tell(th, [EvalOutcome(_quad(th[0]), False)])
    while len(pts) < 1000:  # tell 없이 연속 ask (표본기·상태 기계)
        pts += opt.ask(min(opt.max_batch, 1000 - len(pts)))
    assert len(pts) == 1000
    assert sum(not _ok(sp, t) for t in pts) == 0


def test_o2_initial_points_first(make_plugin):
    init = [{"a": 0.123456789, "b": 3.3e8}, {"a": -0.5, "b": 2.0e10}]
    opt = make_plugin({})
    opt.setup(_space(), 3, init)
    got = [opt.ask(1)[0] for _ in init]
    assert got == init


def test_o3_penalized_and_huge_J(make_plugin):
    opt = make_plugin({})
    opt.setup(_space(), 5, [])
    for i in range(25):
        th = opt.ask(1)
        J = 1e30 if i % 3 == 0 else _quad(th[0])
        opt.tell(th, [EvalOutcome(J, i % 3 == 0, {"x": 1.0})])
    assert opt.best() is not None


def _sequence(make_plugin, seed: int, n: int = 20):
    opt = make_plugin({})
    opt.setup(_space(), seed, [])
    out = []
    for _ in range(n):
        th = opt.ask(1)
        out.append(th[0])
        opt.tell(th, [EvalOutcome(_quad(th[0]), False)])
    return out


def test_o4_determinism(make_plugin):
    assert _sequence(make_plugin, 11) == _sequence(make_plugin, 11)


def test_o5_state_restore(make_plugin):
    a = make_plugin({})
    a.setup(_space(), 7, [{"a": 0.0, "b": 1e9}])
    for _ in range(9):
        th = a.ask(1)
        a.tell(th, [EvalOutcome(_quad(th[0]), False)])
    st = json.loads(json.dumps(a.state()))
    b = make_plugin({})
    b.setup(_space(), 999, [])
    b.restore(st)
    for _ in range(5):
        ta, tb = a.ask(1), b.ask(1)
        assert ta == tb
        a.tell(ta, [EvalOutcome(_quad(ta[0]), False)])
        b.tell(tb, [EvalOutcome(_quad(tb[0]), False)])


def _converge(make_plugin, plugin_name, space, f, init):
    budget = {"bo": 40, "lsq": 20}.get(plugin_name)
    if budget is None:
        pytest.skip(f"{plugin_name}: 수렴 기준 없음(시험용 최적화기)")
    opt = make_plugin({})
    opt.setup(space, 2026, [init])
    best = math.inf
    for _ in range(budget):
        if opt.suggest_stop().stop:
            break
        th = opt.ask(1)
        J = f(th[0])
        best = min(best, J)
        opt.tell(th, [EvalOutcome(J, False)])
    return best


def test_o6_quadratic_convergence(make_plugin, plugin_name):
    sp = ParamSpace((ParamSpec("a", "1", -1.0, 2.0, None, "linear", "plastic"),
                     ParamSpec("c", "1", 0.0, 10.0, None, "linear", "plastic")))
    best = _converge(make_plugin, plugin_name, sp, lambda t: (t["a"] - 0.7) ** 2 + 0.3 * (t["c"] / 10 - 0.35) ** 2,
                     {"a": 0.0, "c": 5.0})
    assert best < 1e-3


def test_o7_log_scale_normalization(make_plugin, plugin_name):
    best = _converge(make_plugin, plugin_name, _space(), _quad, {"a": 0.0, "b": 1e9})
    assert best < 1e-3


def test_o8_max_batch(make_plugin):
    opt = make_plugin({})
    opt.setup(_space(), 1, [])
    with pytest.raises(ValueError):
        opt.ask(opt.max_batch + 1)


def test_o9_residual_components(make_plugin, plugin_name):
    """(1.1) components 의 residual: 키가 있으면 사용(lsq), 없으면 스칼라 모드. 다른 최적화기는 무시해도 예외 없음."""
    sp = _space()
    g = np.linspace(0, 1, 30)
    for with_r in (True, False):
        opt = make_plugin({})
        opt.setup(sp, 4, [{"a": 0.0, "b": 1e9}])
        best = math.inf
        for _ in range(30):
            if opt.suggest_stop().stop:
                break
            th = opt.ask(1)[0]
            r = (th["a"] - 0.7) * g + (math.log10(th["b"]) - 8.2) * g**2
            J = float(np.linalg.norm(r))
            comp = {f"residual:{i:07d}": float(v) for i, v in enumerate(r)} if with_r else {}
            opt.tell([th], [EvalOutcome(J, False, comp)])
            best = min(best, J)
        if plugin_name == "lsq":
            assert best < 1e-3
