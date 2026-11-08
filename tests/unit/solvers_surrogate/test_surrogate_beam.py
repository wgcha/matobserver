"""surrogate_beam: 계약 S1–S5·S9·S11 기준 구현 검증 + 보 이론 해석해 일치 (module-boundaries.md §5.3 O-1)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

import mo_optim_doubles as D
from matobserver.core import types as T
from matobserver.core.errors import IncompatibleMaterial, IncompatibleOverride
from matobserver.solvers.surrogate.adapter import SurrogateBeamAdapter
from matobserver.solvers.surrogate.beam import Section, Uniaxial, three_point_bending
from matobserver.solvers.surrogate.kfile import part_bboxes

REPO = Path(__file__).resolve().parents[3]
W, H, L, E = 0.010, 0.004, 0.064, 2.3e9


def _case(tmp: Path, *, sym="quarter", manifest_extra=None, layers=(), stack="z", slots=("body",)) -> T.SimulationCase:
    man = D.make_manifest(slots=slots, symmetry=sym, layered_regions=bool(layers), extra=manifest_extra)
    mesher = D.BoxMesherDouble({})
    rp = {f"layer:{i}": slots.index(s) + 1 for i, (s, _) in enumerate(layers)} if layers else {"specimen": 1}
    g = T.GeometrySpec("layered_box" if layers else "box", {"width": W, "thickness": H, "total_length": 0.08},
                       layers=tuple((f"layer:{i}", t) for i, (_, t) in enumerate(layers)), stack_axis=stack,
                       region_part_ids=rp)
    mesh = mesher.mesh(g, T.MeshOptions("hex8", 0.001, 2, 10000, symmetry=sym), tmp / "mesh")
    resp = (T.ResponseDef("force_disp", "curve", "force", "displacement",
                          {"y": T.ExtractorRef("rcforc_resultant", {"contact_id": 1, "sign": -1, "scale": "symmetry"}),
                           "x": T.ExtractorRef("nodout_disp", {"node": {"ref": "indenter_ref"}, "sign": -1})},
                          1.0, T.ResponseWindow(None, None, "auto", None)),)
    eng = {"implicit": {"mo_dend": 0.004, "mo_tend": 1.0, "mo_dtout": 0.01, "mo_dtplt": 0.05, "mo_dtmax": 0.01,
                        "mo_htop": H, "mo_hbot": 0.0},
           "explicit": {"mo_dend": 0.004, "mo_tend": 0.008, "mo_dtout": 8e-5, "mo_dtplt": 4e-4, "mo_dtmax": 8e-5,
                        "mo_htop": H, "mo_hbot": 0.0}}
    sb = tuple(T.SlotBinding(s, (i + 1,), 101 + i, 101 + i, {"keyword": "SECTION_SOLID", "elform": -2}, 1101 + i)
               for i, s in enumerate(slots))
    return T.SimulationCase("t1-c1", "t1", "flex3p-test@1", tmp / "recipe", man, {"span": L}, eng, mesh, None, sb, resp,
                            "implicit", "explicit", T.OutputRequest(0.01, 0.05, 0.01, ()))


def _mat(table=None, E_=E):
    rows = None if table is None else tuple(T.TableRow(a, b) for a, b in table)
    return T.NeutralMaterial(T.ElasticBlock(E_, 0.38, 1200.0), rows)


def _snapshot(root: Path) -> dict[str, str]:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_kfile_reader_on_mesh_fixture():
    bb = part_bboxes(REPO / "tests" / "fixtures" / "mesh" / "specimen_hex8.k")
    assert bb and all(v[1] > v[0] and v[5] > v[4] for v in bb.values())


def test_s1_capabilities():
    c = SurrogateBeamAdapter({}).capabilities()
    assert c.unit_system == "mm-N-s-tonne" and {"implicit", "explicit"} <= c.analysis_modes
    assert {"rcforc_resultant", "nodout_disp", "nodout_rel_disp", "field_max"} <= c.extractors
    assert c.material_keywords == {"MAT_001", "MAT_024"} and c.supports_overrides


def test_s2_s3_workdir_and_determinism(tmp_path):
    case = _case(tmp_path)
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "manifest.yaml").write_text("x", encoding="utf-8")
    before = _snapshot(tmp_path)
    ad = SurrogateBeamAdapter({})
    p1 = ad.prepare(case, {"body": _mat()}, tmp_path / "w1")
    p2 = ad.prepare(case, {"body": _mat()}, tmp_path / "w2")
    after = _snapshot(tmp_path)
    new = set(after) - set(before)
    assert all(Path(k).is_relative_to(tmp_path / "w1") or Path(k).is_relative_to(tmp_path / "w2") for k in new)
    assert {k: v for k, v in after.items() if k in before} == before  # 레시피·메시 무수정
    assert p1.main_file.read_bytes() == p2.main_file.read_bytes()


def test_s4_states(tmp_path):
    ad = SurrogateBeamAdapter({})
    nc = D.NeverCancel()
    ok = ad.prepare(_case(tmp_path), {"body": _mat()}, tmp_path / "ok")
    assert ad.run(ok, 1, 10.0, nc).state == T.RunState.NORMAL
    err = ad.prepare(_case(tmp_path, manifest_extra={"surrogate": {"force_state": "error"}}), {"body": _mat()}, tmp_path / "e")
    assert ad.run(err, 1, 10.0, nc).state == T.RunState.ERROR
    slow = ad.prepare(_case(tmp_path, manifest_extra={"surrogate": {"simulated_runtime_s": 100.0}}), {"body": _mat()},
                      tmp_path / "t")
    st = ad.run(slow, 1, 10.0, nc)
    assert st.state == T.RunState.TIMEOUT
    part = ad.extract(slow, st).curves["force_disp"]
    assert 0 < len(part.x) < 101  # 부분 곡선
    fb = ad.prepare(_case(tmp_path, manifest_extra={"surrogate": {"fail_implicit": True}}), {"body": _mat()}, tmp_path / "f")
    st = ad.run(fb, 1, 10.0, nc)
    assert st.state == T.RunState.NORMAL and st.fallback_used and st.mode_used == "explicit"
    bad = replace(_mat(), extensions={"lsdyna.mat187": {"x": 1}})
    with pytest.raises(IncompatibleMaterial):
        ad.prepare(_case(tmp_path), {"body": bad}, tmp_path / "x")
    ov = T.RunOverrides({"body": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}}, True)
    with pytest.raises(IncompatibleOverride):
        ad.prepare(_case(tmp_path, manifest_extra={"surrogate": {"mass_scaling": True}}), {"body": _mat()}, tmp_path / "m",
                   overrides=ov)
    assert ad.prepare(_case(tmp_path), {"body": _mat()}, tmp_path / "o", overrides=ov).mode == "explicit"


@pytest.mark.parametrize("sym", ["quarter", "half", "none"])
def test_s5_extract_beam_theory(tmp_path, sym):
    ad = SurrogateBeamAdapter({})
    p = ad.prepare(_case(tmp_path, sym=sym), {"body": _mat()}, tmp_path / "w")
    st = ad.run(p, 1, 10.0, D.NeverCancel())
    c = ad.extract(p, st).curves["force_disp"]
    assert np.all(np.diff(c.x) > 0) and np.all(c.y[1:] > 0)
    ref = 48 * E * (W * H**3 / 12) * c.x / L**3  # 보 이론 기준 곡선, 전체 모델 하중(대칭 배율 적용)
    assert np.max(np.abs(c.y[1:] / ref[1:] - 1)) <= 1e-6
    assert c.x[-1] == pytest.approx(0.004)


def test_s5_layered_transformed_section(tmp_path):
    ad = SurrogateBeamAdapter({})
    layers = (("a", 0.001), ("b", 0.003))
    case = _case(tmp_path, layers=layers, slots=("a", "b"))
    Ea, Eb = 3e9, 1e9
    p = ad.prepare(case, {"a": _mat(E_=Ea), "b": _mat(E_=Eb)}, tmp_path / "w")
    c = ad.extract(p, ad.run(p, 1, 10.0, D.NeverCancel())).curves["force_disp"]
    zc = np.array([0.0005, 0.0025]); t = np.array([0.001, 0.003]); Es = np.array([Ea, Eb])
    zn = np.sum(Es * t * zc) / np.sum(Es * t)
    EI = np.sum(Es * (W * t**3 / 12 + W * t * (zc - zn) ** 2))
    assert c.y[-1] == pytest.approx(48 * EI * c.x[-1] / L**3, rel=1e-9)


def test_plastic_limits_and_field_max(tmp_path):
    sy = 50e6
    perfect = [(0.0, sy), (0.5, sy)]
    sec = Section.from_boxes([("body", -W / 2, W / 2, 0.0, H)])
    res = three_point_bending(sec, [Uniaxial.from_card(E, perfect)], L, np.linspace(0, 0.03, 60))
    Fp = 4 * (sy * W * H**2 / 4) / L  # 완전소성 붕괴 하중
    assert res.force[-1] <= Fp * (1 + 1e-9) and res.force[-1] > 0.97 * Fp
    ad = SurrogateBeamAdapter({})
    p = ad.prepare(_case(tmp_path), {"body": _mat(D.swift_table(1e8, 0.02, 0.2))}, tmp_path / "w")
    ex = ad.extract(p, ad.run(p, 1, 10.0, D.NeverCancel()))
    assert ex.field_max["body"]["eff_plastic_strain"] > 0


def test_s9_cancel(tmp_path):
    ad = SurrogateBeamAdapter({})
    tok = D.FlagCancel()
    tok.flag = True
    p = ad.prepare(_case(tmp_path), {"body": _mat()}, tmp_path / "w")
    assert ad.run(p, 1, 10.0, tok).state == T.RunState.CANCELLED


def test_s11_single_element(tmp_path):
    ad = SurrogateBeamAdapter({})
    tab = D.swift_table(6e8, 0.004, 0.22, max_strain=0.2, points=12)
    m = _mat(tab)
    c = ad.single_element_uniaxial(m, 0.1, tmp_path / "rt", 10.0)
    ep = np.array([a for a, _ in tab]); sg = np.array([b for _, b in tab])
    assert np.max(np.abs(c.y / np.interp(c.x, ep, sg) - 1)) <= 0.01
    assert c.x[0] == 0.0 and c.x[-1] == pytest.approx(0.1)
    assert len(ad.single_element_uniaxial(_mat(), 0.1, tmp_path / "rt2", 10.0).x) == 0
