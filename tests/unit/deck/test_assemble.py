"""deck.assemble — 예약 include 6종 생성 (S-2), &mo_dtmax 반영 (S-10 앞부분), 결정성, 오류."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from matobserver.core.errors import IncompatibleOverride
from matobserver.core.types import RunOverrides
from matobserver.deck import kfile
from matobserver.deck.assemble import assemble, deck_parameter_values
from matobserver.deck.assemble import find_damping, find_hardcoded_materials, find_mass_scaling, read_recipe_texts

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()
RESERVED6 = {"mo_params.k", "mo_specimen.k", "mo_mat_body.k", "mo_jig.k", "mo_controls.k", "mo_output.k"}


@pytest.mark.parametrize("recipe", ["bending3p-param", "bending3p-fixed"])
def test_s2_reserved_includes_match_expected_text(recipe, tmp_path):
    case = ss.make_case(recipe, tmp_path)
    d = assemble(case, {"body": ss.body_material()}, tmp_path / "run", "implicit")
    files = set(d.files)
    expected_reserved = RESERVED6 if recipe.endswith("param") else RESERVED6 - {"mo_jig.k"}
    assert expected_reserved <= files
    if recipe.endswith("fixed"):
        assert "mo_jig.k" not in files and "jig.k" in files
    for name in ("mo_params.k", "mo_output.k", "mo_mat_body.k"):
        assert (tmp_path / "run" / name).read_text() == (_FIX / "expected" / recipe / name).read_text(), name
    assert (tmp_path / "run" / "mo_specimen.k").read_bytes() == case.specimen_mesh.mesh_path.read_bytes()
    if case.jig_mesh is not None:
        assert (tmp_path / "run" / "mo_jig.k").read_bytes() == case.jig_mesh.mesh_path.read_bytes()
    controls = case.manifest["deck"]["controls"]["implicit"]
    assert (tmp_path / "run" / "mo_controls.k").read_bytes() == (case.recipe_dir / controls).read_bytes()
    main = d.main_file.read_text()
    assert kfile.includes(kfile.parse_k(main))[0] == "mo_params.k"


def test_s2_parameter_unit_conversion(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path, params={"span": 0.05, "fric": 0.25})
    vals = deck_parameter_values(case, "implicit")
    assert vals["span"] == ("R", pytest.approx(50.0, rel=1e-15))  # length m → mm
    assert vals["fric"] == ("R", 0.25)  # dimensionless
    assert vals["mo_dend"][1] == pytest.approx(4.2, rel=1e-12)  # mm
    assert vals["mo_tend"][1] == 1.0  # s
    assert vals["mo_htop"][1] == pytest.approx(4.0)
    params = kfile.read_parameters(kfile.parse_k(assemble(case, {"body": ss.body_material()}, tmp_path / "r", "implicit")
                                                 .workdir.joinpath("mo_params.k").read_text()))
    assert params["span"] == pytest.approx(50.0, rel=1e-10)
    assert list(vals)[:5] == ["span", "r_ind", "r_sup", "overhang", "fric"]


def test_s10_mo_dtmax_written_and_referenced(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    d = assemble(case, {"body": ss.body_material()}, tmp_path / "run", "implicit")
    p = kfile.read_parameters(kfile.read_k(d.workdir / "mo_params.k"))
    assert p["mo_dtmax"] == pytest.approx(case.engine_params_si["implicit"]["mo_dtmax"], rel=1e-9)
    assert p["mo_dtmax"] <= p["mo_dtout"] * (1 + 1e-12)
    ctl = kfile.read_k(d.workdir / "mo_controls.k")
    auto = next(b for b in ctl if b.name == "CONTROL_IMPLICIT_AUTO")
    assert kfile.split_fields(auto.lines[0])[4] == "&mo_dtmax"
    assert kfile.to_float(kfile.split_fields(auto.lines[0])[4], p) == p["mo_dtmax"]


def test_explicit_mode_uses_explicit_controls_and_params(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    d = assemble(case, {"body": ss.body_material()}, tmp_path / "run", "explicit")
    assert "*CONTROL_TIMESTEP" in (d.workdir / "mo_controls.k").read_text()
    p = kfile.read_parameters(kfile.read_k(d.workdir / "mo_params.k"))
    assert p["mo_tend"] == pytest.approx(case.engine_params_si["explicit"]["mo_tend"])


def test_determinism_and_no_writes_to_recipe(tmp_path):
    rdir = tmp_path / "recipe"
    shutil.copytree(ss.RECIPES / "bending3p-param", rdir)
    before = {p: p.read_bytes() for p in rdir.rglob("*") if p.is_file()}
    case = ss.make_case("bending3p-param", tmp_path, recipe_dir=rdir)

    def digest(d):
        return {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in sorted(x.name for x in d.iterdir())}

    a = assemble(case, {"body": ss.body_material()}, tmp_path / "a", "implicit")
    b = assemble(case, {"body": ss.body_material()}, tmp_path / "b", "implicit")
    assert digest(a.workdir) == digest(b.workdir)
    assert {p: p.read_bytes() for p in rdir.rglob("*") if p.is_file()} == before


def test_errors_undeclared_param_missing_slot_kind_mismatch(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    with pytest.raises(ValueError, match="not declared"):
        assemble(dataclasses.replace(case, params_si={**case.params_si, "zzz": 1.0}), {"body": ss.body_material()},
                 tmp_path / "x", "implicit")
    with pytest.raises(ValueError, match="materials missing"):
        assemble(case, {}, tmp_path / "y", "implicit")
    with pytest.raises(ValueError, match="jig_mesh"):
        assemble(dataclasses.replace(case, jig_mesh=None), {"body": ss.body_material()}, tmp_path / "z", "implicit")
    with pytest.raises(ValueError, match="unknown reserved"):
        assemble(dataclasses.replace(case, engine_params_si={"implicit": {"mo_bogus": 1.0}}),
                 {"body": ss.body_material()}, tmp_path / "w", "implicit")


def test_overrides_engine_params_and_mass_scaling(tmp_path):
    case = ss.make_case("bending3p-param", tmp_path)
    ov = RunOverrides(section_by_slot={"body": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}},
                      forbid_mass_scaling=True, engine_params_si={"mo_tend": 0.0084, "mo_dtout": 1e-5})
    d = assemble(case, {"body": ss.body_material()}, tmp_path / "chk", "explicit", ov)
    p = kfile.read_parameters(kfile.read_k(d.workdir / "mo_params.k"))
    assert p["mo_tend"] == pytest.approx(0.0084) and p["mo_dtout"] == pytest.approx(1e-5)
    fixed = ss.make_case("bending3p-fixed", tmp_path / "f")
    with pytest.raises(IncompatibleOverride, match="DT2MS"):
        assemble(fixed, {"body": ss.body_material()}, tmp_path / "chk2", "explicit", ov)
    assert not (tmp_path / "chk2").exists()  # 덱을 쓰기 전에 거절
    with pytest.raises(IncompatibleOverride, match="unknown slots"):
        assemble(case, {"body": ss.body_material()}, tmp_path / "chk3", "explicit",
                 RunOverrides(section_by_slot={"nope": {}}, forbid_mass_scaling=False))


def test_deck_scanners(tmp_path):
    fixed = read_recipe_texts(ss.make_case("bending3p-fixed", tmp_path))
    param = read_recipe_texts(ss.make_case("bending3p-param", tmp_path / "p"))
    assert find_mass_scaling(fixed) and not find_mass_scaling(param)
    assert find_damping(fixed) and not find_damping(param)
    assert find_hardcoded_materials(fixed) == [] and find_hardcoded_materials(param) == []
    assert find_hardcoded_materials({"x.k": "*MAT_ELASTIC\n1 2 3\n"}) == ["x.k: *MAT_ELASTIC"]
    assert find_mass_scaling({"c.k": "*CONTROL_TIMESTEP\n       0.0       0.9         0       0.0   &dtms\n"}, {"dtms": 0.0}) == []
    assert find_mass_scaling({"c.k": "*CONTROL_TIMESTEP\n       0.0       0.9         0       0.0   &dtms\n"})
