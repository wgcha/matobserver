"""프로젝트 골격: pyproject 설정(lsdyna 마커 제외), 패키지 레이아웃, 소유자 표시."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

PACKAGES = ["api", "jobs", "artifacts", "recipes", "matnexus", "validation", "engine", "optimizers",
            "materials", "testspecs", "solvers", "solvers.surrogate", "solvers.lsdyna", "deck", "verify", "mesh"]


def test_pyproject_pytest_and_python_settings():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "matobserver"
    assert data["project"]["requires-python"] == ">=3.11"
    ini = data["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("lsdyna") for m in ini["markers"])
    assert "not lsdyna" in ini["addopts"]            # 기본 실행에서 제외
    assert "--strict-markers" in ini["addopts"]
    assert "dev" in data["project"]["optional-dependencies"]
    assert data["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_lsdyna_marker_registered(pytestconfig: pytest.Config):
    assert any(line.startswith("lsdyna") for line in pytestconfig.getini("markers"))


@pytest.mark.parametrize("pkg", PACKAGES)
def test_other_owner_packages_exist_and_are_importable(pkg: str):
    """각 소유자 패키지가 존재하고 import 된다. `__init__.py` 내용은 각 소유자 범위라 비어 있어야 한다고 강제하지 않는다
    (골격 단계에는 비어 있었지만 소유자가 구현을 채우면 달라진다)."""
    importlib.import_module(f"matobserver.{pkg}")
    init = REPO / "src" / "matobserver" / Path(*pkg.split(".")) / "__init__.py"
    assert init.is_file()


def test_core_modules_importable():
    for mod in ("types", "units", "registry", "config", "errors", "ids", "hashing"):
        importlib.import_module(f"matobserver.core.{mod}")


def test_plugins_d_skeleton_has_owner_comments():
    expected = {"mesh.yaml": "Impl-Mesh", "solver.yaml": "Impl-Solver", "optim.yaml": "Impl-Optim"}
    for name, owner in expected.items():
        first = (REPO / "config" / "plugins.d" / name).read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#") and owner in first
