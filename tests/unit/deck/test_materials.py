"""deck.materials — 재료 키워드 writer·reader (S-3 MatNexus dp600 왕복, 계약 S6/S8 보조)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from matobserver.core.errors import IncompatibleMaterial
from matobserver.core.types import ElasticBlock, NeutralMaterial, SlotBinding, TableRow
from matobserver.deck import kfile
from matobserver.deck.materials import (
    card_text,
    mat_rigid_lines,
    material_keyword,
    read_materials,
    slot_file_text,
)

DP600 = Path(__file__).resolve().parents[2] / "fixtures" / "solver" / "dp600-1elem-lsdyna.k"
REL = 1e-6


def _rel(a, b):
    return abs(a - b) / abs(b) if b else abs(a)


def _assert_same(m1: NeutralMaterial, m2: NeutralMaterial, tol=REL):
    for f in ("youngs_modulus", "poisson_ratio", "density"):
        assert _rel(getattr(m2.elastic, f), getattr(m1.elastic, f)) <= tol, f
    if m1.table is None:
        assert m2.table is None
        return
    assert len(m1.table) == len(m2.table)
    for r1, r2 in zip(m1.table, m2.table):
        assert abs(r2.plastic_strain - r1.plastic_strain) <= tol * max(1.0, r1.plastic_strain)
        assert _rel(r2.true_stress, r1.true_stress) <= tol


def test_s3_dp600_mat024_values_reproduced():
    """S-3: MatNexus dp600-1elem-lsdyna.k 사본의 *MAT_024 값 → NeutralMaterial → writer → reader, 상대오차 ≤1e-6."""
    parsed = read_materials(DP600.read_text())
    src = parsed[1]
    assert src.keyword == "MAT_024"
    m = src.material
    assert m.elastic.youngs_modulus == pytest.approx(210e9, rel=1e-12)
    assert m.elastic.poisson_ratio == 0.3
    assert m.elastic.density == pytest.approx(7850.0, rel=1e-12)
    expected = [(0.0, 350e6), (0.02, 455e6), (0.1, 560e6), (0.4, 700e6), (1.0, 790e6)]
    assert [r.plastic_strain for r in m.table] == pytest.approx([e for e, _ in expected], rel=1e-12)
    assert [r.true_stress for r in m.table] == pytest.approx([s for _, s in expected], rel=1e-12)
    again = read_materials(card_text(m, 1, 100, slot="dp600"))[1]
    assert again.raw["lcss"] == 100 and again.raw["e"] == src.raw["e"] and again.raw["ro"] == src.raw["ro"]
    _assert_same(m, again.material)
    # SIGY 는 표 첫 점(항복점)으로 쓴다 (원본 덱은 0, LCSS 가 우선)
    assert again.raw["sigy"] == 350.0
    assert again.material.extensions == {"lsdyna.mat024": {"vp": 1.0}}


@pytest.mark.parametrize("seed", range(5))
def test_random_roundtrip_including_awkward_density(seed):
    rng = np.random.default_rng(seed)
    E = float(10 ** rng.uniform(6, 12))
    nu = float(rng.uniform(0.0, 0.49))
    rho = float(rng.uniform(800, 9000))
    eps = np.concatenate([[0.0], np.sort(rng.uniform(1e-4, 0.5, 6))])
    sig = np.cumsum(rng.uniform(1e5, 5e7, 7)) + 1e6
    m = NeutralMaterial(ElasticBlock(E, nu, rho), tuple(TableRow(float(a), float(b)) for a, b in zip(eps, sig)))
    b = SlotBinding("body", (1, 2), 11, 12, {"keyword": "SECTION_SOLID", "elform": -2}, 13)
    _assert_same(m, read_materials(slot_file_text(b, m))[11].material)
    el = NeutralMaterial(ElasticBlock(E, nu, rho))
    _assert_same(el, read_materials(slot_file_text(b, el))[11].material)


def test_units_pa_to_mpa_and_kg_m3_to_tonne_mm3():
    m = NeutralMaterial(ElasticBlock(2.0e9, 0.38, 1200.0))
    raw = read_materials(card_text(m, 5, None, slot="x"))[5].raw
    assert raw["e"] == 2000.0 and raw["ro"] == pytest.approx(1.2e-9, rel=1e-15) and raw["pr"] == 0.38


def test_long_format_used_only_when_needed():
    nice = NeutralMaterial(ElasticBlock(2.0e9, 0.38, 1200.0))
    awkward = NeutralMaterial(ElasticBlock(2.0e9, 0.38, 1234.56789))
    assert "*MAT_ELASTIC\n" in card_text(nice, 1, None, slot="a").replace("_TITLE", "")
    t = card_text(awkward, 1, None, slot="a")
    assert "*MAT_ELASTIC_TITLE +" in t
    assert read_materials(t)[1].material.elastic.density == pytest.approx(1234.56789, rel=1e-12)
    assert "+" not in card_text(awkward, 1, None, slot="a", long_format="never").split("\n")[3]


def test_keyword_choice_and_extensions():
    el = NeutralMaterial(ElasticBlock(1e9, 0.3, 1000.0))
    pl = NeutralMaterial(ElasticBlock(1e9, 0.3, 1000.0), (TableRow(0.0, 1e7), TableRow(0.1, 2e7)))
    assert material_keyword(el) == "MAT_001" and material_keyword(pl) == "MAT_024"
    with pytest.raises(IncompatibleMaterial):
        card_text(NeutralMaterial(el.elastic, extensions={"lsdyna.mat187": {"x": 1}}), 1, None, slot="a")
    with pytest.raises(IncompatibleMaterial):
        card_text(NeutralMaterial(pl.elastic, pl.table, extensions={"lsdyna.mat024": {"bogus": 1}}), 1, 2, slot="a")
    with pytest.raises(IncompatibleMaterial):
        card_text(pl, 1, None, slot="a")  # MAT_024 needs lcid
    t = card_text(NeutralMaterial(pl.elastic, pl.table, extensions={"lsdyna.mat024": {"fail": 0.3}}), 1, 2, slot="a")
    assert read_materials(t)[1].material.extensions == {"lsdyna.mat024": {"fail": 0.3}}


def test_slot_file_parts_section_and_hourglass_override():
    pl = NeutralMaterial(ElasticBlock(1e9, 0.3, 1000.0), (TableRow(0.0, 1e7), TableRow(0.1, 2e7)))
    b = SlotBinding("core", (3, 4), 21, 22, {"keyword": "SECTION_SOLID", "elform": -2}, 23)
    plain = kfile.parse_k(slot_file_text(b, pl))
    parts = [blk for blk in plain if blk.name == "PART"]
    assert [kfile.split_fields(p.lines[1])[:5] for p in parts] == [["3", "22", "21", "0", "0"], ["4", "22", "21", "0", "0"]]
    assert kfile.split_fields(next(blk for blk in plain if blk.name == "SECTION_SOLID").lines[0])[:2] == ["22", "-2"]
    assert not any(blk.name == "HOURGLASS" for blk in plain)
    ov = kfile.parse_k(slot_file_text(b, pl, section_override={"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}))
    assert kfile.split_fields(next(blk for blk in ov if blk.name == "SECTION_SOLID").lines[0])[:2] == ["22", "1"]
    hg = next(blk for blk in ov if blk.name == "HOURGLASS")
    assert kfile.split_fields(hg.lines[0])[:3] == ["22", "6", "0.1"]
    assert all(kfile.split_fields(p.lines[1])[4] == "22" for p in ov if p.name == "PART")
    with pytest.raises(IncompatibleMaterial):
        slot_file_text(SlotBinding("s", (1,), 1, 1, {"keyword": "SECTION_SHELL"}, None), NeutralMaterial(pl.elastic))


def test_mat_rigid_roundtrip():
    text = "\n".join(["*KEYWORD", *mat_rigid_lines(1001, 2.1e11, 0.3, 7850.0), "*END"])
    r = read_materials(text)[1001]
    assert r.keyword == "MAT_020" and r.raw == {"ro": pytest.approx(7.85e-9), "e": 210000.0, "pr": 0.3}
