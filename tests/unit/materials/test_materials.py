"""재료 모델 단위: 힌트 의존 범위(plugins.md §4.3 표), 기본 범위(STP), 노드·샘플링, describe."""

from __future__ import annotations

import pytest

from matobserver.core.types import ElasticBlock, NeutralMaterial, TableRow
from matobserver.materials.mat001 import Mat001
from matobserver.materials.mat024 import Mat024Coeff, Mat024Table, strain_samples

REF = NeutralMaterial(ElasticBlock(2e9, 0.38, 1200.0), (TableRow(0.0, 4e7), TableRow(0.1, 5e7)))
H = {"E0": 2e9, "sigma_y0": 4e7}


def _space(m, hints=H, ref=REF):
    m.bind(ref)
    m.initial_guess(hints)
    return {p.name: p for p in m.param_space().params}


def test_ranges_follow_contract_table():
    p = _space(Mat001({}))
    assert (p["E"].lower, p["E"].upper, p["E"].scale, p["E"].stage) == (2e8, 2e10, "log", "elastic")
    s = _space(Mat024Coeff({"hardening_law": "swift"}))
    assert (s["K"].lower, s["K"].upper) == (4e7, 8e8) and (s["eps0"].lower, s["eps0"].upper) == (1e-4, 0.05)
    assert (s["n"].lower, s["n"].upper) == (0.01, 0.8)
    v = _space(Mat024Coeff({"hardening_law": "voce"}))
    assert (v["sigma_y"].lower, v["sigma_y"].upper) == pytest.approx((1.2e7, 1.2e8)) and v["Q"].upper == pytest.approx(2e8)
    assert (v["b"].lower, v["b"].upper, v["b"].scale) == (1.0, 500.0, "log")
    sv = _space(Mat024Coeff({"hardening_law": "swift_voce"}))
    assert (sv["alpha"].lower, sv["alpha"].upper) == (0.0, 1.0)
    t = _space(Mat024Table({"table_nodes": 8}))
    assert len([k for k in t if k.startswith("dsig_")]) == 7 and t["dsig_3"].upper == pytest.approx(8e7)


def test_default_ranges_without_hints():
    p = _space(Mat001({"default_E_range_pa": [1e6, 1e12]}), hints={})
    assert (p["E"].lower, p["E"].upper) == (1e6, 1e12) and p["E"].initial == 2e9  # 기준 카드 값
    p2 = _space(Mat001({}), hints={}, ref=None)
    assert p2["E"].initial == pytest.approx(1e9)  # log 중앙


def test_nodes_and_samples():
    t = Mat024Table({"table_nodes": 4, "node_spacing": "explicit", "strain_nodes": [0.0, 0.01, 0.03, 0.08]})
    assert list(t.nodes) == [0.0, 0.01, 0.03, 0.08]
    with pytest.raises(ValueError):
        Mat024Table({"node_spacing": "explicit", "strain_nodes": [0.01, 0.02]})
    e = strain_samples(0.2, 50, "geometric", 1e-4)
    assert e[0] == 0 and e[1] == pytest.approx(1e-4) and e[-1] == 0.2 and len(e) == 50
    c = Mat024Coeff({"max_strain": 0.05, "table_points": 10})
    sp = _space(c)
    th = {k: v.initial for k, v in sp.items()}
    assert c.to_neutral(th).table[-1].plastic_strain == 0.05
    d = c.describe(th)
    assert d["hardening"]["law"] == "swift" and set(d["hardening"]["coefficients"]) == {"K", "eps0", "n"}
    assert Mat024Table({}).describe(th)["hardening"]["law"] is None
