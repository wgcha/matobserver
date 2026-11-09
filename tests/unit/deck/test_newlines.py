"""줄바꿈: 읽기는 CRLF·CR 허용(LF 정규화), 쓰기는 OS 무관 LF 고정. 레시피 사본은 원본 바이트 그대로(recipe.md §2·§5 해시 = 원본 바이트)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from matobserver.deck import kfile
from matobserver.deck.assemble import assemble, find_damping, find_mass_scaling, read_recipe_texts
from matobserver.deck.materials import read_materials

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()


def _to(data: bytes, nl: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", nl)


@pytest.mark.parametrize("nl", [b"\r\n", b"\r"])
def test_parse_k_mesh_materials_params_newline_independent(tmp_path, nl):
    dp = (_FIX / "dp600-1elem-lsdyna.k").read_bytes()
    p = tmp_path / "dp.k"
    p.write_bytes(_to(dp, nl))
    a, b = kfile.read_k(_FIX / "dp600-1elem-lsdyna.k"), kfile.read_k(p)
    assert [(x.name, x.lines) for x in a] == [(x.name, x.lines) for x in b]
    assert all("\r" not in ln for x in b for ln in x.lines + [x.raw])
    assert kfile.read_mesh(p).nodes == kfile.read_mesh(_FIX / "dp600-1elem-lsdyna.k").nodes
    assert read_materials(_to(dp, nl).decode()) == read_materials(dp.decode())
    prm = _to(b"*PARAMETER\nR span,6.4e+01\nR    h    4.0\n", nl).decode()
    assert kfile.read_parameters(kfile.parse_k(prm)) == {"span": 64.0, "h": 4.0}
    assert kfile.param_refs(_to(b"*X\n&mo_tend\n$ &no\n&fric\n", nl).decode()) == {"mo_tend", "fric"}


def test_crlf_mesh_file(tmp_path):
    mr = ss.write_specimen(tmp_path / "m", nx=4)
    crlf = tmp_path / "crlf.k"
    crlf.write_bytes(_to(mr.mesh_path.read_bytes(), b"\r\n"))
    a, b = kfile.read_mesh(mr.mesh_path), kfile.read_mesh(crlf)
    assert (a.nodes, a.solids, a.node_sets, a.part_sets, a.header_comments) == \
           (b.nodes, b.solids, b.node_sets, b.part_sets, b.header_comments)


@pytest.mark.parametrize("recipe", ["bending3p-param", "bending3p-fixed"])
def test_crlf_recipe_assembles_same_generated_files_lf_only(tmp_path, recipe):
    rdir = tmp_path / "crlf_recipe"
    shutil.copytree(ss.RECIPES / recipe, rdir)
    for f in rdir.rglob("*"):
        if f.is_file():
            f.write_bytes(_to(f.read_bytes(), b"\r\n"))
    mesh = ss.write_specimen(tmp_path / "mesh")
    lf_case = ss.make_case(recipe, tmp_path / "a", mesh=mesh)
    cr_case = ss.make_case(recipe, tmp_path / "b", recipe_dir=rdir, mesh=mesh, jig=lf_case.jig_mesh)
    a = assemble(lf_case, {"body": ss.body_material()}, tmp_path / "lf", "implicit")
    b = assemble(cr_case, {"body": ss.body_material()}, tmp_path / "cr", "implicit")
    for name in ("mo_params.k", "mo_output.k", "mo_mat_body.k", "mo_specimen.k"):
        assert (a.workdir / name).read_bytes() == (b.workdir / name).read_bytes(), name
        assert b"\r" not in (b.workdir / name).read_bytes()  # 생성 파일은 OS 무관 LF
    # 레시피 소유 파일은 원본 바이트 그대로 복사(해시 규칙과 일치), 내용 해석은 같다
    assert (b.workdir / "main.k").read_bytes() == (rdir / "deck" / "main.k").read_bytes()
    assert (b.workdir / "mo_controls.k").read_bytes() == (rdir / lf_case.manifest["deck"]["controls"]["implicit"]).read_bytes()
    ta, tb = read_recipe_texts(lf_case), read_recipe_texts(cr_case)
    assert ta == tb
    assert find_mass_scaling(ta) == find_mass_scaling(tb) and find_damping(ta) == find_damping(tb)


def test_generated_text_is_lf_on_any_os(tmp_path):
    from matobserver.solvers.lsdyna import single_element as se

    main = se.write_deck(ss.body_material(), 0.05, tmp_path)
    assert b"\r" not in main.read_bytes() and b"\r" not in (tmp_path / se.MAT_FILE).read_bytes()
