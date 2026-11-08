"""GeometryMesher 계약 테스트 G1–G11 (plugins.md §5.4). 소유: Impl-Mesh.

레지스트리 `geometry_mesher` 의 enabled 항목마다 매개변수화한다(conftest `plugin_name`).
항목별 대표 케이스(_CASES)를 돌리며, 해당 플러그인에 의미 없는 항목(예: 지그의 체적)은 skip 사유를 남긴다.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from matobserver.core import types as T
from matobserver.mesh import kfile

PLUGIN_TYPE = "geometry_mesher"

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "mesh"
REF = (T.ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"}),
       T.ReferencePointRequest("top_end", {"x": "max", "y": 0.0, "z": "max"}))
DIMS = {"width": 0.010, "thickness": 0.004, "total_length": 0.080}
SET_IDS = {"sym_x0": 901, "sym_y0": 902}


def _opts(topology="hex8", h=0.001, sym="quarter", max_elements=200_000, mins=4) -> T.MeshOptions:
    return T.MeshOptions(topology=topology, target_size_m=h, min_elems_through_thickness=mins,
                         max_elements=max_elements, symmetry=sym, part_set_id=900, node_set_ids=SET_IDS)


def _specimen_cases(name: str) -> list[tuple[str, T.GeometrySpec, T.MeshOptions]]:
    if name == "box":
        spec = T.GeometrySpec(kind="box", dimensions=DIMS, region_part_ids={"specimen": 1}, reference_points=REF)
        return [(f"{s}-{t}", spec, _opts(t, sym=s)) for s, t in
                (("quarter", "hex8"), ("half", "tet4"), ("none", "tet10"))]
    if name == "layered_box":
        z = T.GeometrySpec(kind="layered_box", dimensions=DIMS, layers=(("layer:0", 0.001), ("layer:1", 0.003)),
                           stack_axis="z", region_part_ids={"layer:0": 11, "layer:1": 12}, reference_points=REF)
        y = T.GeometrySpec(kind="layered_box", dimensions=DIMS,
                           layers=(("layer:0", 0.001), ("layer:1", 0.002), ("layer:2", 0.001)), stack_axis="y",
                           region_part_ids={"layer:0": 11, "layer:1": 12, "layer:2": 13}, reference_points=REF)
        return [("z-quarter-hex8", z, _opts("hex8", sym="quarter")),
                ("y-none-tet4", y, _opts("tet4", h=0.0015, sym="none"))]
    if name == "stp":
        one = T.GeometrySpec(kind="stp", dimensions={}, stp_path=FIX / "block.stp",
                             region_part_ids={"body:0": 1}, reference_points=REF)
        two = T.GeometrySpec(kind="stp", dimensions={}, stp_path=FIX / "two_body.stp",
                             region_part_ids={"body:0": 21, "body:1": 22}, reference_points=REF)
        return [("block-tet10", one, _opts("tet10", h=0.002, sym="none", mins=2)),
                ("two_body-tet4", two, _opts("tet4", h=0.002, sym="none", mins=2))]
    return []


def _is_jig(make_plugin) -> bool:
    return make_plugin().role == "jig"


def _jig_cases(registry, tmp: Path) -> list[tuple[str, T.JigSpec, None]]:
    box = registry.create(PLUGIN_TYPE, "box")
    out = []
    for sym in ("quarter", "none"):
        spec = T.GeometrySpec(kind="box", dimensions=DIMS, region_part_ids={"specimen": 1}, reference_points=REF)
        sm = box.mesh(spec, _opts("hex8", sym=sym), tmp / f"jigspec-{sym}")
        js = T.JigSpec(generator="jig.bending3p_rollers",
                       params_si={"span": 0.064, "indenter_radius": 0.005, "support_radius": 0.005},
                       part_ids={"indenter": 1001, "support": 1002}, mids={"indenter": 1001, "support": 1002},
                       rigid={"E": 2.1e11, "poisson": 0.3, "density": 7850.0}, specimen=sm, symmetry=sym)
        out.append((f"jig-{sym}", js, None))
    return out


@pytest.fixture
def cases(plugin_name, make_plugin, registry, tmp_path):
    if _is_jig(make_plugin):
        return _jig_cases(registry, tmp_path)
    c = _specimen_cases(plugin_name)
    assert c, f"no contract cases defined for {plugin_name}"
    return c


@pytest.fixture
def results(cases, make_plugin, tmp_path):
    plugin = make_plugin()
    return [(label, spec, opts, plugin.mesh(spec, opts, tmp_path / label)) for label, spec, opts in cases]


def _k(result, jig: bool):
    return kfile.read_k(result.mesh_path.read_bytes(), "jig" if jig else "specimen")


def _nodes_m(kf) -> tuple[np.ndarray, np.ndarray]:
    ids = np.array(sorted(kf.nodes))
    xyz = np.array([kf.nodes[i] for i in ids]) * 1e-3
    return ids, xyz


# ---------------------------------------------------------------------------------- G1
def test_g1_volume(plugin_name, make_plugin, results, config):
    if _is_jig(make_plugin):
        pytest.skip("jig: rigid shell, no volume")
    tol = config.get("mesh.volume_rel_tol_stp") if plugin_name == "stp" else config.get("mesh.volume_rel_tol_parametric")
    for label, _s, _o, r in results:
        rel = abs(r.volume_m3 - r.reference_volume_m3) / r.reference_volume_m3
        assert rel <= tol, (label, rel)


# ---------------------------------------------------------------------------------- G2
def test_g2_quality(make_plugin, results, config):
    if _is_jig(make_plugin):
        pytest.skip("jig: rigid shell, quality not applicable")
    for label, _s, opts, r in results:
        assert r.quality.min_scaled_jacobian >= config.get("mesh.min_scaled_jacobian"), label
        assert r.quality.max_aspect_ratio <= config.get("mesh.max_aspect_ratio"), label
        if r.quality.elems_through_thickness is not None:
            assert r.quality.elems_through_thickness >= opts.min_elems_through_thickness, label
        assert not any(w.code == "MESH_QUALITY_LOW" for w in r.warnings), label


# ---------------------------------------------------------------------------------- G3
def test_g3_part_ids(make_plugin, results):
    jig = _is_jig(make_plugin)
    for label, spec, _o, r in results:
        kf = _k(r, jig)
        elems = kf.shells if jig else kf.solids
        pids = [p for p, _c in elems.values()]
        if jig:
            assert set(pids) == set(spec.part_ids.values()), label
            assert dict(r.part_ids) == {"indenter": spec.part_ids["indenter"], "support": spec.part_ids["support"]}
            continue
        expected = set(spec.region_part_ids.values())
        assert set(pids) == expected, label
        assert sum(int(v["element_count"]) for v in r.regions.values()) == len(elems) == r.element_count
        for region, info in r.regions.items():
            assert info["part_id"] == spec.region_part_ids[region], (label, region)
            assert pids.count(int(info["part_id"])) == info["element_count"], (label, region)


# ---------------------------------------------------------------------------------- G4
def test_g4_reference_nodes(make_plugin, results):
    jig = _is_jig(make_plugin)
    for label, spec, _o, r in results:
        kf = _k(r, jig)
        ids, xyz = _nodes_m(kf)
        if jig:
            assert set(r.reference_nodes) >= {"indenter_ref", "support_top"}, label
            used = {n for _p, c in kf.shells.values() for n in c}
            for n in r.reference_nodes.values():
                assert n in used, (label, "reference node must belong to a rigid part")
            continue
        lo, hi = xyz.min(axis=0), xyz.max(axis=0)
        for rp in spec.reference_points:
            nid = r.reference_nodes[rp.name]
            p = np.array(kf.nodes[nid]) * 1e-3
            for ax, key in enumerate("xyz"):
                v = rp.locate[key]
                tgt = lo[ax] if v == "min" else hi[ax] if v == "max" else float(v)
                assert abs(p[ax] - tgt) <= 0.5 * r.target_size_used_m + 1e-12, (label, rp.name, key)
            assert r.node_sets[rp.name] >= 4_900_001
            assert kf.node_sets[r.node_sets[rp.name]][1] == (nid,)


# ---------------------------------------------------------------------------------- G5
def test_g5_reparse(make_plugin, results):
    jig = _is_jig(make_plugin)
    for label, _s, _o, r in results:
        raw = r.mesh_path.read_bytes()
        kf = _k(r, jig)  # 허용 키워드·고정 폭·머리 주석 검사 포함
        hf = kf.header_fields()
        assert hf["units"] == "mm-N-s-tonne"
        assert hf["generator"] == make_plugin().name
        assert raw.startswith(b"*KEYWORD\n") and raw.endswith(b"*END\n")
        if not jig:
            _ids, xyz = _nodes_m(kf)
            assert np.allclose(xyz.min(axis=0), r.bbox_m[0], atol=1e-12, rtol=0)
            assert np.allclose(xyz.max(axis=0), r.bbox_m[1], atol=1e-12, rtol=0)
            assert hf["topology"] == _o.topology
            assert kf.keywords.count("*ELEMENT_SOLID") == 1
            forbidden = {"*PART", "*MAT_RIGID", "*SECTION_SHELL", "*ELEMENT_SHELL"}
            assert not forbidden & set(kf.keywords)
        else:
            assert {"*NODE", "*ELEMENT_SHELL", "*PART", "*SECTION_SHELL", "*MAT_RIGID"} <= set(kf.keywords)


# ---------------------------------------------------------------------------------- G6
def test_g6_id_ranges(make_plugin, results):
    jig = _is_jig(make_plugin)
    for label, spec, opts, r in results:
        kf = _k(r, jig)
        nr = spec.node_id_range if jig else opts.node_id_range
        er = spec.element_id_range if jig else opts.element_id_range
        elems = kf.shells if jig else kf.solids
        assert nr[0] <= min(kf.nodes) and max(kf.nodes) <= nr[1], label
        assert er[0] <= min(elems) and max(elems) <= er[1], label


# ---------------------------------------------------------------------------------- G7
def test_g7_symmetry_sets(make_plugin, results):
    jig = _is_jig(make_plugin)
    checked = 0
    for label, spec, opts, r in results:
        kf = _k(r, jig)
        ids, xyz = _nodes_m(kf)
        sym = spec.symmetry if jig else opts.symmetry
        if jig:
            if sym == "quarter":
                assert (xyz[:, 0] >= -1e-12).all() and (xyz[:, 1] >= -1e-12).all(), label
            elif sym == "half":
                assert (xyz[:, 1] >= -1e-12).all(), label
            checked += 1
            continue
        planes = {"none": (), "half": ("y0",), "quarter": ("x0", "y0")}[r.symmetry]
        for plane in planes:
            sid = opts.node_set_ids[f"sym_{plane}"]
            assert r.node_sets[f"sym_{plane}"] == sid
            members = np.array(kf.node_sets[sid][1])
            ax = 0 if plane == "x0" else 1
            coord = np.array([kf.nodes[n][ax] for n in members]) * 1e-3
            assert (np.abs(coord) <= 1e-9).all(), (label, plane)
            on_plane = set(ids[np.abs(xyz[:, ax]) <= 1e-9].tolist())
            assert on_plane == set(members.tolist()), (label, plane)
            checked += 1
        if r.symmetry != "none":
            assert (xyz[:, 1] >= 0).all(), label
        if r.symmetry == "quarter":
            assert (xyz[:, 0] >= 0).all(), label
    if not checked:
        pytest.skip("no symmetric case for this plugin (STP is meshed without symmetry, recipe.md §4.3)")


# ---------------------------------------------------------------------------------- G8
def test_g8_determinism(make_plugin, cases, results, tmp_path):
    plugin = make_plugin()
    for (label, spec, opts), (_l, _s, _o, r) in zip(cases, results):
        again = plugin.mesh(spec, opts, tmp_path / "again" / label)
        assert again.mesh_path.read_bytes() == r.mesh_path.read_bytes(), label


# ---------------------------------------------------------------------------------- G9
def test_g9_invalid_spec_issues(plugin_name, make_plugin, cases):
    plugin = make_plugin()
    bad: list = []
    _label, spec, _opts0 = cases[0]
    if plugin.role == "jig":
        bad.append(T.JigSpec(generator=spec.generator, params_si={"span": -1.0}, part_ids={}, mids={}, rigid={},
                             specimen=spec.specimen, symmetry="quarter"))
        bad.append(T.GeometrySpec(kind="box", dimensions=DIMS))
    else:
        bad.append(T.GeometrySpec(kind=spec.kind, dimensions={"width": -1.0}, region_part_ids={},
                                  reference_points=(T.ReferencePointRequest("p", {"q": 1}),)))
        if plugin_name == "stp":
            bad.append(T.GeometrySpec(kind="stp", dimensions={}, stp_path=FIX / "corrupt.stp",
                                      region_part_ids={"body:0": 1}))
            bad.append(T.GeometrySpec(kind="stp", dimensions={}, stp_path=FIX / "does_not_exist.stp"))
        bad.append(T.GeometrySpec(kind="cylinder", dimensions={}))
    for b in bad:
        issues = plugin.validate(b)
        assert isinstance(issues, list) and issues, b
        assert all(isinstance(i, T.Issue) and i.code and i.severity in ("error", "warning") for i in issues)
        assert any(i.severity == "error" for i in issues)
    # 정상 spec 은 오류 없음
    assert not [i for i in plugin.validate(spec) if i.severity == "error"]


# ---------------------------------------------------------------------------------- G10
def test_g10_coarsen(make_plugin, cases, tmp_path):
    plugin = make_plugin()
    if plugin.role == "jig":
        pytest.skip("jig: element count follows specimen size, no max_elements")
    label, spec, opts = cases[0]
    first = plugin.mesh(spec, opts, tmp_path / "base")
    limit = max(50, first.element_count // 2)
    small = T.MeshOptions(**{**opts.__dict__, "max_elements": limit})
    r = plugin.mesh(spec, small, tmp_path / "coarse")
    assert r.element_count <= limit, (label, r.element_count, limit)
    assert r.target_size_used_m > first.target_size_used_m
    assert any(w.code == "MESH_COARSENED" and w.severity == "warning" for w in r.warnings)


# ---------------------------------------------------------------------------------- G11
def test_g11_layered(plugin_name, results):
    if plugin_name != "layered_box":
        pytest.skip("G11 applies to layered_box only")
    for label, spec, opts, r in results:
        total = sum(v["volume_m3"] for v in r.regions.values())
        assert math.isclose(total, r.volume_m3, rel_tol=1e-12), label
        kf = kfile.read_k(r.mesh_path.read_bytes())
        ax = 2 if spec.stack_axis == "z" else 1
        lo = -0.5 * sum(t for _n, t in spec.layers) if ax == 1 else 0.0
        bounds = np.cumsum([t for _n, t in spec.layers])[:-1] + lo
        pid_of = {name: spec.region_part_ids[name] for name, _t in spec.layers}
        names = [n for n, _t in spec.layers]
        for k, zb in enumerate(bounds):
            below, above = pid_of[names[k]], pid_of[names[k + 1]]
            nodes_by_pid: dict[int, set[int]] = {below: set(), above: set()}
            for pid, conn in kf.solids.values():
                if pid in nodes_by_pid:
                    nodes_by_pid[pid].update(conn)
            iface = {n for n, xyz in kf.nodes.items() if abs(xyz[ax] * 1e-3 - zb) <= 1e-12}
            assert iface, (label, k)
            assert iface <= nodes_by_pid[below] and iface <= nodes_by_pid[above], (label, "interface nodes shared")
        assert {int(v["part_id"]) for v in r.regions.values()} == set(spec.region_part_ids.values())
