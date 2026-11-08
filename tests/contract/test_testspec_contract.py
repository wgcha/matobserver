"""TestSpec 계약 테스트 T1–T9 (plugins.md §5.5). 소유: Impl-Optim."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from matobserver.core import errors as E
from matobserver.core import types as T

PLUGIN_TYPE = "test_spec"

MANIFEST = {
    "specimen": {"geometry_kinds": ["box", "layered_box", "stp"]},
    "parameters": [{"name": "span", "default": 0.064, "matnexus_key": "span"}],
    "responses": [{"response_key": "force_disp", "test_channel": "force", "axis": "displacement",
                   "extractor": {"y": {"name": "rcforc_resultant", "contact_id": 1, "component": "z", "sign": -1,
                                       "scale": "symmetry"},
                                 "x": {"name": "nodout_disp", "node": {"ref": "indenter_ref"}, "component": "z", "sign": -1}},
                   "alt_axes": {"deflection": {"x": {"name": "nodout_rel_disp", "node": {"ref": "midspan_bottom"},
                                                     "ref_nodes": [{"ref": "support_top"}], "component": "z", "sign": -1}}},
                   "weight": 1.0, "window": {"split": "auto"}}],
}


@pytest.fixture(scope="module")
def flex_run(fixtures_dir):
    doc = json.loads((fixtures_dir / "optim" / "flexural_run.json").read_text(encoding="utf-8"))
    ch = {k: np.asarray(v["values"], dtype=float) for k, v in doc["channels"].items()}
    return T.RunRecord(run_id="inline:" + doc["run_key"], test_type_key=doc["test_type_key"], channels=ch,
                       specimen_dimensions=doc["specimen_dimensions"], conditions=doc["conditions"],
                       instrument=doc["instrument"], specimen_id=doc["specimen_ref"], material_id=None,
                       source_sha256=None, fetched_hash="sha256:x")


def _ti(kind="box", **kw):
    d = {"test_key": "t1", "geometry": {"kind": kind}, "recipe_params": {}}
    d.update(kw)
    return d


def test_t1_map_run_si_and_channel_rule(make_plugin, flex_run):
    ts = make_plugin({})
    assert flex_run.test_type_key in ts.matnexus_test_type_keys
    d = ts.map_run(flex_run, "auto", 1.8e-7)
    assert d.x_quantity == "displacement" and d.displacement_source == "crosshead"  # 크로스헤드 기본
    assert np.array_equal(d.y, flex_run.channels["force"])
    d2 = make_plugin({"prefer_deflection": True}).map_run(flex_run, "auto", 1.8e-7)
    assert d2.x_quantity == "deflection" and d2.compliance_m_per_n is None
    d3 = ts.map_run(flex_run, "deflection", 1.8e-7)
    assert d3.displacement_source == "deflectometer"
    no_disp = T.RunRecord(**{**flex_run.__dict__, "channels": {k: v for k, v in flex_run.channels.items()
                                                              if k != "displacement"}})
    assert ts.map_run(no_disp, "auto", None).x_quantity == "deflection"


def test_t2_missing_channels_dims(make_plugin, flex_run):
    ts = make_plugin({})
    bad = T.RunRecord(**{**flex_run.__dict__, "channels": {"time": flex_run.channels["time"]},
                         "specimen_dimensions": {"width": 0.01}})
    codes = {i.code for i in ts.validate(_ti(), [bad], {})}
    assert E.CHANNEL_MISSING in codes and E.SPECIMEN_DIMENSION_MISSING in codes
    four = T.RunRecord(**{**flex_run.__dict__, "specimen_dimensions": {**flex_run.specimen_dimensions, "load_span": 0.02}})
    assert E.TEST_VARIANT_UNSUPPORTED in {i.code for i in ts.validate(_ti(), [four], MANIFEST)}
    assert E.GEOMETRY_KIND_NOT_ALLOWED in {i.code for i in ts.validate(_ti("cylinder"), [flex_run], MANIFEST)}
    short = T.RunRecord(**{**flex_run.__dict__, "specimen_dimensions": {**flex_run.specimen_dimensions, "span": 0.012}})
    assert E.GEOMETRY_DIMENSION_INVALID in {i.code for i in ts.validate(_ti(), [short], MANIFEST)}
    mid = T.RunRecord(**{**flex_run.__dict__, "specimen_dimensions": {**flex_run.specimen_dimensions, "span": 0.03}})
    iss = ts.validate(_ti(), [mid], MANIFEST)
    assert [i.severity for i in iss if i.code == E.BEAM_THEORY_APPROXIMATE] == ["warning"]
    assert ts.validate(_ti(), [flex_run], MANIFEST) == []


def test_t3_extractors_supported(make_plugin, registry):
    ts = make_plugin({})
    names = set()
    for xq in ("displacement", "deflection"):
        for r in ts.responses(MANIFEST, xq):
            names |= {ex.name for ex in r.extractor.values()}
    checked = 0
    for sname in registry.names("solver"):
        try:
            caps = registry.create("solver", sname, {}).capabilities()
        except Exception:
            continue
        assert names <= set(caps.extractors), (sname, names - set(caps.extractors))
        checked += 1
    assert checked >= 1


def test_t4_allowed_geometries_registered(make_plugin, registry):
    meshers = set(registry.names("geometry_mesher"))
    if not meshers:
        pytest.skip("geometry_mesher 미등록(Impl-Mesh 병합 전)")
    assert set(make_plugin({}).allowed_geometries()) <= meshers


def test_t5_beam_theory_E0(make_plugin):
    ts = make_plugin({})
    E0, b, h, L = 2.31e9, 0.0101, 0.004, 0.064
    k = 4 * E0 * b * h**3 / L**3
    x = np.linspace(0, 0.003, 200)
    y = k * x
    d = T.TestData("r", "displacement", x, y, "crosshead", None, {}, {"width": b, "thickness": h, "span": L}, {})
    g = T.GeometrySpec("box", {"width": b, "thickness": h, "total_length": 0.08})
    hints = ts.initial_hints([d], g, {"span": L})
    assert abs(hints["E0"] / E0 - 1) <= 1e-3
    assert hints["eps_p_max0"] >= 0.01
    assert ts.initial_hints([d], T.GeometrySpec("stp", {}), {"span": L}) == {}


def test_t6_no_compliance_in_map_run(make_plugin, flex_run):
    d = make_plugin({}).map_run(flex_run, "displacement", 1.8e-7)
    assert np.array_equal(d.x, flex_run.channels["displacement"])  # 비트 동일
    assert d.compliance_m_per_n == 1.8e-7


def test_t7_determinism(make_plugin, flex_run):
    a, b = make_plugin({}), make_plugin({})
    da, db = a.map_run(flex_run, "auto", 1e-7), b.map_run(flex_run, "auto", 1e-7)
    assert np.array_equal(da.x, db.x) and np.array_equal(da.y, db.y)
    assert a.responses(MANIFEST, "displacement") == b.responses(MANIFEST, "displacement")


def test_t8_param_space_targets(make_plugin):
    ts = make_plugin({})
    sp = ts.param_space(_ti(), MANIFEST)
    assert isinstance(sp, T.ParamSpace)
    assert set(sp.names()) == set(ts.param_targets())


def test_t9_reference_strain_rate(make_plugin, flex_run):
    ts = make_plugin({})
    h, V, L = 0.004, 3.3e-5, 0.064
    g = T.GeometrySpec("box", {"width": 0.01, "thickness": h, "total_length": 0.08})
    d = ts.map_run(flex_run, "displacement", None)
    info = ts.reference_strain_rate(d, g, {"span": L})
    assert info.velocity_source == "condition.test_speed"
    assert abs(info.value_1_s - 6 * h * V / L**2) <= 1e-12 * (6 * h * V / L**2)
    nospeed = T.TestData(**{**d.__dict__, "meta": {**d.meta, "conditions": {}}})
    i2 = ts.reference_strain_rate(nospeed, g, {"span": L})
    t, x = d.meta["time"], d.x
    v_exp = float(np.median(np.diff(x[: int(np.argmax(d.y)) + 1]) / np.diff(t[: int(np.argmax(d.y)) + 1])))
    assert i2.velocity_source == "time_channel" and math.isclose(i2.value_1_s, 6 * h * v_exp / L**2, rel_tol=1e-12)
    none = T.TestData(**{**d.__dict__, "meta": {"conditions": {}}})
    i3 = ts.reference_strain_rate(none, g, {"span": L})
    assert i3.value_1_s is None and i3.velocity_source == "none"


def test_t10_load_mode(make_plugin):
    assert make_plugin({}).load_mode in {"three_point_bending", "four_point_bending", "uniaxial_compression",
                                          "dynamic_bending"}
