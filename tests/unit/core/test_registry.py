"""(f) 레지스트리 로드와 잘못된 항목 거절."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from matobserver.core.config import Config
from matobserver.core.errors import RegistryError, UnknownPluginError
from matobserver.core.registry import PLUGIN_TYPES, Registry, required_members

REPO = Path(__file__).resolve().parents[3]

PLUGIN_MODULE = '''
class FakeOptimizer:
    name = "fake_opt"
    version = "1.0.0"
    max_batch = 1
    def __init__(self, options):
        self.options = dict(options)
    def setup(self, space, seed, initial_points): ...
    def ask(self, n=1): ...
    def tell(self, thetas, outcomes): ...
    def suggest_stop(self): ...
    def best(self): ...
    def state(self): ...
    def restore(self, state): ...

class WrongName(FakeOptimizer):
    name = "something_else"

class WrongVersion(FakeOptimizer):
    version = "9.9.9"

class MissingAsk:
    name = "fake_opt"
    version = "1.0.0"
    max_batch = 1
    def __init__(self, options): ...
    def setup(self, space, seed, initial_points): ...

class NoMaxBatch(FakeOptimizer):
    max_batch = None
    name = "fake_opt"

class BadCtor(FakeOptimizer):
    def __init__(self): ...

class FakeMat:
    name = "fake_mat"
    version = "1.0.0"
    def __init__(self, options):
        self.options = dict(options)
    def bind(self, reference): ...
    def param_space(self): ...
    def to_neutral(self, theta_local): ...
    def capabilities(self): ...
    def initial_guess(self, hints): ...
    def describe(self, theta_local): ...

class FakeMesher:
    name = "box"
    version = "1.0.0"
    role = "specimen"
    def __init__(self, options): ...
    def validate(self, spec): ...
    def mesh(self, spec, opts, out_dir): ...

class BadRoleMesher(FakeMesher):
    role = "tool"

not_a_class = 3
'''


@pytest.fixture
def plugin_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    modname = "mo_fake_plugins_for_registry_test"
    (tmp_path / f"{modname}.py").write_text(textwrap.dedent(PLUGIN_MODULE), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(modname, None)
    yield modname
    sys.modules.pop(modname, None)


def write(dirpath: Path, filename: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / filename
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def entry_yaml(mod: str, *, type_="optimizer", name="fake_opt", cls="FakeOptimizer", version="1.0.0",
               extra: tuple[str, ...] = ()) -> str:
    lines = [
        'schema_version: "1.0"',
        "plugins:",
        f"  - type: {type_}",
        f"    name: {name}",
        f'    class: "{mod}:{cls}"',
        f'    version: "{version}"',
        *(f"    {line}" for line in extra),
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 저장소의 실제 plugins.d
def test_repo_plugins_dir_loads_empty_skeleton():
    reg = Registry.from_paths([REPO / "config" / "plugins.d"])
    assert len(reg) >= 0  # 소유자들이 항목을 추가해 가므로 개수는 고정하지 않는다
    for f in ("mesh.yaml", "solver.yaml", "optim.yaml"):
        assert (REPO / "config" / "plugins.d" / f).read_text(encoding="utf-8").lstrip().startswith("#")


def test_from_config_uses_registry_paths():
    reg = Registry.from_config(Config(), base_dir=REPO)
    assert isinstance(reg, Registry)


# ------------------------------------------------------------------ 정상 로드
def test_load_and_create(tmp_path, plugin_module):
    write(tmp_path / "plugins.d", "optim.yaml", entry_yaml(plugin_module, extra=("defaults: {a: 1, b: 2}",)))
    reg = Registry.from_paths([tmp_path / "plugins.d"])
    assert reg.names("optimizer") == ["fake_opt"]
    inst = reg.create("optimizer", "fake_opt", {"b": 5, "c": 6})
    assert inst.options == {"a": 1, "b": 5, "c": 6}  # defaults <- 요청 options
    assert reg.versions() == {"optimizer": {"fake_opt": "1.0.0"}}
    assert reg.plugin_class("optimizer", "fake_opt").__name__ == "FakeOptimizer"


def test_files_merged_in_name_order(tmp_path, plugin_module):
    d = tmp_path / "plugins.d"
    write(d, "b.yaml", entry_yaml(plugin_module, type_="material_model", name="fake_mat", cls="FakeMat"))
    write(d, "a.yaml", entry_yaml(plugin_module))
    write(d, "c.yaml", "# comment only\n")          # 주석뿐인 빈 파일 허용
    write(d, "d.yaml", 'schema_version: "1.0"\nplugins: []\n')
    write(d, "ignored.txt", "not yaml: [")
    reg = Registry.from_paths([d])
    assert [(e.type, e.name) for e in reg.entries()] == [("optimizer", "fake_opt"), ("material_model", "fake_mat")]


def test_aliases_resolve_by_when(tmp_path, plugin_module):
    body = f'''
    schema_version: "1.0"
    plugins:
      - type: material_model
        name: fake_mat
        class: "{plugin_module}:FakeMat"
        version: "1.0.0"
        aliases:
          - name: fakealias
            when: {{plasticity_mode: table}}
    '''
    write(tmp_path, "m.yaml", body)
    reg = Registry.from_paths([tmp_path])
    assert reg.resolve_name("material_model", "fakealias", {"plasticity_mode": "table"}) == "fake_mat"
    with pytest.raises(UnknownPluginError):
        reg.resolve_name("material_model", "fakealias", {"plasticity_mode": "coeff"})
    with pytest.raises(UnknownPluginError):
        reg.resolve_name("material_model", "fakealias")
    assert isinstance(reg.create("material_model", "fakealias", context={"plasticity_mode": "table"}).options, dict)


def test_disabled_loaded_but_not_selectable(tmp_path, plugin_module):
    write(tmp_path, "o.yaml", entry_yaml(plugin_module, extra=("enabled: false",)))
    reg = Registry.from_paths([tmp_path])
    assert reg.names("optimizer") == []                      # enabled 만
    assert reg.names("optimizer", enabled_only=False) == ["fake_opt"]
    with pytest.raises(UnknownPluginError) as ei:
        reg.create("optimizer", "fake_opt")
    assert ei.value.code == "OPTIMIZER_UNKNOWN" and ei.value.disabled


def test_disabled_entry_with_missing_class_does_not_fail_startup(tmp_path):
    write(tmp_path, "o.yaml", '''
    schema_version: "1.0"
    plugins:
      - {type: optimizer, name: later, class: "not_yet_written:X", version: "0.0.1", enabled: false}
    ''')
    Registry.from_paths([tmp_path])  # 예외 없음


def test_unknown_name_codes(tmp_path):
    write(tmp_path, "e.yaml", 'schema_version: "1.0"\nplugins: []\n')
    reg = Registry.from_paths([tmp_path])
    for ptype, code in (("optimizer", "OPTIMIZER_UNKNOWN"), ("solver", "SOLVER_UNKNOWN"),
                        ("material_model", "MATERIAL_MODEL_UNKNOWN"), ("test_spec", "TEST_TYPE_UNKNOWN")):
        with pytest.raises(UnknownPluginError) as ei:
            reg.create(ptype, "nope")
        assert ei.value.code == code


# ------------------------------------------------------------------ 잘못된 항목 거절 (기동 실패)
def test_duplicate_type_name_rejected(tmp_path, plugin_module):
    write(tmp_path, "a.yaml", entry_yaml(plugin_module))
    write(tmp_path, "b.yaml", entry_yaml(plugin_module))
    with pytest.raises(RegistryError, match="duplicate plugin optimizer/fake_opt"):
        Registry.from_paths([tmp_path])


def test_same_name_different_type_is_allowed(tmp_path, plugin_module):
    write(tmp_path, "a.yaml", entry_yaml(plugin_module, name="box"))
    write(tmp_path, "b.yaml", entry_yaml(plugin_module, type_="geometry_mesher", name="box", cls="FakeMesher"))
    assert len(Registry.from_paths([tmp_path], check_classes=False)) == 2


@pytest.mark.parametrize("cls,msg", [
    ("WrongName", "class name"),
    ("WrongVersion", "class version"),
    ("MissingAsk", "missing method 'ask'"),
    ("NoMaxBatch", "max_batch"),
    ("BadCtor", "constructor"),
    ("not_a_class", "not a class"),
    ("DoesNotExist", "no attribute"),
])
def test_class_mismatch_rejected(tmp_path, plugin_module, cls, msg):
    write(tmp_path, "o.yaml", entry_yaml(plugin_module, cls=cls))
    with pytest.raises(RegistryError, match=msg):
        Registry.from_paths([tmp_path])


def test_class_mismatch_not_checked_when_disabled_or_skipped(tmp_path, plugin_module):
    write(tmp_path, "o.yaml", entry_yaml(plugin_module, cls="WrongName"))
    reg = Registry.from_paths([tmp_path], check_classes=False)  # 구조 검사만
    with pytest.raises(RegistryError):
        reg.check_classes()
    with pytest.raises(RegistryError):
        reg.create("optimizer", "fake_opt")


def test_import_failure_rejected(tmp_path):
    write(tmp_path, "o.yaml", entry_yaml("no_such_module_xyz"))
    with pytest.raises(RegistryError, match="cannot import"):
        Registry.from_paths([tmp_path])


def test_role_of_mesher_validated(tmp_path, plugin_module):
    write(tmp_path, "m.yaml", entry_yaml(plugin_module, type_="geometry_mesher", name="box", cls="BadRoleMesher"))
    with pytest.raises(RegistryError, match="role"):
        Registry.from_paths([tmp_path])


def test_wrong_protocol_for_type(tmp_path, plugin_module):
    # material 클래스를 optimizer 로 등록
    write(tmp_path, "m.yaml", entry_yaml(plugin_module, name="fake_mat", cls="FakeMat"))
    with pytest.raises(RegistryError, match="missing"):
        Registry.from_paths([tmp_path])


@pytest.mark.parametrize("body,msg", [
    ("- just a list\n", "top level"),
    ('plugins: []\n', "schema_version"),
    ('schema_version: "2.0"\nplugins: []\n', "unsupported schema_version"),
    ('schema_version: "1.1"\nplugins: []\n', "unsupported schema_version"),
    ('schema_version: 1.0\nplugins: []\n', "schema_version"),            # 숫자 1.0 (문자열 아님)
    ('schema_version: "1.0"\nplugins: {}\n', "must be a list"),
    ('schema_version: "1.0"\nextra: 1\nplugins: []\n', "unknown top-level"),
    ('schema_version: "1.0"\nplugins: [3]\n', "must be a mapping"),
    ('schema_version: "1.0"\nplugins:\n  - {type: nope, name: abc, class: "m:C", version: "1.0.0"}\n', "type must be"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: "A", class: "m:C", version: "1.0.0"}\n', "name must match"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: "x", class: "m:C", version: "1.0.0"}\n', "name must match"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "mC", version: "1.0.0"}\n', "class must look like"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C", version: "1.0"}\n', "SemVer"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C", version: "1.0.0", enabled: "yes"}\n', "enabled must be a bool"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C"}\n', "missing required key 'version'"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C", version: "1.0.0", bogus: 1}\n', "unknown keys"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C", version: "1.0.0", defaults: [1]}\n', "defaults must be a mapping"),
    ('schema_version: "1.0"\nplugins:\n  - {type: optimizer, name: abc, class: "m:C", version: "1.0.0", aliases: [{when: {}}]}\n', "aliases"),
    ('schema_version: "1.0"\nplugins: [\n', "invalid YAML"),
])
def test_malformed_entries_rejected(tmp_path, body, msg):
    write(tmp_path, "bad.yaml", body)
    with pytest.raises(RegistryError, match=msg):
        Registry.from_paths([tmp_path], check_classes=False)


def test_alias_collisions_rejected(tmp_path, plugin_module):
    # 별칭이 실제 플러그인 이름과 충돌
    write(tmp_path, "a.yaml", f'''
    schema_version: "1.0"
    plugins:
      - {{type: material_model, name: fake_mat, class: "{plugin_module}:FakeMat", version: "1.0.0"}}
      - {{type: material_model, name: other_mat, class: "{plugin_module}:FakeMat", version: "1.0.0", aliases: [{{name: fake_mat}}]}}
    ''')
    with pytest.raises(RegistryError, match="collides"):
        Registry.from_paths([tmp_path], check_classes=False)


def test_duplicate_alias_same_when_rejected(tmp_path, plugin_module):
    write(tmp_path, "a.yaml", f'''
    schema_version: "1.0"
    plugins:
      - {{type: material_model, name: m_one, class: "{plugin_module}:FakeMat", version: "1.0.0", aliases: [{{name: mat, when: {{k: a}}}}]}}
      - {{type: material_model, name: m_two, class: "{plugin_module}:FakeMat", version: "1.0.0", aliases: [{{name: mat, when: {{k: a}}}}]}}
    ''')
    with pytest.raises(RegistryError, match="duplicate alias"):
        Registry.from_paths([tmp_path], check_classes=False)


def test_missing_registry_path_rejected(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        Registry.from_paths([tmp_path / "nope"])


def test_unknown_plugin_type_argument():
    reg = Registry([], check_classes=False)
    with pytest.raises(ValueError):
        reg.entry("bogus", "x")


# ------------------------------------------------------------------ Protocol 필수 멤버 도출
def test_required_members_derived_from_protocols():
    attrs, methods = required_members(__import__("matobserver.core.types", fromlist=["x"]).Optimizer)
    assert set(attrs) == {"name", "version", "max_batch"}
    assert set(methods) == {"setup", "ask", "tell", "suggest_stop", "best", "state", "restore"}
    assert set(PLUGIN_TYPES) == {"optimizer", "solver", "material_model", "geometry_mesher", "test_spec"}
