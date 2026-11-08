"""LsDynaAdapter: fallback(S-7), cleanup·keep 매핑(S-10), RunOverrides(S-11 덱 부분), 1요소(S-9, 가짜),
그리고 LS-DYNA 실행 필요 항목(@pytest.mark.lsdyna: S-6, S-11 explicit 실행, tet10 실행, 1요소 실제 실행)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from matobserver.core.config import Config, load_config
from matobserver.core.errors import FALLBACK_EXPLICIT_USED, IncompatibleOverride
from matobserver.core.types import RunOverrides, RunState, TableRow, NeutralMaterial, ElasticBlock
from matobserver.deck import kfile
from matobserver.solvers.lsdyna.adapter import LsDynaAdapter, SolverRunFailed, keep_level

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()


class Never:
    def is_set(self):
        return False


OVR = RunOverrides(section_by_slot={"body": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}}, forbid_mass_scaling=True)


# ---------------------------------------------------------------- S-7
def test_s7_implicit_failure_falls_back_to_explicit(tmp_path, monkeypatch):
    a = ss.fake_adapter()
    case = ss.make_case("bending3p-param", tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "run")
    assert (tmp_path / "run" / "explicit" / "main.k").is_file()
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "diverge", "explicit": "normal"})
    st = a.run(prep, 2, 30, Never())
    assert st.state is RunState.NORMAL and st.fallback_used is True and st.mode_used == "explicit"
    assert st.warnings[0].code == FALLBACK_EXPLICIT_USED
    ex = a.extract(prep, st)
    assert len(ex.curves["force_disp"].x) > 20 and "ke_ie_ratio_max" in ex.energy
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "error", "explicit": "error"})
    st = a.run(prep, 2, 30, Never())
    assert st.state is RunState.ERROR and st.fallback_used


def test_no_fallback_when_none_or_timeout(tmp_path, monkeypatch):
    a = ss.fake_adapter()
    fixed = ss.make_case("bending3p-fixed", tmp_path)  # fallback: none
    prep = a.prepare(fixed, {"body": ss.body_material()}, tmp_path / "run")
    assert not (tmp_path / "run" / "explicit").exists()
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "error"})
    st = a.run(prep, 1, 30, Never())
    assert st.state is RunState.ERROR and not st.fallback_used


# ---------------------------------------------------------------- S-10
def _populate(d: Path):
    names = ["main.k", "contact.k", "mo_params.k", "mo_output.k", "binout", "binout0001", "d3hsp", "messag", "mes0000",
             "mo_run.log", "d3plot", "d3plot01", "d3dump01", "runrsf", "d3full01", "scr0000", "adptmp", "fake_x.count"]
    for n in names:
        (d / n).write_text("x")


KEEP_DECK_LOG = {"main.k", "contact.k", "mo_params.k", "mo_output.k", "binout", "binout0001", "d3hsp", "messag",
                 "mes0000", "mo_run.log"}


@pytest.mark.parametrize("keep, expected", [
    ("all", None),
    ("results", KEEP_DECK_LOG | {"d3plot", "d3plot01"}),
    ("minimal", KEEP_DECK_LOG),
])
def test_s10_cleanup_keep_levels_and_inside_only(tmp_path, keep, expected):
    a = ss.fake_adapter()
    case = ss.make_case("bending3p-param", tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "run")
    for f in (tmp_path / "run").rglob("*"):
        if f.is_file():
            f.unlink()
    _populate(prep.workdir)
    (prep.workdir / "explicit").mkdir(exist_ok=True)
    _populate(prep.workdir / "explicit")
    outside = tmp_path / "outside_d3dump01"
    outside.write_text("keep me")
    (prep.workdir / "link_d3dump").symlink_to(outside)
    a.cleanup(prep, keep)
    assert outside.read_text() == "keep me" and (prep.workdir / "link_d3dump").is_symlink()
    for d in (prep.workdir, prep.workdir / "explicit"):
        left = {p.name for p in d.iterdir() if p.is_file() and not p.is_symlink()}
        assert left == (set(left) if expected is None else expected)
        if expected is None:
            assert "d3dump01" in left


def test_s10_keep_all_files_disables_cleanup(tmp_path):
    a = ss.fake_adapter(**{"solver.keep_all_files": True})
    case = ss.make_case("bending3p-param", tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "run")
    (prep.workdir / "d3dump01").write_text("x")
    a.cleanup(prep, "minimal")
    assert (prep.workdir / "d3dump01").exists()


@pytest.mark.parametrize("mode, best, other", [("best", "results", "minimal"), ("all", "results", "results"),
                                                 ("none", "minimal", "minimal")])
def test_s10_keep_d3plot_mapping(mode, best, other):
    cfg = Config({"solver.keep_d3plot": mode})
    assert keep_level(cfg, best=True) == best and keep_level(cfg, best=False) == other
    assert keep_level(Config({"solver.keep_d3plot": mode, "solver.keep_all_files": True}), best=False) == "all"


# ---------------------------------------------------------------- S-11 (덱 부분)
def test_s11_overrides_write_elform_hourglass_and_force_explicit(tmp_path):
    a = ss.fake_adapter()
    case = ss.make_case("bending3p-param", tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "chk", overrides=OVR)
    assert prep.mode == "explicit" and not (tmp_path / "chk" / "explicit").exists()
    blocks = kfile.read_k(tmp_path / "chk" / "mo_mat_body.k")
    assert kfile.split_fields(next(b for b in blocks if b.name == "SECTION_SOLID").lines[0])[1] == "1"
    hg = next(b for b in blocks if b.name == "HOURGLASS")
    hgid = kfile.split_fields(hg.lines[0])[0]
    assert kfile.split_fields(hg.lines[0])[1:3] == ["6", "0.1"]
    assert kfile.split_fields(next(b for b in blocks if b.name == "PART").lines[1])[4] == hgid
    assert "*CONTROL_TIMESTEP" in (tmp_path / "chk" / "mo_controls.k").read_text()
    with pytest.raises(ValueError):
        a.prepare(case, {"body": ss.body_material()}, tmp_path / "chk2", mode="implicit", overrides=OVR)


def test_s11_mass_scaling_is_incompatible_override(tmp_path):
    a = ss.fake_adapter()
    fixed = ss.make_case("bending3p-fixed", tmp_path)
    with pytest.raises(IncompatibleOverride):
        a.prepare(fixed, {"body": ss.body_material()}, tmp_path / "chk", overrides=OVR)
    a.prepare(fixed, {"body": ss.body_material()}, tmp_path / "chk_ok",
              overrides=RunOverrides(section_by_slot={}, forbid_mass_scaling=False))


def test_s11_explicit_check_run_with_fake_records_ke_ie(tmp_path, monkeypatch):
    a = ss.fake_adapter()
    case = ss.make_case("bending3p-param", tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "chk", overrides=OVR)
    ss.set_behavior(monkeypatch, tmp_path, {"explicit": "normal", "implicit": "error"})
    st = a.run(prep, 2, 30, Never())
    assert st.state is RunState.NORMAL and st.mode_used == "explicit" and not st.fallback_used
    assert a.extract(prep, st).energy["ke_ie_ratio_max"] == pytest.approx(0.001, rel=1e-6)


# ---------------------------------------------------------------- S-9 (가짜 배치로 배선 확인)
@pytest.mark.parametrize("table", [
    ((0.0, 3.5e8), (0.02, 4.55e8), (0.1, 5.6e8), (0.2, 6.3e8)),
    ((0.0, 4.0e7), (0.01, 4.8e7), (0.05, 6.0e7)),
])
def test_s9_single_element_reproduces_table_fake(tmp_path, table):
    a = ss.fake_adapter()
    m = NeutralMaterial(ElasticBlock(2.1e11 if table[0][1] > 1e8 else 2.0e9, 0.3, 7850.0),
                        tuple(TableRow(e, s) for e, s in table))
    c = a.single_element_uniaxial(m, table[-1][0], tmp_path / "se", 30)
    assert c.x_quantity == "eff_plastic_strain" and c.y_quantity == "true_stress"
    sel = (c.x > 1e-4) & (c.x <= table[-1][0])  # 입력 테이블 범위 안만 비교(밖은 솔버 외삽)
    ref = np.interp(c.x[sel], [e for e, _ in table], [s for _, s in table])
    assert np.max(np.abs(c.y[sel] - ref) / ref) <= 0.01
    assert c.x.max() >= table[-1][0]


def test_single_element_failure_raises(tmp_path, monkeypatch):
    a = ss.fake_adapter(**{"solver.lsdyna.batch_command": [sys.executable, "-c", "import sys; sys.exit(1)"]})
    with pytest.raises(SolverRunFailed):
        a.single_element_uniaxial(ss.body_material(), 0.05, tmp_path / "se", 10)


# ================================================================ LS-DYNA 실제 실행 (기본 제외)
def _real_adapter() -> LsDynaAdapter:
    cfg = load_config()
    if not cfg.get("solver.lsdyna.script"):
        pytest.skip("solver.lsdyna.script not configured (set MATOBSERVER_CONFIG)")
    return LsDynaAdapter({"config": cfg})


def _real_case(tmp_path, topology="hex8", x_u=0.0004):
    mesh = ss.write_specimen(tmp_path / "mesh", nx=40, ny=5, nz=4, topology=topology)
    jig = ss.write_jig(tmp_path / "mesh" / "jig", n_arc=12, ny=6)
    case = ss.make_case("bending3p-param", tmp_path, mesh=mesh, jig=jig, x_u=x_u)
    if topology != "hex8":
        import dataclasses

        slots = tuple(dataclasses.replace(b, section={"keyword": "SECTION_SOLID", "elform": 13 if topology == "tet4" else 16})
                      for b in case.slots)
        case = dataclasses.replace(case, slots=slots)
    return case


@pytest.mark.lsdyna
def test_s6_bending_sample_deck_implicit_matches_beam_theory(tmp_path):
    """S-6: 3점굽힘 샘플 덱 implicit 정상 종료, F-δ 초기 기울기 vs 보 이론 4Ebh³/L³ ≤5%."""
    a = _real_adapter()
    case = _real_case(tmp_path)
    mat = ss.body_material(E=2.0e9, sy=None)
    prep = a.prepare(case, {"body": mat}, tmp_path / "run", mode="implicit")
    st = a.run(prep, int(a.config.get("solver.lsdyna.ncpu")), float(a.config.get("solver.run_timeout_s")), Never())
    assert st.state is RunState.NORMAL and st.mode_used == "implicit", st.termination_message
    c = a.extract(prep, st).curves["force_disp"]
    assert len(c.x) >= 20
    from matobserver.verify.recipe_checks import beam_stiffness, initial_slope

    k = initial_slope(c.x, c.y, 0.1, 0.4)
    k_th = beam_stiffness(2.0e9, ss.WIDTH, ss.THICK, ss.SPAN)
    assert abs(k / k_th - 1.0) <= 0.05


@pytest.mark.lsdyna
def test_s11_explicit_check_run_records_ke_ie(tmp_path):
    a = _real_adapter()
    case = _real_case(tmp_path)
    prep = a.prepare(case, {"body": ss.body_material()}, tmp_path / "chk", overrides=OVR)
    st = a.run(prep, int(a.config.get("solver.lsdyna.ncpu")), float(a.config.get("solver.run_timeout_s")), Never())
    assert st.state is RunState.NORMAL and st.mode_used == "explicit"
    ex = a.extract(prep, st)
    assert "ke_ie_ratio_max" in ex.energy and np.isfinite(ex.energy["ke_ie_ratio_max"])


@pytest.mark.lsdyna
@pytest.mark.parametrize("topology", ["tet4"])
def test_tet_specimen_runs_without_negative_volume(tmp_path, topology):
    """module-boundaries.md §2 주: tet 샘플 덱 실제 실행. tet10은 Impl-Mesh 픽스처(tests/fixtures/mesh/)가 생기면
    그 파일로 같은 검사를 한다(없으면 skip)."""
    a = _real_adapter()
    case = _real_case(tmp_path, topology=topology)
    prep = a.prepare(case, {"body": ss.body_material(sy=None)}, tmp_path / "run", mode="implicit")
    st = a.run(prep, int(a.config.get("solver.lsdyna.ncpu")), float(a.config.get("solver.run_timeout_s")), Never())
    assert st.state is RunState.NORMAL, st.termination_message
    assert "negative volume" not in (Path(prep.main_file).parent / "d3hsp").read_text().lower()


@pytest.mark.lsdyna
def test_tet10_mesh_fixture_runs(tmp_path):
    import dataclasses

    mesh_fix = Path(__file__).resolve().parents[2] / "fixtures" / "mesh"
    cands = sorted(mesh_fix.glob("*tet10*.k")) if mesh_fix.is_dir() else []
    if not cands:
        pytest.skip("no tet10 sample in tests/fixtures/mesh/ (Impl-Mesh)")
    a = _real_adapter()
    case = _real_case(tmp_path)
    md = kfile.read_mesh(cands[0])
    assert md.solid_kind() == "tet10"
    pid = next(iter(md.solids.values()))[0]
    mesh = dataclasses.replace(case.specimen_mesh, mesh_path=cands[0])
    slots = tuple(dataclasses.replace(b, part_ids=(pid,), section={"keyword": "SECTION_SOLID", "elform": 16})
                  for b in case.slots)
    case = dataclasses.replace(case, specimen_mesh=mesh, slots=slots)
    prep = a.prepare(case, {"body": ss.body_material(sy=None)}, tmp_path / "run", mode="implicit")
    st = a.run(prep, int(a.config.get("solver.lsdyna.ncpu")), float(a.config.get("solver.run_timeout_s")), Never())
    assert st.state is RunState.NORMAL, st.termination_message
    assert "negative volume" not in (Path(prep.main_file).parent / "d3hsp").read_text().lower()


@pytest.mark.lsdyna
def test_s9_single_element_real_run(tmp_path):
    a = _real_adapter()
    m = NeutralMaterial(ElasticBlock(2.1e11, 0.3, 7850.0),
                        (TableRow(0.0, 3.5e8), TableRow(0.02, 4.55e8), TableRow(0.1, 5.6e8), TableRow(0.2, 6.3e8)))
    c = a.single_element_uniaxial(m, 0.2, tmp_path / "se", float(a.config.get("solver.run_timeout_s")))
    sel = (c.x > 1e-3) & (c.x <= m.table[-1].plastic_strain)
    ref = np.interp(c.x[sel], [r.plastic_strain for r in m.table], [r.true_stress for r in m.table])
    assert np.max(np.abs(c.y[sel] - ref) / ref) <= float(a.config.get("verify.roundtrip_tol"))
