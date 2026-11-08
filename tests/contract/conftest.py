"""계약 테스트 공통 픽스처 (소유: Impl-Backend). 타입별 계약 테스트 파일은 각 플러그인 소유자가 쓴다.

계약 테스트는 레지스트리의 `enabled: true` 항목 전부에 대해 매개변수화한다 (plugins.md §2 등록 조건, §5).

사용법 (pytest `--import-mode=importlib` 라 conftest 를 import 할 수 없으므로 픽스처·훅으로 제공한다):

    PLUGIN_TYPE = "optimizer"            # 모듈 상수: optimizer|solver|material_model|geometry_mesher|test_spec

    def test_o1(plugin_name, make_plugin):   # plugin_name 은 enabled 항목마다 자동 매개변수화
        opt = make_plugin({})                # registry.create(PLUGIN_TYPE, plugin_name, options)

(`TestSpec`·`TestData` 를 import 하는 모듈에서 pytest 가 Test* 클래스로 수집하지 않게 하려면
`from matobserver.core import types as T` 처럼 모듈로 접근한다.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matobserver.core.config import Config
from matobserver.core.registry import PLUGIN_TYPES, Registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
PLUGINS_DIR = REPO_ROOT / "config" / "plugins.d"


def _load_registry(check_classes: bool = True) -> Registry:
    return Registry.from_paths([PLUGINS_DIR], check_classes=check_classes)


def enabled_plugin_names(plugin_type: str) -> list[str]:
    """컬렉션 시점용: 레지스트리에서 enabled 인 플러그인 이름 목록 (항목이 없으면 빈 목록)."""
    if plugin_type not in PLUGIN_TYPES:
        raise ValueError(f"unknown plugin type {plugin_type!r}")
    return _load_registry(check_classes=False).names(plugin_type, enabled_only=True)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """`plugin_name` 인자를 쓰는 테스트를 모듈의 PLUGIN_TYPE 에서 enabled 인 항목마다 매개변수화한다."""
    if "plugin_name" not in metafunc.fixturenames:
        return
    plugin_type = getattr(metafunc.module, "PLUGIN_TYPE", None)
    if plugin_type is None:
        raise pytest.UsageError(f"{metafunc.module.__name__}: define PLUGIN_TYPE to use the plugin_name fixture")
    metafunc.parametrize("plugin_name", enabled_plugin_names(plugin_type))


@pytest.fixture
def plugin_type(request: pytest.FixtureRequest) -> str:
    return request.module.PLUGIN_TYPE


@pytest.fixture
def make_plugin(registry: Registry, plugin_type: str, plugin_name: str):
    """`make_plugin(options=None)` -> 레지스트리로 만든 플러그인 인스턴스 (defaults <- options 병합 적용)."""

    def _make(options=None, **kwargs):
        return registry.create(plugin_type, plugin_name, options, **kwargs)

    return _make


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """tests/fixtures/ (하위: shared=Backend, mesh/solver/optim/backend=각 소유자). 폴더가 없을 수 있다."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def config() -> Config:
    """기본 설정(파일·환경변수 영향 없음)."""
    return Config()


@pytest.fixture(scope="session")
def registry() -> Registry:
    """config/plugins.d 전체를 읽고 enabled 항목의 클래스를 검사한 레지스트리."""
    return _load_registry(check_classes=True)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """평가용 임시 작업 폴더."""
    d = tmp_path / "work"
    d.mkdir()
    return d


class NeverCancelled:
    """CancelToken 구현 (항상 False)."""

    def is_set(self) -> bool:
        return False


class SettableCancel:
    """CancelToken 구현 (set() 하면 True)."""

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


@pytest.fixture
def never_cancelled() -> NeverCancelled:
    return NeverCancelled()


@pytest.fixture
def cancel_token() -> SettableCancel:
    return SettableCancel()
