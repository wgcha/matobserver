"""M-7 지그 롤러: span·R 변화 시 위치·초기 간격 0, 참조 노드, *MAT_RIGID 값, 법선."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from matobserver.core import types as T
from matobserver.mesh import kfile
from matobserver.mesh.box import BoxMesher
from matobserver.mesh.errors import MeshError
from matobserver.mesh.jig import Bending3pRollersJig

DIMS = {"width": 0.010, "thickness": 0.004, "total_length": 0.080}
T_SHELL = 1e-4


def specimen(tmp, sym, thickness=0.004):
    spec = T.GeometrySpec(kind="box", dimensions={**DIMS, "thickness": thickness}, region_part_ids={"specimen": 1})
    o = T.MeshOptions(topology="hex8", target_size_m=0.001, min_elems_through_thickness=4, max_elements=10**6,
                      symmetry=sym, node_set_ids={"sym_x0": 901, "sym_y0": 902})
    return BoxMesher({}).mesh(spec, o, tmp / f"spec-{sym}-{thickness}")


def jig(sm, sym, span=0.064, ri=0.005, rs=0.005, rigid=None, **params):
    return T.JigSpec(generator="jig.bending3p_rollers",
                     params_si={"span": span, "indenter_radius": ri, "support_radius": rs, **params},
                     part_ids={"indenter": 1001, "support": 1002}, mids={"indenter": 2001, "support": 2002},
                     rigid=rigid if rigid is not None else {}, specimen=sm, symmetry=sym)


def nodes_of(kf, pid):
    ids = sorted({n for p, c in kf.shells.values() if p == pid for n in c})
    return ids, np.array([kf.nodes[n] for n in ids]) * 1e-3


@pytest.mark.parametrize("sym", ["quarter", "half", "none"])
@pytest.mark.parametrize("span, ri, rs, h", [(0.064, 0.005, 0.005, 0.004), (0.050, 0.003, 0.002, 0.004),
                                              (0.064, 0.005, 0.005, 0.0025)])
def test_m7_positions(tmp_path, sym, span, ri, rs, h):
    sm = specimen(tmp_path, sym, h)
    r = Bending3pRollersJig({}).mesh(jig(sm, sym, span, ri, rs), None, tmp_path / "jig")
    kf = kfile.read_k(r.mesh_path.read_bytes(), "jig")
    top = sm.top_z_at_load_m
    assert top == pytest.approx(h, abs=1e-15)
    off = 0.5 * T_SHELL
    # 압자: 축 (x=0, z=top+R+t/2), 중면 반지름 R, 바깥면 최저 = top (초기 간격 0)
    _ids, P = nodes_of(kf, 1001)
    rad = np.hypot(P[:, 0], P[:, 2] - (top + ri + off))
    assert np.abs(rad - ri).max() <= 1e-9
    assert abs((P[:, 2].min() - off) - top) <= 1e-9
    # 지지: 축 (±span/2, z=-(R+t/2)), 바깥면 최고 = 0
    _ids, S = nodes_of(kf, 1002)
    xc = np.where(S[:, 0] > 0, span / 2, -span / 2)
    rad = np.hypot(S[:, 0] - xc, S[:, 2] + rs + off)
    assert np.abs(rad - rs).max() <= 1e-9
    assert abs(S[:, 2].max() + off) <= 1e-9
    n_sup = 1 if sym == "quarter" else 2
    assert len(set(np.round(xc, 9))) == n_sup
    # 참조 노드
    ind = np.array(kf.nodes[r.reference_nodes["indenter_ref"]]) * 1e-3
    sup = np.array(kf.nodes[r.reference_nodes["support_top"]]) * 1e-3
    assert np.abs(ind - [0.0, 0.0, top + off]).max() <= 1e-9
    assert np.abs(sup - [span / 2, 0.0, -off]).max() <= 1e-9
    assert r.reference_nodes["indenter_ref"] in {n for p, c in kf.shells.values() if p == 1001 for n in c}
    assert r.reference_nodes["support_top"] in {n for p, c in kf.shells.values() if p == 1002 for n in c}
    # 대칭 영역만
    if sym == "quarter":
        assert (P[:, 0] >= -1e-12).all() and (S[:, 0] > 0).all()
    if sym in ("quarter", "half"):
        assert (np.vstack([P, S])[:, 1] >= -1e-12).all()
        assert np.vstack([P, S])[:, 1].max() == pytest.approx(0.5 * (DIMS["width"] + 2 * 0.001))
    else:
        assert np.vstack([P, S])[:, 1].max() == pytest.approx(0.5 * (DIMS["width"] + 2 * 0.001))
        assert np.vstack([P, S])[:, 1].min() == pytest.approx(-0.5 * (DIMS["width"] + 2 * 0.001))
    assert min(kf.nodes) >= 5_000_000 and max(kf.shells) <= 5_999_999


def test_m7_rigid_values_from_spec_and_defaults(tmp_path):
    sm = specimen(tmp_path, "quarter")
    r = Bending3pRollersJig({}).mesh(jig(sm, "quarter"), None, tmp_path / "d")
    kf = kfile.read_k(r.mesh_path.read_bytes(), "jig")
    ro, E, pr = kf.rigid_mats[2001]
    assert ro == pytest.approx(7850 * 1e-12) and E == pytest.approx(2.1e11 * 1e-6) and pr == pytest.approx(0.3)
    assert kf.parts[1001] == ("matobserver jig indenter", 1001, 2001)
    assert kf.sections[1002] == (2, pytest.approx(T_SHELL * 1e3))
    custom = {"E": 7.0e10, "poisson": 0.33, "density": 2700.0}
    r2 = Bending3pRollersJig({}).mesh(jig(sm, "quarter", rigid=custom), None, tmp_path / "c")
    kf2 = kfile.read_k(r2.mesh_path.read_bytes(), "jig")
    assert kf2.rigid_mats[2002] == (pytest.approx(2.7e-9), pytest.approx(7.0e4), pytest.approx(0.33))
    r3 = Bending3pRollersJig({"rigid_E_pa": 2.0e11}).mesh(jig(sm, "quarter"), None, tmp_path / "o")
    assert kfile.read_k(r3.mesh_path.read_bytes(), "jig").rigid_mats[2001][1] == pytest.approx(2.0e5)


def test_shell_normals_point_outward(tmp_path):
    sm = specimen(tmp_path, "none")
    r = Bending3pRollersJig({}).mesh(jig(sm, "none"), None, tmp_path / "j")
    kf = kfile.read_k(r.mesh_path.read_bytes(), "jig")
    top = sm.top_z_at_load_m
    for pid, (ax, az) in ((1001, (0.0, top + 0.005 + 5e-5)),):
        for p, conn in kf.shells.values():
            if p != pid:
                continue
            X = np.array([kf.nodes[n] for n in conn]) * 1e-3
            n = np.cross(X[1] - X[0], X[3] - X[0])
            c = X.mean(axis=0)
            radial = np.array([c[0] - ax, 0.0, c[2] - az])
            assert np.dot(n, radial) > 0


def test_roller_length_explicit_and_alias(tmp_path):
    sm = specimen(tmp_path, "quarter")
    r = Bending3pRollersJig({}).mesh(
        T.JigSpec(generator="jig.bending3p_rollers",
                  params_si={"span": 0.064, "indenter_r": 0.005, "support_r": 0.005, "jig_len": 0.03},
                  part_ids={"indenter": 1, "support": 2}, mids={"indenter": 1, "support": 2}, rigid={},
                  specimen=sm, symmetry="quarter"), None, tmp_path / "j")
    kf = kfile.read_k(r.mesh_path.read_bytes(), "jig")
    y = np.array([v[1] for v in kf.nodes.values()]) * 1e-3
    assert y.max() == pytest.approx(0.015)


def test_contact_offset_off(tmp_path):
    sm = specimen(tmp_path, "quarter")
    r = Bending3pRollersJig({"contact_offset": False}).mesh(jig(sm, "quarter"), None, tmp_path / "j")
    kf = kfile.read_k(r.mesh_path.read_bytes(), "jig")
    assert np.array(kf.nodes[r.reference_nodes["indenter_ref"]])[2] * 1e-3 == pytest.approx(0.004, abs=1e-12)
    assert np.array(kf.nodes[r.reference_nodes["support_top"]])[2] == 0.0


def test_jig_validate():
    m = Bending3pRollersJig({})
    codes = [i.code for i in m.validate(T.JigSpec(generator="x", params_si={}, part_ids={}, mids={}, rigid={},
                                                   specimen=None, symmetry="quarter"))]  # type: ignore[arg-type]
    assert "GEOMETRY_KIND_NOT_ALLOWED" in codes and codes.count("GEOMETRY_DIMENSION_MISSING") >= 3


def test_jig_validate_geometry(tmp_path):
    sm = specimen(tmp_path, "quarter")
    m = Bending3pRollersJig({})
    assert m.validate(jig(sm, "quarter")) == []
    assert any("half length" in i.message for i in m.validate(jig(sm, "quarter", span=0.09)))
    assert any("overlap" in i.message for i in m.validate(jig(sm, "quarter", span=0.012, ri=0.005, rs=0.005)))
    assert any("symmetry" in (i.field or "") for i in m.validate(jig(sm, "none")))
    assert m.validate(jig(sm, "quarter", rigid={"E": -1.0}))
    with pytest.raises(MeshError):
        m.mesh(jig(sm, "quarter", span=-1.0), None, tmp_path / "bad")


def test_jig_determinism(tmp_path):
    sm = specimen(tmp_path, "quarter")
    a = Bending3pRollersJig({}).mesh(jig(sm, "quarter"), None, tmp_path / "a")
    b = Bending3pRollersJig({}).mesh(jig(dataclasses.replace(sm), "quarter"), None, tmp_path / "b")
    assert a.mesh_path.read_bytes() == b.mesh_path.read_bytes()
    assert math.isfinite(sm.top_z_at_load_m)
