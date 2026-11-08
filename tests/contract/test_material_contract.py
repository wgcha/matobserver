"""MaterialModel 계약 테스트 M1–M8 (plugins.md §5.3). 소유: Impl-Optim."""

from __future__ import annotations

import math

import numpy as np
import pytest

from matobserver.core.types import ElasticBlock, NeutralMaterial, TableRow, neutral_material_violations

PLUGIN_TYPE = "material_model"
REF = NeutralMaterial(ElasticBlock(2.0e9, 0.38, 1200.0), (TableRow(0.0, 4.0e7), TableRow(0.05, 5.0e7)))
HINTS = {"E0": 2.2e9, "sigma_y0": 4.0e7, "eps_p_max0": 0.05}
CONFIGS = [{}, {"hardening_law": "voce"}, {"hardening_law": "swift_voce"}, {"table_nodes": 5, "node_spacing": "linear"},
           {"node_spacing": "explicit", "strain_nodes": [0.0, 0.002, 0.01, 0.05]}]


def _model(make_plugin, opts=None, hints=HINTS, ref=REF):
    try:
        m = make_plugin(opts or {})
    except (ValueError, TypeError):
        pytest.skip("option set not applicable to this model")
    m.bind(ref)
    m.initial_guess(hints)
    return m


def _points(space, rng, n):
    pts = [{p.name: p.lower for p in space.params}, {p.name: p.upper for p in space.params},
           {p.name: p.initial for p in space.params}]
    for _ in range(n):
        th = {}
        for p in space.params:
            u = rng.random()
            th[p.name] = math.exp(math.log(p.lower) + u * math.log(p.upper / p.lower)) if p.scale == "log" else \
                p.lower + u * (p.upper - p.lower)
        pts.append(th)
    return pts


@pytest.mark.parametrize("opts", CONFIGS)
def test_m1_param_space(make_plugin, opts):
    m = _model(make_plugin, opts)
    sp = m.param_space()
    names = sp.names()
    assert len(set(names)) == len(names)
    stages = m.capabilities().stages
    for p in sp.params:
        assert p.lower < p.upper
        assert p.initial is not None and p.lower <= p.initial <= p.upper
        assert p.stage in stages
        if p.scale == "log":
            assert p.lower > 0


@pytest.mark.parametrize("opts", CONFIGS)
def test_m2_invariants(make_plugin, opts):
    m = _model(make_plugin, opts)
    rng = np.random.default_rng(0)
    bad = 0
    for th in _points(m.param_space(), rng, 500):
        bad += bool(neutral_material_violations(m.to_neutral(th)))
    assert bad == 0


def test_m2_no_hints(make_plugin):
    m = _model(make_plugin, {}, hints={}, ref=None)
    rng = np.random.default_rng(1)
    for th in _points(m.param_space(), rng, 50):
        assert not neutral_material_violations(m.to_neutral(th))


def test_m3_determinism(make_plugin):
    a, b = _model(make_plugin), _model(make_plugin)
    th = {p.name: p.initial for p in a.param_space().params}
    assert a.param_space() == b.param_space()
    assert a.to_neutral(th).to_card_blocks() == b.to_neutral(th).to_card_blocks()


def test_m4_card_block_keys(make_plugin):
    m = _model(make_plugin)
    cb = m.to_neutral({p.name: p.initial for p in m.param_space().params}).to_card_blocks()
    assert set(cb) == {"elastic", "table", "rate_table"}
    assert set(cb["elastic"]) == {"youngs_modulus", "poisson_ratio", "density"}
    if cb["table"] is not None:
        assert all(set(r) == {"plastic_strain", "true_stress"} for r in cb["table"])


def test_m5_nu_rho_from_reference(make_plugin):
    m = _model(make_plugin)
    nm = m.to_neutral({p.name: p.initial for p in m.param_space().params})
    assert nm.elastic.poisson_ratio == REF.elastic.poisson_ratio
    assert nm.elastic.density == REF.elastic.density


def test_m6_produces_table_flag(make_plugin):
    m = _model(make_plugin)
    nm = m.to_neutral({p.name: p.initial for p in m.param_space().params})
    if not m.capabilities().produces_table:
        assert nm.table is None
    else:
        assert nm.table is not None


def test_m7_coeff_analytic(make_plugin, plugin_name):
    if plugin_name != "mat024_coeff":
        pytest.skip("mat024_coeff only")
    for law in ("swift", "voce", "swift_voce"):
        m = _model(make_plugin, {"hardening_law": law})
        th = {p.name: p.initial for p in m.param_space().params}
        th.update({"K": 6.0e8, "eps0": 0.004, "n": 0.22, "sigma_y": 3.0e8, "Q": 1.5e8, "b": 35.0, "alpha": 0.3})
        th = {k: v for k, v in th.items() if k in m.param_space().names()}
        for row in m.to_neutral(th).table:
            e = row.plastic_strain
            sw = th.get("K", 0) * (th.get("eps0", 1) + e) ** th.get("n", 1) if "K" in th else 0.0
            vo = th["sigma_y"] + th["Q"] * (1 - math.exp(-th["b"] * e)) if "Q" in th else 0.0
            ref = {"swift": sw, "voce": vo, "swift_voce": th.get("alpha", 0) * sw + (1 - th.get("alpha", 0)) * vo}[law]
            assert abs(row.true_stress - ref) <= 1e-9 * ref


def test_m8_table_monotonic(make_plugin, plugin_name):
    if plugin_name != "mat024_table":
        pytest.skip("mat024_table only")
    m = _model(make_plugin, {"table_nodes": 9})
    rng = np.random.default_rng(5)
    viol = 0
    for th in _points(m.param_space(), rng, 500):
        s = [r.true_stress for r in m.to_neutral(th).table]
        viol += any(b < a for a, b in zip(s, s[1:]))
    assert viol == 0


def test_m9_call_order(make_plugin):
    """(1.1) bind → initial_guess → param_space 순서에서 initial 이 경계 안, 같은 순서면 결정적."""
    spaces = []
    for _ in range(2):
        m = make_plugin({})
        m.bind(REF)
        g = m.initial_guess(HINTS)
        sp = m.param_space()
        for p in sp.params:
            assert p.lower <= p.initial <= p.upper
            if p.name in g:
                assert p.lower <= g[p.name] <= p.upper
        spaces.append(sp)
    assert spaces[0] == spaces[1]
