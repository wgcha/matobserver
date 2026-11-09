"""verify_recipe 종단 (가짜 배치 스크립트): parametric 통과, fixed 경고, 실패·strict·error 경로 (S-8)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from matobserver.core import errors as E
from matobserver.core.types import ResolvedRecipe
from matobserver.verify.dryrun import verify_recipe

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()


def _registry(kind, cfg):
    if kind == "fake":
        return ss.FakeRegistry(cfg)
    from matobserver.core.registry import Registry

    reg = Registry.from_paths([_FIX.parents[2] / "config" / "plugins.d"])
    for t, n in (("geometry_mesher", "box"), ("geometry_mesher", "jig.bending3p_rollers"), ("test_spec", "bending")):
        if n not in reg.names(t):
            pytest.skip(f"{t}/{n} not registered yet")
    return reg


def _run(tmp_path, recipe, *, registry="fake", strict=False, recipe_dir=None, **cfg):
    c = ss.fake_config(**cfg)
    svc = ss.FakeServices(root=tmp_path, config=c, registry=_registry(registry, c))
    rdir = recipe_dir or ss.RECIPES / recipe
    m = yaml.safe_load((rdir / "manifest.yaml").read_text())
    return verify_recipe(ResolvedRecipe(f"{recipe}@1", "sha256:fixture", rdir, m), svc, strict), svc


def _codes(rep):
    return {c["code"]: c["result"] for c in rep["checks"]}


@pytest.mark.parametrize("registry", ["fake", "real"])
def test_parametric_recipe_passes(tmp_path, monkeypatch, registry):
    ss.set_behavior(monkeypatch, tmp_path, {})
    rep, svc = _run(tmp_path, "bending3p-param", registry=registry)
    assert rep["status"] == "passed", rep
    codes = _codes(rep)
    for code in (E.VERIFY_TERMINATION, E.VERIFY_RESPONSE, E.VERIFY_UNITS, E.VERIFY_PENETRATION, E.VERIFY_MESH_CONVERGENCE):
        assert codes[code] == "pass", code
    assert rep["solver"] == {"name": "lsdyna", "adapter_version": "1.0.0", "version": "R13.1.1"}
    assert rep["verification_id"] == "ver_fixture" and rep["started_at"] and rep["finished_at"]
    assert rep["artifacts"] == [{"artifact_id": "art_1", "kind": "verification_bundle"}]
    with zipfile.ZipFile(svc.artifacts[0][0]) as z:
        names = z.namelist()
    assert "s1/mo_params.k" in names and "s2/mo_params.k" in names and "s1/binout" in names
    # 산출물·실행 폴더는 work_dir/_recipes/{verification_id}/ 아래에만
    assert all(str(p).startswith(str(tmp_path / "_recipes" / "ver_fixture")) for p, _ in svc.artifacts)


@pytest.mark.parametrize("registry", ["fake", "real"])
def test_fixed_recipe_warnings_and_reference_curve(tmp_path, monkeypatch, registry):
    ss.set_behavior(monkeypatch, tmp_path, {})
    rep, _ = _run(tmp_path, "bending3p-fixed", registry=registry)
    codes = _codes(rep)
    assert rep["status"] == "passed_with_warnings", rep
    for code in (E.WARN_MASS_SCALING, E.WARN_DAMPING, E.WARN_NO_FALLBACK, E.WARN_COMPLIANCE_MISSING):
        assert codes[code] == "warn", code
    assert codes[E.WARN_HARDCODED_MATERIAL] == "pass"  # 지그 *MAT_RIGID 는 허용
    try:
        import matobserver.engine.alignment  # noqa: F401
        import matobserver.engine.objective  # noqa: F401
    except ImportError:
        assert codes[E.VERIFY_REFERENCE_CURVE] == "skipped"
    else:
        assert codes[E.VERIFY_REFERENCE_CURVE] == "pass"
    rep_strict, _ = _run(tmp_path / "strict", "bending3p-fixed", registry=registry, strict=True)
    assert rep_strict["status"] == "failed"


@pytest.mark.parametrize("behavior, code", [
    ({"implicit": "error", "explicit": "error"}, E.VERIFY_TERMINATION),
    ({"stiffness_scale": 3.0}, E.VERIFY_UNITS),
    ({"penetration_mm": 0.01}, E.VERIFY_PENETRATION),
    ({"first_force_N": 5.0}, E.VERIFY_PENETRATION),
])
def test_failures_reject(tmp_path, monkeypatch, behavior, code):
    ss.set_behavior(monkeypatch, tmp_path, behavior)
    rep, _ = _run(tmp_path, "bending3p-param")
    assert rep["status"] == "failed"
    assert _codes(rep)[code] == "fail"


def test_fallback_used_is_pass_with_warning(tmp_path, monkeypatch):
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "diverge"})
    rep, _ = _run(tmp_path, "bending3p-param")
    codes = _codes(rep)
    assert codes[E.VERIFY_TERMINATION] == "pass" and codes[E.VERIFY_FALLBACK_USED] == "warn"
    assert codes[E.WARN_KE_RATIO] == "pass"  # explicit 실행 KE/IE = 0.001
    assert rep["status"] == "passed_with_warnings"


def test_reference_card_missing_slot_is_error(tmp_path, monkeypatch):
    ss.set_behavior(monkeypatch, tmp_path, {})
    rdir = tmp_path / "recipe"
    shutil.copytree(ss.RECIPES / "bending3p-param", rdir)
    (rdir / "verification" / "reference_card.yaml").write_text("other: {elastic: {youngs_modulus: 1.0e9, "
                                                                 "poisson_ratio: 0.3, density: 1000.0}}\n")
    rep, _ = _run(tmp_path, "bending3p-param", recipe_dir=rdir)
    assert rep["status"] == "error" and "body" in rep["error"]["message"]


def test_crlf_recipe_dry_run_same_verdict(tmp_path, monkeypatch):
    ss.set_behavior(monkeypatch, tmp_path, {})
    rdir = tmp_path / "recipe"
    shutil.copytree(ss.RECIPES / "bending3p-fixed", rdir)
    for f in rdir.rglob("*"):
        if f.is_file():
            f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    rep, _ = _run(tmp_path / "crlf", "bending3p-fixed", recipe_dir=rdir)
    ref, _ = _run(tmp_path / "lf", "bending3p-fixed")
    assert rep["status"] == ref["status"] == "passed_with_warnings"
    assert _codes(rep) == _codes(ref)
