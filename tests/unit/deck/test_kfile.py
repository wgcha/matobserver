"""deck.kfile — 형식 도구와 .k 리더 (module-boundaries.md §2 메시 규격 재파싱 포함)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from matobserver.deck import kfile

_SS = Path(__file__).resolve().parents[2] / "fixtures" / "solver" / "solver_support.py"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _SS)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


@pytest.mark.parametrize("v", [0.3, 210000.0, 7.85e-9, 1.2e-9, -3.25, 0.49925, 1e30, 123456789.0])
def test_fit_width_and_closeness(v):
    s = kfile.fit(v)
    assert len(s) == 10
    assert abs(float(s) - v) / abs(v) < 5e-6


def test_fit_exact_short_values():
    assert kfile.fit(7.85e-9).strip() == "7.85E-9"
    assert kfile.fit(0.0).strip() == "0.0"
    assert kfile.fit(2000.0).strip() == "2000.0"
    assert float(kfile.fit(1.2345678901e-9, 20)) == 1.2345678901e-9


def test_fit_rejects_nonfinite():
    with pytest.raises(ValueError):
        kfile.fit(float("nan"))


def test_card_and_fields():
    line = kfile.card([1, 2.5, "&mo_dtout", None, 0])
    assert line == "         1       2.5 &mo_dtout                   0"
    assert kfile.split_fields(line) == ["1", "2.5", "&mo_dtout", "", "0"]
    assert kfile.split_fields("1, 2.5 ,&x") == ["1", "2.5", "&x"]
    with pytest.raises(ValueError):
        kfile.card(["toolongfield"])


def test_parse_k_blocks_comments_end_and_long_format():
    text = "$ head\n*KEYWORD\n*MAT_ELASTIC +\n$# c\n1 2\n\n*PART\nt\n*END\n*NODE\n1\n"
    blocks = kfile.parse_k(text)
    assert [b.name for b in blocks] == ["KEYWORD", "MAT_ELASTIC", "PART", "END"]
    assert blocks[1].long_format and blocks[1].width == 20 and blocks[1].lines == ["1 2"]
    assert kfile.parse_k("*MAT_ELASTIC+\n1\n")[0].long_format


def test_to_float_param_refs_and_fortran_exponent():
    assert kfile.to_float("&mo_dend", {"mo_dend": 4.2}) == 4.2
    assert kfile.to_float("-&mo_dend", {"mo_dend": 4.2}) == -4.2
    assert kfile.to_float("1.5-3") == 1.5e-3
    assert kfile.to_float("") == 0.0
    with pytest.raises(KeyError):
        kfile.to_float("&nope", {})


def test_read_parameters_comma_and_fixed():
    blocks = kfile.parse_k("*PARAMETER\nR span,6.4e+01\nI nseg,6\n*PARAMETER\nR    h    4.0\n")
    p = kfile.read_parameters(blocks)
    assert p == {"span": 64.0, "nseg": 6.0, "h": 4.0}
    assert kfile.parameter_names_defined(blocks) == ["span", "nseg", "h"]


def test_param_refs_and_includes():
    text = "*INCLUDE\nmo_params.k\n$ &ignored\n*CONTROL_TERMINATION\n&mo_tend &FRIC\n"
    assert kfile.param_refs(text) == {"mo_tend", "fric"}
    assert kfile.includes(kfile.parse_k(text)) == ["mo_params.k"]


def test_read_mesh_two_line_hex8_and_sets(tmp_path):
    ss = _support()
    mr = ss.write_specimen(tmp_path, nx=4, ny=2, nz=2)
    md = kfile.read_mesh(mr.mesh_path)
    assert len(md.nodes) == mr.node_count and len(md.solids) == mr.element_count
    assert md.solid_kind() == "hex8"
    assert set(md.node_sets) == {901, 902} and md.part_sets == {900: (1,)}
    assert all(md.nodes[n][0] == 0.0 for n in md.node_sets[901])
    assert md.keywords[0] == "KEYWORD" and md.header_comments[0] == "$ matobserver mesh"


def test_read_mesh_tet4_degenerate_and_tet10(tmp_path):
    ss = _support()
    md = kfile.read_mesh(ss.write_specimen(tmp_path / "t4", nx=2, ny=1, nz=1, topology="tet4").mesh_path)
    assert md.solid_kind() == "tet4"
    tet10 = ("*KEYWORD\n*NODE\n" + "".join(f"{i:>8d}{float(i):>16.9e}{0.0:>16.9e}{0.0:>16.9e}{0:>8d}{0:>8d}\n" for i in range(1, 11))
             + "*ELEMENT_SOLID\n" + f"{1:>8d}{1:>8d}\n" + "".join(f"{i:>8d}" for i in range(1, 11)) + "\n*END\n")
    p = tmp_path / "t10.k"
    p.write_text(tet10)
    md10 = kfile.read_mesh(p)
    assert md10.solids[1] == (1, tuple(range(1, 11))) and md10.solid_kind() == "tet10"


def test_read_mesh_one_line_legacy_element():
    md = kfile.read_mesh(Path(__file__).resolve().parents[2] / "fixtures" / "solver" / "dp600-1elem-lsdyna.k")
    assert md.solids[1] == (1, (1, 2, 3, 4, 5, 6, 7, 8))
    assert md.nodes[7] == (1.0, 1.0, 1.0)
    assert md.node_sets[4] == (2, 3, 6, 7)
