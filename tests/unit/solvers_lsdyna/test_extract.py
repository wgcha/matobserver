"""S-5 binout 추출: 기준 binout 픽스처(합성, 실제 LS-DYNA 디렉터리 모양)에서 RCFORC·NODOUT 곡선,
부호·대칭 배율·SI·공통 시간축 보간 — 기준 CSV와 ≤1e-9. 추출기 단위 규칙은 DictSource로."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from matobserver.core.types import ExtractorRef, ResponseDef, ResponseWindow
from matobserver.solvers.lsdyna.binout import DictSource, open_binout
from matobserver.solvers.lsdyna.extract import (
    ExtractSettings,
    extract_case,
    glstat_ratio,
    pair_on_time,
    rcforc_resultant,
)

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"
REF = _FIX / "reference_binout"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()
W = ResponseWindow(None, None, "auto", None)


def _ref(name, **args):
    return ExtractorRef(name=name, args=args)


def _case(tmp_path, responses):
    c = ss.make_case("bending3p-param", tmp_path)
    return dataclasses.replace(c, responses=tuple(responses))


def test_s5_reference_binout_against_csv(tmp_path):
    ref = np.loadtxt(REF / "reference_curve.csv", delimiter=",", comments="#")
    resp = [
        ResponseDef("force_disp", "curve", "force", "displacement",
                    {"y": _ref("rcforc_resultant", contact_id=7, side="master", component="z", sign=-1, scale="symmetry"),
                     "x": _ref("nodout_disp", node={"ref": "indenter_ref"}, component="z", sign=-1)}, 1.0, W),
        ResponseDef("force_defl", "curve", "force", "deflection",
                    {"y": _ref("rcforc_resultant", contact_id=7, side="master", component="z", sign=-1, scale="symmetry"),
                     "x": _ref("nodout_rel_disp", node={"ref": "midspan_bottom"}, ref_nodes=[{"ref": "support_top"}],
                               component="z", sign=-1)}, 1.0, W),
        ResponseDef("ke", "scalar", "ke", None, {"value": _ref("glstat_ratio", numerator="kinetic", denominator="internal")}, 1.0, W),
    ]
    case = _case(tmp_path, resp)
    src = open_binout(REF)
    assert src is not None
    ex = extract_case(src, case, REF, "explicit", ExtractSettings(), field_reader=lambda d, c: {})
    c = ex.curves["force_disp"]
    assert c.x_quantity == "displacement" and c.y_quantity == "force"
    np.testing.assert_allclose(c.x, ref[:, 1], rtol=0, atol=1e-9 * np.max(ref[:, 1]))
    np.testing.assert_allclose(c.y, ref[:, 2], rtol=1e-9, atol=1e-9)
    assert np.all(np.diff(c.x) > 0) and np.all(c.y[1:] > 0)  # 하중 구간 x 단조, 힘 양수(압입 방향)
    d = ex.curves["force_defl"]
    np.testing.assert_allclose(d.x, ref[:, 3], rtol=0, atol=1e-9 * np.max(ref[:, 3]))
    t = ref[:, 0]
    expected_ratio = np.max(0.002 * t ** 2 / (1.0 + t ** 2))
    assert ex.scalars["ke"] == pytest.approx(expected_ratio, rel=1e-12)
    assert ex.energy["ke_ie_ratio_max"] == pytest.approx(expected_ratio, rel=1e-12)


def test_rcforc_side_scale_component_rules(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    t = np.array([0.0, 1.0, 2.0])
    z = np.array([[0, 0], [10, -10], [20, -20]], dtype=float)
    src = DictSource({"rcforc": {"ids": [1, 1], "side": [0, 1], "time": t, "x_force": 0 * z, "y_force": 0 * z + 3.0,
                                 "z_force": z}})
    s = ExtractSettings()
    _, f = rcforc_resultant(src, {"contact_id": 1, "side": "master", "component": "z", "sign": -1, "scale": "symmetry"}, case, s)
    np.testing.assert_allclose(f, [0, 40, 80])
    _, f = rcforc_resultant(src, {"contact_id": 1, "side": "slave", "component": "z", "scale": 2.0}, case, s)
    np.testing.assert_allclose(f, [0, 20, 40])
    _, f = rcforc_resultant(src, {"contact_id": 1, "side": "slave", "component": "magnitude"}, case, s)
    np.testing.assert_allclose(f, np.hypot([0, 10, 20], 3.0))
    swapped = ExtractSettings(rcforc_side_codes={"slave": 1, "master": 0})
    _, f = rcforc_resultant(src, {"contact_id": 1, "side": "master", "component": "z"}, case, swapped)
    np.testing.assert_allclose(f, [0, 10, 20])
    no_side = DictSource({"rcforc": {"ids": [1, 1], "time": t, "z_force": z}})
    _, f = rcforc_resultant(no_side, {"contact_id": 1, "side": "master", "component": "z"}, case, s)
    np.testing.assert_allclose(f, [0, -10, -20])
    with pytest.raises(KeyError):
        rcforc_resultant(src, {"contact_id": 9, "component": "z"}, case, s)


def test_missing_blocks_give_empty_curves(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    ex = extract_case(DictSource({}), case, tmp_path, "implicit", ExtractSettings(), field_reader=lambda d, c: {})
    assert len(ex.curves["force_disp"].x) == 0 and ex.energy == {}
    ex = extract_case(None, case, tmp_path, "implicit", ExtractSettings(), field_reader=lambda d, c: {})
    assert len(ex.curves["force_disp"].y) == 0


def test_pair_on_time_overlap_only():
    x, y = pair_on_time(np.array([0.0, 1.0]), np.array([0.0, 2.0]), np.array([-1.0, 0.5, 1.0, 2.0]), np.array([9, 1, 2, 9.0]))
    np.testing.assert_allclose(x, [1.0, 2.0])
    np.testing.assert_allclose(y, [1.0, 2.0])


def test_glstat_ratio_ignores_zero_internal_energy():
    src = DictSource({"glstat": {"time": [0, 1, 2], "kinetic_energy": [1.0, 0.1, 0.2], "internal_energy": [0.0, 1.0, 4.0]}})
    assert glstat_ratio(src) == pytest.approx(0.1)


def test_field_max_reader_is_used(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    ex = extract_case(DictSource({}), case, tmp_path, "implicit", ExtractSettings(),
                      field_reader=lambda d, c: {"body": {"eff_plastic_strain": 0.031}})
    assert ex.field_max == {"body": {"eff_plastic_strain": 0.031}}
