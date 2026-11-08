"""SolverAdapter 계약 테스트 S1–S12 (plugins.md §5.2). 소유: Impl-Solver.

레지스트리의 `enabled: true` solver 전부에 대해 매개변수화된다(conftest `plugin_name`). 구현별 차이(가짜 실행 환경,
고장 주입, 생성 재료 파일 읽기)는 아래 `Harness`가 맡고 판정은 공통이다.

* `lsdyna`: 실행 없는 단위는 가짜 배치 스크립트(tests/fixtures/solver/fake_lsdyna.py). 실제 실행 항목은
  tests/unit/solvers_lsdyna/test_adapter.py 의 `@pytest.mark.lsdyna`.
* `surrogate_beam`(Impl-Optim 기준 구현): 고장 주입은 매니페스트 `surrogate` 키. 라이선스 모사(S7)는 해당 없음 → skip.
  구현이 레지스트리에 없으면 매개변수화되지 않는다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from matobserver.core.config import Config
from matobserver.core.errors import IncompatibleMaterial, IncompatibleOverride
from matobserver.core.types import (
    ElasticBlock,
    NeutralMaterial,
    RunOverrides,
    RunState,
    SolverAdapter,
    TableRow,
)

PLUGIN_TYPE = "solver"
_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()
CFG = Config()


class Never:
    def is_set(self) -> bool:
        return False


class Flag:
    def __init__(self) -> None:
        self.ev = threading.Event()

    def is_set(self) -> bool:
        return self.ev.is_set()


# ======================================================================================
# 구현별 하네스
# ======================================================================================
class Harness:
    grace_s = float(CFG.get("solver.kill_grace_s"))
    poll_s = float(CFG.get("solver.poll_interval_s"))

    def __init__(self, make_plugin, monkeypatch, tmp: Path) -> None:
        self.make_plugin, self.mp, self.tmp = make_plugin, monkeypatch, tmp

    def adapter(self) -> Any:
        return self.make_plugin({})

    def case(self, recipe: str = "bending3p-param", sub: str = "c"):
        rdir = self.tmp / "recipes" / recipe
        if not rdir.exists():
            shutil.copytree(ss.RECIPES / recipe, rdir)
        return ss.make_case(recipe, self.tmp / sub, recipe_dir=rdir)

    def inject(self, case, fault: str):
        """fault ∈ normal|error|hang|license|fail_implicit|mass_scaling → (case, cancel 토큰) 또는 pytest.skip."""
        return case, Never()

    def read_material(self, prepared, slot: str, mid: int) -> NeutralMaterial:
        pytest.skip("no material reader for this adapter")

    def check_override_deck(self, prepared) -> None:
        pytest.skip("no deck inspection for this adapter")


class LsDynaHarness(Harness):
    def __init__(self, *a) -> None:
        super().__init__(*a)
        self.cfg = ss.fake_config()
        self.grace_s = float(self.cfg.get("solver.kill_grace_s"))
        self.poll_s = float(self.cfg.get("solver.poll_interval_s"))

    def adapter(self):
        return self.make_plugin({"config": self.cfg})

    def inject(self, case, fault):
        if fault == "mass_scaling":
            return self.case("bending3p-fixed", "mass"), Never()
        beh = {"normal": {}, "error": {"implicit": "error", "explicit": "error"},
               "hang": {"implicit": "hang", "explicit": "hang"}, "license": {"implicit": "license"},
               "fail_implicit": {"implicit": "diverge", "explicit": "normal"}}[fault]
        ss.set_behavior(self.mp, self.tmp, beh)
        return case, Never()

    def read_material(self, prepared, slot, mid):
        from matobserver.deck.materials import read_materials

        return read_materials((prepared.workdir / f"mo_mat_{slot}.k").read_text())[mid].material

    def check_override_deck(self, prepared):
        from matobserver.deck import kfile

        blocks = kfile.read_k(prepared.workdir / "mo_mat_body.k")
        sec = kfile.split_fields(next(b for b in blocks if b.name == "SECTION_SOLID").lines[0])
        hg = kfile.split_fields(next(b for b in blocks if b.name == "HOURGLASS").lines[0])
        part = kfile.split_fields(next(b for b in blocks if b.name == "PART").lines[1])
        assert sec[1] == "1" and hg[1:3] == ["6", "0.1"] and part[4] == hg[0]


class SurrogateHarness(Harness):
    def _with(self, case, **sim):
        m = dict(case.manifest)
        m["surrogate"] = {**(m.get("surrogate") or {}), **sim}
        return dataclasses.replace(case, manifest=m)

    def inject(self, case, fault):
        if fault == "error":
            return self._with(case, force_state="error"), Never()
        if fault == "hang":
            return self._with(case, simulated_runtime_s=1e9), Never()
        if fault == "fail_implicit":
            return self._with(case, fail_implicit=True), Never()
        if fault == "mass_scaling":
            return self._with(case, mass_scaling=True), Never()
        if fault == "license":
            pytest.skip("surrogate_beam has no license mechanism")
        return case, Never()

    def read_material(self, prepared, slot, mid):
        doc = json.loads(Path(prepared.main_file).read_text())
        cb = doc["materials"][slot]
        e = cb["elastic"]
        table = cb.get("table")
        return NeutralMaterial(ElasticBlock(e["youngs_modulus"], e["poisson_ratio"], e["density"]),
                               None if table is None else tuple(TableRow(r["plastic_strain"], r["true_stress"]) for r in table))


HARNESSES = {"lsdyna": LsDynaHarness, "surrogate_beam": SurrogateHarness}


_READY: dict[str, str | None] = {}


def _smoke(h: Harness) -> str | None:
    """구현 준비 확인: 정상 케이스 prepare→run→extract 가 예외 없이 끝나는가. 계약 판정과 무관한 예외
    (TypeError·NameError 등 = 구현 미완성)면 사유 문자열."""
    try:
        a = h.adapter()
        case, cancel = h.inject(h.case(sub="smoke"), "normal")
        prep = a.prepare(case, _mat(), h.tmp / "smoke_run")
        a.extract(prep, a.run(prep, 1, 30.0, cancel))
    except pytest.skip.Exception:
        raise
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


@pytest.fixture
def h(plugin_name, make_plugin, monkeypatch, tmp_path) -> Harness:
    harness = HARNESSES.get(plugin_name, Harness)(make_plugin, monkeypatch, tmp_path)
    if plugin_name != "lsdyna":  # 다른 소유자 구현(surrogate_beam: Impl-Optim)이 미완성이면 skip
        if plugin_name not in _READY:
            _READY[plugin_name] = _smoke(harness)
        if _READY[plugin_name] is not None:
            pytest.skip(f"{plugin_name} implementation not ready: {_READY[plugin_name]}")
    return harness


def _mat() -> dict[str, NeutralMaterial]:
    return {"body": ss.body_material()}


def _snapshot(root: Path, exclude: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and exclude not in p.parents:
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _run(h, case, fault="normal", timeout_s=30.0, sub="run", mode=None, overrides=None):
    a = h.adapter()
    case, cancel = h.inject(case, fault)
    prep = a.prepare(case, _mat(), h.tmp / sub, mode=mode, overrides=overrides)
    return a, prep, a.run(prep, 1, timeout_s, cancel)


# ======================================================================================
# S1–S12
# ======================================================================================
def test_s1_capabilities(h):
    a = h.adapter()
    assert isinstance(a, SolverAdapter)
    c = a.capabilities()
    assert c.unit_system == "mm-N-s-tonne"
    assert c.analysis_modes and c.analysis_modes <= {"implicit", "explicit"}
    assert {"rcforc_resultant", "nodout_disp"} <= c.extractors
    assert {"MAT_001", "MAT_024"} <= c.material_keywords
    assert isinstance(c.extension_keys, frozenset) and isinstance(c.supports_overrides, bool)


def test_s2_prepare_writes_only_inside_workdir(h):
    case = h.case()
    work = h.tmp / "work"
    before = _snapshot(h.tmp, work)
    h.adapter().prepare(case, _mat(), work)
    assert _snapshot(h.tmp, work) == before  # 레시피 사본·메시·그 밖 파일 변화 0건
    assert any(work.iterdir())


def test_s3_prepare_deterministic(h):
    case = h.case()
    a = h.adapter()

    def digest(d: Path):
        return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest() for p in d.rglob("*") if p.is_file()}

    pa = a.prepare(case, _mat(), h.tmp / "a")
    pb = h.adapter().prepare(case, _mat(), h.tmp / "b")
    assert digest(pa.workdir) == digest(pb.workdir)


def test_s4_normal_error_timeout(h):
    _, _, st = _run(h, h.case(), "normal", sub="ok")
    assert st.state is RunState.NORMAL
    _, _, st = _run(h, h.case(), "error", sub="err")
    assert st.state is RunState.ERROR
    timeout = 1.0
    t0 = time.monotonic()
    _, _, st = _run(h, h.case(), "hang", timeout_s=timeout, sub="hang")
    assert st.state is RunState.TIMEOUT
    assert time.monotonic() - t0 <= timeout + h.grace_s + 1.0  # +1 s: 프로세스 기동 여유


def test_s5_extract_si_monotonic_positive_symmetry_scaled(h):
    case = h.case()
    a, prep, st = _run(h, case)
    c = a.extract(prep, st).curves["force_disp"]
    assert len(c.x) >= 20 and len(c.x) == len(c.y)
    assert np.all(np.diff(c.x) >= 0) and c.x[-1] > c.x[0]  # 하중 구간 x 단조
    assert np.all(c.y[1:] > 0)  # 힘 양수(압입 방향)
    dend = case.engine_params_si[st.mode_used]["mo_dend"]
    assert c.x[-1] == pytest.approx(dend, rel=1e-6)  # SI(m): 덱 mm → m 환산 확인
    # 대칭 배율: scale 생략 응답과 비교하면 force_scale 배
    r = case.responses[0]
    y_noscale = dataclasses.replace(r.extractor["y"], args={k: v for k, v in r.extractor["y"].args.items() if k != "scale"})
    case2 = dataclasses.replace(case, responses=(dataclasses.replace(r, extractor={**r.extractor, "y": y_noscale}),))
    prep2 = dataclasses.replace(prep, case=case2)
    c2 = a.extract(prep2, st).curves["force_disp"]
    np.testing.assert_allclose(c.y, c2.y * case.manifest["symmetry"]["force_scale"], rtol=1e-12)


def test_s6_material_roundtrip_units(h):
    m = NeutralMaterial(ElasticBlock(2.3456789e9, 0.3812345, 1234.56789),
                        (TableRow(0.0, 4.123456e7), TableRow(0.0123456, 4.81e7), TableRow(0.0567, 6.0123456e7)))
    case = h.case()
    prep = h.adapter().prepare(case, {"body": m}, h.tmp / "rt")
    b = case.slots[0]
    back = h.read_material(prep, "body", b.mid)
    for f in ("youngs_modulus", "poisson_ratio", "density"):
        assert getattr(back.elastic, f) == pytest.approx(getattr(m.elastic, f), rel=1e-6)
    assert len(back.table) == len(m.table)
    for r0, r1 in zip(m.table, back.table):
        assert r1.plastic_strain == pytest.approx(r0.plastic_strain, rel=1e-6, abs=1e-12)
        assert r1.true_stress == pytest.approx(r0.true_stress, rel=1e-6)
    if isinstance(h, LsDynaHarness):  # Pa→MPa, kg/m³→tonne/mm³ 를 파일 원값으로 확인
        from matobserver.deck.materials import read_materials

        raw = read_materials((prep.workdir / "mo_mat_body.k").read_text())[b.mid].raw
        assert raw["e"] == pytest.approx(2345.6789, rel=1e-9) and raw["ro"] == pytest.approx(1.23456789e-9, rel=1e-9)


def test_s7_license_retry_then_license_timeout(h):
    a, prep, st = _run(h, h.case(), "license")
    assert st.state is RunState.LICENSE_TIMEOUT and st.license_waits >= 1


def test_s8_unsupported_extension_raises(h):
    m = NeutralMaterial(ElasticBlock(2e9, 0.38, 1200.0), extensions={"nosolver.unknown_keyword": {"x": 1.0}})
    with pytest.raises(IncompatibleMaterial):
        h.adapter().prepare(h.case(), {"body": m}, h.tmp / "ext")


def test_s9_cancel_token(h):
    a = h.adapter()
    case, _ = h.inject(h.case(), "hang")
    prep = a.prepare(case, _mat(), h.tmp / "cancel")
    tok = Flag()
    delay = 0.5 if isinstance(h, LsDynaHarness) else 0.0
    if delay:
        threading.Timer(delay, tok.ev.set).start()
    else:
        tok.ev.set()
    t0 = time.monotonic()
    st = a.run(prep, 1, 60.0, tok)
    assert st.state is RunState.CANCELLED
    assert time.monotonic() - t0 <= delay + h.poll_s + h.grace_s + 1.0


@pytest.mark.parametrize("keep", ["all", "results", "minimal"])
def test_s10_cleanup_inside_only(h, keep):
    a, prep, st = _run(h, h.case(), sub=f"cl_{keep}")
    sentinel = h.tmp / "outside_d3dump01"
    sentinel.write_text("x")
    before_out = _snapshot(h.tmp, prep.workdir)
    before_in = {p for p in prep.workdir.rglob("*") if p.is_file()}
    a.cleanup(prep, keep)
    assert _snapshot(h.tmp, prep.workdir) == before_out
    after_in = {p for p in prep.workdir.rglob("*") if p.is_file()}
    assert prep.main_file in after_in  # 덱은 모든 단계에서 남는다
    if keep == "all":
        assert after_in == before_in
    assert a.extract(prep, st).curves["force_disp"].x.size > 0  # 결과(binout 등)도 남는다


def test_s11_single_element_reproduces_table(h):
    a = h.adapter()
    table = ((0.0, 3.5e8), (0.02, 4.55e8), (0.1, 5.6e8), (0.2, 6.3e8))
    m = NeutralMaterial(ElasticBlock(2.1e11, 0.3, 7850.0), tuple(TableRow(e, s) for e, s in table))
    c = a.single_element_uniaxial(m, 0.2, h.tmp / "se", 60.0)
    sel = (c.x > 1e-4) & (c.x <= table[-1][0])
    assert sel.sum() >= 5
    ref = np.interp(c.x[sel], [e for e, _ in table], [s for _, s in table])
    assert np.max(np.abs(c.y[sel] - ref) / ref) <= float(CFG.get("verify.roundtrip_tol"))


def test_s12_run_overrides(h):
    a = h.adapter()
    if not a.capabilities().supports_overrides:
        pytest.skip("supports_overrides=False")
    ov = RunOverrides(section_by_slot={"body": {"elform": 1, "hourglass": {"ihq": 6, "qm": 0.1}}}, forbid_mass_scaling=True)
    case, cancel = h.inject(h.case(), "fail_implicit")  # implicit이 실패하도록 해도 fallback 없이 explicit 고정
    prep = a.prepare(case, _mat(), h.tmp / "ov", overrides=ov)
    assert prep.mode == "explicit"
    h.check_override_deck(prep) if isinstance(h, LsDynaHarness) else None
    st = a.run(prep, 1, 30.0, cancel)
    assert st.state is RunState.NORMAL and st.mode_used == "explicit" and st.fallback_used is False
    ms_case, _ = h.inject(h.case(sub="ms"), "mass_scaling")
    with pytest.raises(IncompatibleOverride):
        a.prepare(ms_case, _mat(), h.tmp / "ov_ms", overrides=ov)
