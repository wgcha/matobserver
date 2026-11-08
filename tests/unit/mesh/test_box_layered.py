"""M-2 box quarter/half/none, M-3 layered_box 2·3층 stack z·y, M-6 조대화, 예약 토폴로지."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from matobserver.core import types as T
from matobserver.mesh import kfile
from matobserver.mesh.box import BoxMesher, LayeredBoxMesher
from matobserver.mesh.errors import MeshError

DIMS = {"width": 0.010, "thickness": 0.004, "total_length": 0.080}
MID = T.ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"})
FACTOR = {"none": 1, "half": 2, "quarter": 4}


def opts(topology="hex8", sym="quarter", h=0.001, mins=4, max_elements=200_000):
    return T.MeshOptions(topology=topology, target_size_m=h, min_elems_through_thickness=mins,
                         max_elements=max_elements, symmetry=sym, part_set_id=900,
                         node_set_ids={"sym_x0": 901, "sym_y0": 902})


def box_spec(**kw):
    base = dict(kind="box", dimensions=DIMS, region_part_ids={"specimen": 1}, reference_points=(MID,))
    base.update(kw)
    return T.GeometrySpec(**base)


@pytest.mark.parametrize("sym", ["quarter", "half", "none"])
@pytest.mark.parametrize("topology", ["hex8", "tet4", "tet10"])
def test_m2_box_volume_sets_reference(tmp_path, sym, topology):
    r = BoxMesher({}).mesh(box_spec(), opts(topology, sym), tmp_path)
    ref = 0.08 * 0.01 * 0.004
    assert r.reference_volume_m3 == pytest.approx(ref, rel=1e-15)
    assert abs(r.volume_m3 - ref) / ref <= 1e-9
    kf = kfile.read_k(r.mesh_path.read_bytes())
    xyz = np.array(list(kf.nodes.values())) * 1e-3
    meshed = np.prod(xyz.max(0) - xyz.min(0))
    assert meshed * FACTOR[sym] == pytest.approx(ref, rel=1e-12)
    # 대칭 세트
    assert set(r.node_sets) == {"midspan_bottom"} | {f"sym_{p}" for p in {"none": (), "half": ("y0",),
                                                                            "quarter": ("x0", "y0")}[sym]}
    # 참조점은 정확히 (0, 0, 0)
    assert np.allclose(np.array(kf.nodes[r.reference_nodes["midspan_bottom"]]), 0.0, atol=0)
    assert r.top_z_at_load_m == pytest.approx(0.004, abs=1e-15) and r.bottom_z_m == 0.0
    assert r.quality.elems_through_thickness == 4
    assert kfile.solid_topology(next(iter(kf.solids.values()))[1]) == topology
    assert kf.part_sets[900] == (1,)
    res = json.loads((tmp_path / "result.json").read_text())
    assert res["specimen"]["element_count"] == r.element_count


def test_box_tet10_midside_nodes_on_edges(tmp_path):
    r = BoxMesher({}).mesh(box_spec(), opts("tet10", h=0.002), tmp_path)
    kf = kfile.read_k(r.mesh_path.read_bytes())
    for _pid, conn in list(kf.solids.values())[:200]:
        X = np.array([kf.nodes[n] for n in conn])
        for k, (a, b) in enumerate([(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]):
            assert np.allclose(X[4 + k], 0.5 * (X[a] + X[b]), atol=1e-9)
        v = np.dot(X[1] - X[0], np.cross(X[2] - X[0], X[3] - X[0]))
        assert v > 0


def test_box_thin_specimen_through_thickness(tmp_path):
    spec = box_spec(dimensions={"width": 0.01, "thickness": 0.0005, "total_length": 0.04})
    r = BoxMesher({}).mesh(spec, opts("hex8", h=0.001, mins=4), tmp_path)
    assert r.quality.elems_through_thickness == 4
    assert r.quality.max_aspect_ratio <= 10.0  # 면내 크기 상한(aspect cap)
    assert not r.warnings


def test_m6_box_coarsen(tmp_path):
    r = BoxMesher({}).mesh(box_spec(), opts("hex8", h=0.0005, max_elements=1000), tmp_path)
    assert r.element_count <= 1000
    assert r.target_size_used_m > 0.0005
    w = [w for w in r.warnings if w.code == "MESH_COARSENED"]
    assert w and w[0].context["max_elements"] == 1000


def test_coarsen_impossible_raises(tmp_path):
    with pytest.raises(MeshError):
        BoxMesher({}).mesh(box_spec(), opts("hex8", mins=8, max_elements=5), tmp_path)


@pytest.mark.parametrize("topo", ["quad4_axisym", "shell4"])
def test_reserved_topologies_rejected(tmp_path, topo):
    o = dataclasses.replace(opts(), topology=topo)
    with pytest.raises(MeshError, match="reserved"):
        BoxMesher({}).mesh(box_spec(), o, tmp_path)


def test_box_validate_issues():
    m = BoxMesher({})
    codes = {i.code for i in m.validate(box_spec(dimensions={"width": 0.01, "thickness": 0}, region_part_ids={}))}
    assert {"GEOMETRY_DIMENSION_MISSING", "GEOMETRY_DIMENSION_INVALID", "SLOT_UNASSIGNED"} <= codes
    assert m.validate(box_spec(region_part_ids={"*": 3})) == []


def test_missing_sym_set_ids_use_fallback(tmp_path):
    o = dataclasses.replace(opts(), node_set_ids={})
    r = BoxMesher({}).mesh(box_spec(), o, tmp_path)
    assert r.node_sets["sym_x0"] == 4_899_001 and r.node_sets["sym_y0"] == 4_899_002


# ------------------------------------------------------------------ layered_box (M-3)
def layered(layers, axis, pids=None):
    pids = pids or {n: 10 + i for i, (n, _t) in enumerate(layers)}
    return T.GeometrySpec(kind="layered_box", dimensions=DIMS, layers=tuple(layers), stack_axis=axis,
                          region_part_ids=pids, reference_points=(MID,))


TWO = [("layer:0", 0.001), ("layer:1", 0.003)]
THREE = [("layer:0", 0.0005), ("layer:1", 0.003), ("layer:2", 0.0005)]


@pytest.mark.parametrize("layers", [TWO, THREE], ids=["2layer", "3layer"])
@pytest.mark.parametrize("axis, sym", [("z", "quarter"), ("z", "none"), ("y", "none")])
@pytest.mark.parametrize("topology", ["hex8", "tet10"])
def test_m3_layered(tmp_path, layers, axis, sym, topology):
    spec = layered(layers, axis)
    r = LayeredBoxMesher({}).mesh(spec, opts(topology, sym, h=0.001), tmp_path)
    kf = kfile.read_k(r.mesh_path.read_bytes())
    L, W = DIMS["total_length"], DIMS["width"]
    for i, (name, t) in enumerate(layers):
        reg = r.regions[name]
        assert reg["part_id"] == 10 + i
        assert abs(reg["volume_m3"] - L * W * t) / (L * W * t) <= 1e-9
        # 그 층 요소는 그 층 범위 안
        ax = 2 if axis == "z" else 1
        lo = sum(tt for _n, tt in layers[:i]) - (0.5 * sum(tt for _n, tt in layers) if axis == "y" else 0.0)
        hi = lo + t
        for pid, conn in kf.solids.values():
            if pid == 10 + i:
                c = np.array([kf.nodes[n][ax] for n in conn]) * 1e-3
                assert c.min() >= lo - 1e-12 and c.max() <= hi + 1e-12
                break
    assert sum(v["volume_m3"] for v in r.regions.values()) == pytest.approx(r.volume_m3, rel=1e-12)
    assert abs(r.volume_m3 - r.reference_volume_m3) / r.reference_volume_m3 <= 1e-9
    xyz = np.array(list(kf.nodes.values())) * 1e-3
    if axis == "y":  # 측면 배치: z 범위 = 폭, y 범위 = 적층 두께
        assert xyz[:, 2].max() == pytest.approx(W) and np.ptp(xyz[:, 1]) == pytest.approx(0.004)
        assert r.top_z_at_load_m == pytest.approx(W)
    else:
        assert r.top_z_at_load_m == pytest.approx(0.004)
    assert r.quality.max_aspect_ratio <= 10.0 and r.quality.min_scaled_jacobian >= 0.2


def test_layered_y_with_symmetry_rejected(tmp_path):
    with pytest.raises(MeshError, match="stack_axis"):
        LayeredBoxMesher({}).mesh(layered(TWO, "y"), opts("hex8", "half"), tmp_path)


def test_layered_validate():
    m = LayeredBoxMesher({})
    bad = layered([("layer:0", 0.001), ("layer:0", -1.0)], "z", pids={"layer:0": 1})
    codes = [i.code for i in m.validate(bad)]
    assert "GEOMETRY_DIMENSION_INVALID" in codes
    mismatch = layered([("layer:0", 0.001), ("layer:1", 0.002)], "z")  # 합 3 mm ≠ thickness 4 mm
    assert any("sum of layer" in i.message for i in m.validate(mismatch))
    nopid = layered(TWO, "z", pids={"layer:0": 1})
    assert [i.code for i in m.validate(nopid)] == ["SLOT_UNASSIGNED"]
    assert m.validate(layered(TWO, "z")) == []
