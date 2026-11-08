"""B-10: 레지스트리 로더 — 이름 중복·클래스 불일치는 기동 실패 (서버 `app_factory`, CLI `serve`/`worker`)."""

from __future__ import annotations

import io

import pytest
import yaml

from matobserver import cli
from matobserver.api.app import app_factory
from matobserver.core.errors import RegistryError

from be_support import PLUGINS_DIR, make_config

GOOD = {"type": "test_spec", "name": "bending", "class": "matobserver.testspecs.bending:BendingTestSpec", "version": "1.0.0", "enabled": True}


def write_plugins(d, *files):
    d.mkdir(exist_ok=True)
    for name, entries in files:
        (d / name).write_text(yaml.safe_dump({"schema_version": "1.0", "plugins": entries}), encoding="utf-8")
    return d


def config_file(tmp_path, registry_dir):
    cfg = {
        "paths": {"data_dir": str(tmp_path / "data"), "work_dir": str(tmp_path / "data" / "work")},
        "jobstore": {"sqlite_path": str(tmp_path / "data" / "jobs.sqlite")},
        "recipes": {"store_dir": str(tmp_path / "data" / "recipes")},
        "registry": {"paths": [str(registry_dir)]},
    }
    p = tmp_path / "matobserver.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def duplicate_dir(tmp_path):
    return write_plugins(tmp_path / "dup", ("a.yaml", [GOOD]), ("b.yaml", [dict(GOOD)]))


def mismatch_dir(tmp_path):
    bad = dict(GOOD, **{"class": "be_support:FakeCompressionSpec"})  # 클래스 name 'compression' != 등록 이름 'bending'
    return write_plugins(tmp_path / "mismatch", ("a.yaml", [bad]))


def import_failure_dir(tmp_path):
    return write_plugins(tmp_path / "noimport", ("a.yaml", [dict(GOOD, **{"class": "no_such_module_xyz:Thing"})]))


def missing_dir(tmp_path):
    return tmp_path / "does-not-exist"


BAD_DIRS = [duplicate_dir, mismatch_dir, import_failure_dir, missing_dir]


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    for k in ("MATOBSERVER_CONFIG",):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("make_dir", BAD_DIRS, ids=[f.__name__ for f in BAD_DIRS])
def test_app_factory_fails_startup(tmp_path, monkeypatch, make_dir):
    monkeypatch.setenv("MATOBSERVER_CONFIG", str(config_file(tmp_path, make_dir(tmp_path))))
    with pytest.raises(RegistryError):
        app_factory()


def test_app_factory_message_names_the_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("MATOBSERVER_CONFIG", str(config_file(tmp_path, duplicate_dir(tmp_path))))
    with pytest.raises(RegistryError, match="duplicate plugin test_spec/bending"):
        app_factory()
    monkeypatch.setenv("MATOBSERVER_CONFIG", str(config_file(tmp_path, mismatch_dir(tmp_path))))
    with pytest.raises(RegistryError, match="class name"):
        app_factory()


@pytest.mark.parametrize("command", ["serve", "worker"])
@pytest.mark.parametrize("make_dir", BAD_DIRS, ids=[f.__name__ for f in BAD_DIRS])
def test_cli_exit_1_on_registry_error(tmp_path, make_dir, command):
    cfg = config_file(tmp_path, make_dir(tmp_path))
    called = []
    err = io.StringIO()
    code = cli.main(["--config", str(cfg), command], env={}, stderr=err, serve_runner=lambda *a, **k: called.append(1))
    assert code == 1 and not called
    assert "기동 실패" in err.getvalue()


def test_cli_bad_config_file_exit_1(tmp_path):
    err = io.StringIO()
    assert cli.main(["--config", str(tmp_path / "nope.yaml"), "serve"], env={}, stderr=err, serve_runner=lambda *a, **k: None) == 1


def test_repo_registry_starts_cleanly(tmp_path, monkeypatch):
    """저장소의 실제 config/plugins.d 는 기동에 성공한다."""
    cfg = make_config(tmp_path)
    assert str(PLUGINS_DIR) in cfg.get("registry.paths")
    p = config_file(tmp_path, PLUGINS_DIR)
    monkeypatch.setenv("MATOBSERVER_CONFIG", str(p))
    app = app_factory()
    assert "bending" in app.state.ctx.registry.names("test_spec")


def test_disabled_broken_entry_does_not_block_startup(tmp_path, monkeypatch):
    d = write_plugins(tmp_path / "dis", ("a.yaml", [GOOD, dict(GOOD, name="ghost", enabled=False, **{"class": "no_such_module_xyz:Thing"})]))
    monkeypatch.setenv("MATOBSERVER_CONFIG", str(config_file(tmp_path, d)))
    assert app_factory().state.ctx.registry.names("test_spec") == ["bending"]

