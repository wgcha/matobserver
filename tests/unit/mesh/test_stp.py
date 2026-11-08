"""M-4 STP 픽스처 3종 tet4/tet10, M-5 손상·얇은 부위, 배치·바디 매핑."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from matobserver.core import types as T
from matobserver.mesh import elements, kfile
from matobserver.mesh.errors import MeshError
from matobserver.mesh.stp import StpMesher

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "mesh"
MID = T.ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"})
OPT = {"curvature_elements": 8}  # 테스트 속도용(필렛 R1 에 2π당 8요소)


def opts(topology, h=0.0015, mins=2, sym="none", max_elements=200_000):
    return T.MeshOptions(topology=topology, target_size_m=h, min_elems_through_thickness=mins,
                         max_elements=max_elements, symmetry=sym, part_set_id=900)


def spec(name, pids, **kw):
    return T.GeometrySpec(kind="stp", dimensions={}, stp_path=FIX / name, region_part_ids=pids,
                          reference_points=(MID,), **kw)


CASES = {
    "block": ("block.stp", {"body:0": 1}, 40 * 10 * 4e-9),
    "fillet": ("fillet_block.stp", {"specimen": 1}, None),
    "two_body": ("two_body.stp", {"body:0": 21, "body:1": 22}, 40 * 10 * 4e-9),
}


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("topology", ["tet4", "tet10"])
def test_m4_stp_fixtures(tmp_path, case, topology):
    fname, pids, analytic = CASES[case]
    r = StpMesher(OPT).mesh(spec(fname, pids), opts(topology), tmp_path)
    if analytic is not None:
        assert r.reference_volume_m3 == pytest.approx(analytic, rel=1e-9)  # STEP mm → m 환산
    else:  # 필렛 R1 4개: 40×(10×4 − 4·(1 − π/4)) mm³
        assert r.reference_volume_m3 == pytest.approx(40 * (40 - 4 * (1 - np.pi / 4)) * 1e-9, rel=1e-6)
    assert abs(r.volume_m3 - r.reference_volume_m3) / r.reference_volume_m3 <= 0.01
    assert r.quality.min_scaled_jacobian >= 0.2 and r.quality.max_aspect_ratio <= 10
    assert r.bbox_m[0] == pytest.approx((-0.02, -0.005, 0.0), abs=1e-9)
    assert r.bbox_m[1] == pytest.approx((0.02, 0.005, 0.004), abs=1e-9)
    assert r.bottom_z_m == 0.0 and r.top_z_at_load_m == pytest.approx(0.004, abs=1e-9)
    kf = kfile.read_k(r.mesh_path.read_bytes())
    pids_used = {p for p, _c in kf.solids.values()}
    assert pids_used == set(pids.values())
    if case == "two_body":
        # 바디→PID: body:0 은 아래(z ≤ 2 mm), body:1 은 위
        for pid, conn in kf.solids.values():
            z = np.array([kf.nodes[n][2] for n in conn])
            assert (z <= 2.0 + 1e-9).all() if pid == 21 else (z >= 2.0 - 1e-9).all()
        assert r.regions["body:0"]["volume_m3"] == pytest.approx(8e-7, rel=0.01)
        # 계면 절점 공유(fragment 적합 메시)
        n21 = {n for p, c in kf.solids.values() if p == 21 for n in c}
        n22 = {n for p, c in kf.solids.values() if p == 22 for n in c}
        assert len(n21 & n22) > 10
    if topology == "tet10":
        X = np.array([[kf.nodes[n] for n in c] for _p, c in kf.solids.values()]) * 1e-3
        assert (elements.tet10_detj(X) > 0).all()
        if case != "fillet":  # 평면 형상: 중간절점은 변 중점
            for k, (a, b) in enumerate(elements.TET10_EDGES):
                assert np.allclose(X[:, 4 + k], 0.5 * (X[:, a] + X[:, b]), atol=1e-12)
    assert np.abs(np.array(kf.nodes[r.reference_nodes["midspan_bottom"]]) * 1e-3 - [0, 0, 0]).max() \
        <= 0.5 * r.target_size_used_m


def test_m5_corrupt_and_thin():
    m = StpMesher({})
    bad = m.validate(spec("corrupt.stp", {"body:0": 1}))
    assert [i.code for i in bad] == ["STP_UNREADABLE"]
    thin = m.validate(spec("thin.stp", {"body:0": 1}))
    assert [i.code for i in thin] == ["STP_CHECK_FAILED"] and "thin" in thin[0].message
    missing = m.validate(spec("nope.stp", {"body:0": 1}))
    assert [i.code for i in missing] == ["FILE_NOT_FOUND"]


def test_m5_mesh_raises_with_code(tmp_path):
    with pytest.raises(MeshError) as e:
        StpMesher({}).mesh(spec("corrupt.stp", {"body:0": 1}), opts("tet4"), tmp_path)
    assert e.value.code == "STP_UNREADABLE"


def test_text_file_not_step(tmp_path):
    p = tmp_path / "x.stp"
    p.write_text("hello\n")
    s = T.GeometrySpec(kind="stp", dimensions={}, stp_path=p, region_part_ids={"body:0": 1})
    assert [i.code for i in StpMesher({}).validate(s)] == ["STP_UNREADABLE"]


def test_body_slot_mismatch():
    m = StpMesher({})
    codes = [i.code for i in m.validate(spec("two_body.stp", {"body:0": 1}))]
    assert codes == ["STP_BODY_SLOT_MISMATCH"]
    codes = [i.code for i in m.validate(spec("block.stp", {"body:0": 1, "body:3": 2}))]
    assert codes == ["STP_BODY_SLOT_MISMATCH"]
    assert m.validate(spec("two_body.stp", {"*": 5})) == []


def test_symmetry_ignored_warning(tmp_path):
    r = StpMesher(OPT).mesh(spec("block.stp", {"body:0": 1}), opts("tet4", h=0.003, sym="quarter"), tmp_path)
    assert r.symmetry == "none"
    assert any(w.code == "SYMMETRY_IGNORED_STP" for w in r.warnings)
    assert "sym_x0" not in r.node_sets


def test_explicit_placement(tmp_path):
    m = StpMesher(OPT)
    # 원본 블록은 z ∈ [0, 4] mm, x ∈ [-20, 20]: z축 90° 회전 → 길이 방향이 y, translate x +0.001
    ok = spec("block.stp", {"body:0": 1}, placement={"mode": "explicit", "rotate_deg": [0, 0, 90],
                                                    "translate": [0.001, 0.0, 0.0]})
    assert m.validate(ok) == []
    r = m.mesh(ok, opts("tet4", h=0.003), tmp_path / "ok")
    assert r.bbox_m[0] == pytest.approx((-0.004, -0.02, 0.0), abs=1e-9)
    assert r.bbox_m[1] == pytest.approx((0.006, 0.02, 0.004), abs=1e-9)
    lifted = spec("block.stp", {"body:0": 1}, placement={"mode": "explicit", "translate": [0, 0, 0.001]})
    assert [i.code for i in m.validate(lifted)] == ["STP_CHECK_FAILED"]
    with pytest.raises(MeshError) as e:
        m.mesh(lifted, opts("tet4", h=0.003), tmp_path / "bad")
    assert e.value.code == "STP_CHECK_FAILED"


def test_stp_hex8_rejected(tmp_path):
    with pytest.raises(MeshError, match="not supported"):
        StpMesher({}).mesh(spec("block.stp", {"body:0": 1}), opts("hex8"), tmp_path)


def test_stp_determinism_independent_of_folder(tmp_path):
    m = StpMesher(OPT)
    a = m.mesh(spec("fillet_block.stp", {"body:0": 1}), opts("tet10", h=0.003), tmp_path / "a")
    b = m.mesh(spec("fillet_block.stp", {"body:0": 1}), opts("tet10", h=0.003), tmp_path / "b" / "c")
    assert a.mesh_path.read_bytes() == b.mesh_path.read_bytes()
