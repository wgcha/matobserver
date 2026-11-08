"""플러그인 레지스트리: `config/plugins.d/*.yaml` 로더·검사 (plugins.md §2).

* 위치: 설정 `registry.paths`(기본 `["config/plugins.d"]`) 아래 `*.yaml`을 이름순으로 모두 읽어 합친다.
* 같은 (`type`, `name`)이 두 번 나오면 기동 실패(`RegistryError`).
* 로딩 규칙: `class`를 import하고 해당 Protocol 필수 속성·메서드가 있는지 검사(클래스 속성 `name`, `version`이 YAML과 일치).
  불일치 시 기동 실패. `enabled: false`는 로드(구조 검사)만 하고 import 검사는 하지 않으며, 요청에서 고르면 `UnknownPluginError`
  (→ 400 `*_UNKNOWN`). 생성자 시그니처는 `Cls(options: Mapping[str, Any])`; 옵션 병합 = `defaults` ← 요청 `options`(1단계 병합).
* 엔진은 플러그인 구현 모듈을 import 하지 않고 이 레지스트리로만 얻는다.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

import yaml

from .config import safe_load_yaml
from .errors import RegistryError, UnknownPluginError
from .types import GeometryMesher, MaterialModel, Optimizer, SolverAdapter, TestSpec

SUPPORTED_SCHEMA_MAJOR: Final = 1
SUPPORTED_SCHEMA_MINOR: Final = 0

PLUGIN_TYPES: Final[tuple[str, ...]] = ("optimizer", "solver", "material_model", "geometry_mesher", "test_spec")
PROTOCOL_BY_TYPE: Final[Mapping[str, type]] = {
    "optimizer": Optimizer,
    "solver": SolverAdapter,
    "material_model": MaterialModel,
    "geometry_mesher": GeometryMesher,
    "test_spec": TestSpec,
}

NAME_RE: Final = re.compile(r"^[a-z0-9_.]{2,40}$")
CLASS_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
SEMVER_RE: Final = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_ENTRY_KEYS: Final = frozenset({"type", "name", "class", "version", "enabled", "aliases", "defaults"})
_ALIAS_KEYS: Final = frozenset({"name", "when"})


@dataclass(frozen=True)
class PluginAlias:
    name: str
    when: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginEntry:
    type: str
    name: str
    class_path: str  # YAML `class`: "모듈:클래스"
    version: str
    enabled: bool = True
    aliases: tuple[PluginAlias, ...] = ()
    defaults: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""  # 항목이 적힌 파일 (진단용)


def required_members(proto: type) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Protocol 의 필수 (데이터 속성, 메서드) 이름. `__init__`은 별도 검사하므로 제외한다."""
    attrs: list[str] = []
    methods: list[str] = []
    for klass in reversed(proto.__mro__):
        if klass is object or klass.__name__ in {"Protocol", "Generic"}:
            continue
        for ann in inspect.get_annotations(klass):
            if not ann.startswith("_") and ann not in attrs:
                attrs.append(ann)
        for name, value in klass.__dict__.items():
            if name.startswith("_") or name in attrs:
                continue
            if callable(value) and name not in methods:
                methods.append(name)
    return tuple(attrs), tuple(methods)


def check_plugin_class(entry: PluginEntry, cls: Any) -> list[str]:
    """클래스가 해당 type 의 Protocol 형태와 YAML 의 name/version 을 만족하는지 검사. 문제 설명 목록(비면 통과)."""
    problems: list[str] = []
    if not inspect.isclass(cls):
        return [f"{entry.class_path} is not a class"]
    attrs, methods = required_members(PROTOCOL_BY_TYPE[entry.type])
    for a in attrs:
        if not hasattr(cls, a):
            problems.append(f"missing attribute {a!r}")
    for m in methods:
        if not callable(getattr(cls, m, None)):
            problems.append(f"missing method {m!r}")
    missing = object()  # 속성이 있으나 값이 None 인 경우도 잡기 위한 표지
    cls_name = getattr(cls, "name", missing)
    if cls_name is not missing and cls_name != entry.name:
        problems.append(f"class name {cls_name!r} != registry name {entry.name!r}")
    cls_version = getattr(cls, "version", missing)
    if cls_version is not missing and cls_version != entry.version:
        problems.append(f"class version {cls_version!r} != registry version {entry.version!r}")
    if entry.type == "geometry_mesher":
        role = getattr(cls, "role", missing)
        if role is not missing and role not in ("specimen", "jig"):
            problems.append(f"role must be 'specimen' or 'jig', got {role!r}")
    if entry.type == "optimizer":
        mb = getattr(cls, "max_batch", missing)
        if mb is not missing and (not isinstance(mb, int) or isinstance(mb, bool) or mb < 1):
            problems.append(f"max_batch must be a positive int, got {mb!r}")
    try:
        inspect.signature(cls).bind({})
    except TypeError as e:
        problems.append(f"constructor must accept (options): {e}")
    except ValueError:  # 시그니처를 알 수 없는 내장형 등
        pass
    return problems


def _parse_entry(raw: Any, source: str, index: int) -> PluginEntry:
    where = f"{source}: plugins[{index}]"
    if not isinstance(raw, Mapping):
        raise RegistryError(f"{where}: entry must be a mapping")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise RegistryError(f"{where}: unknown keys {sorted(unknown)}")
    for req in ("type", "name", "class", "version"):
        if req not in raw:
            raise RegistryError(f"{where}: missing required key {req!r}")
    ptype, name, cls_path, version = raw["type"], raw["name"], raw["class"], raw["version"]
    if ptype not in PLUGIN_TYPES:
        raise RegistryError(f"{where}: type must be one of {list(PLUGIN_TYPES)}, got {ptype!r}")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise RegistryError(f"{where}: name must match {NAME_RE.pattern}, got {name!r}")
    where = f"{source}: {ptype}/{name}"
    if not isinstance(cls_path, str) or not CLASS_RE.match(cls_path):
        raise RegistryError(f"{where}: class must look like 'package.module:ClassName', got {cls_path!r}")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        raise RegistryError(f"{where}: version must be a SemVer string, got {version!r}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegistryError(f"{where}: enabled must be a bool, got {enabled!r}")
    defaults = raw.get("defaults", {}) or {}
    if not isinstance(defaults, Mapping):
        raise RegistryError(f"{where}: defaults must be a mapping")
    aliases_raw = raw.get("aliases", []) or []
    if not isinstance(aliases_raw, list):
        raise RegistryError(f"{where}: aliases must be a list")
    aliases: list[PluginAlias] = []
    for j, a in enumerate(aliases_raw):
        if not isinstance(a, Mapping) or set(a) - _ALIAS_KEYS or "name" not in a:
            raise RegistryError(f"{where}: aliases[{j}] must be a mapping with 'name' and optional 'when'")
        if not isinstance(a["name"], str) or not NAME_RE.match(a["name"]):
            raise RegistryError(f"{where}: aliases[{j}].name must match {NAME_RE.pattern}, got {a['name']!r}")
        when = a.get("when", {}) or {}
        if not isinstance(when, Mapping):
            raise RegistryError(f"{where}: aliases[{j}].when must be a mapping")
        aliases.append(PluginAlias(name=a["name"], when=dict(when)))
    return PluginEntry(
        type=ptype, name=name, class_path=cls_path, version=version, enabled=enabled,
        aliases=tuple(aliases), defaults=dict(defaults), source=source,
    )


def parse_plugin_file(path: Path) -> list[PluginEntry]:
    """plugins.d 파일 1개를 읽어 항목 목록으로. 비어 있거나 주석뿐인 파일은 항목 0개."""
    try:
        data = safe_load_yaml(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryError(f"{path}: invalid YAML: {e}") from e
    if data is None:
        return []
    if not isinstance(data, Mapping):
        raise RegistryError(f"{path}: top level must be a mapping")
    unknown = set(data) - {"schema_version", "plugins"}
    if unknown:
        raise RegistryError(f"{path}: unknown top-level keys {sorted(unknown)}")
    sv = data.get("schema_version")
    if not isinstance(sv, str) or not re.match(r"^\d+\.\d+$", sv):
        raise RegistryError(f"{path}: schema_version must be a string like \"1.0\", got {sv!r}")
    major, minor = (int(p) for p in sv.split("."))
    if major != SUPPORTED_SCHEMA_MAJOR or minor > SUPPORTED_SCHEMA_MINOR:
        raise RegistryError(f"{path}: unsupported schema_version {sv!r}")
    plugins = data.get("plugins")
    if plugins is None:
        return []
    if not isinstance(plugins, list):
        raise RegistryError(f"{path}: 'plugins' must be a list")
    return [_parse_entry(raw, str(path), i) for i, raw in enumerate(plugins)]


class Registry:
    """읽기 전용 플러그인 레지스트리."""

    def __init__(self, entries: Iterable[PluginEntry], *, check_classes: bool = True) -> None:
        self._entries: dict[tuple[str, str], PluginEntry] = {}
        for e in entries:
            key = (e.type, e.name)
            if key in self._entries:
                raise RegistryError(
                    f"duplicate plugin {e.type}/{e.name}: {self._entries[key].source} and {e.source}"
                )
            self._entries[key] = e
        self._check_aliases()
        self._classes: dict[tuple[str, str], type] = {}
        if check_classes:
            self.check_classes()

    # ---- 생성 ----
    @classmethod
    def from_paths(cls, paths: Sequence[str | os.PathLike[str]], *, check_classes: bool = True) -> Registry:
        """폴더(또는 yaml 파일) 목록에서 읽는다. 폴더는 `*.yaml`을 이름순으로 모두 읽는다."""
        entries: list[PluginEntry] = []
        for p in paths:
            base = Path(p)
            if base.is_dir():
                files = sorted(base.glob("*.yaml"), key=lambda f: f.name)
            elif base.is_file():
                files = [base]
            else:
                raise RegistryError(f"registry path not found: {base}")
            for f in files:
                entries.extend(parse_plugin_file(f))
        return cls(entries, check_classes=check_classes)

    @classmethod
    def from_config(cls, config: Any, *, base_dir: str | os.PathLike[str] | None = None,
                    check_classes: bool = True) -> Registry:
        """`registry.paths` 설정으로 로드. 상대 경로는 base_dir(없으면 현재 작업 폴더) 기준."""
        raw_paths = config.get("registry.paths")
        root = Path(base_dir) if base_dir is not None else None
        resolved = [(root / p if root is not None and not Path(p).is_absolute() else Path(p)) for p in raw_paths]
        return cls.from_paths(resolved, check_classes=check_classes)

    # ---- 검사 ----
    def _check_aliases(self) -> None:
        real = {(e.type, e.name) for e in self._entries.values()}
        seen: dict[tuple[str, str, str], str] = {}
        for e in self._entries.values():
            for a in e.aliases:
                if (e.type, a.name) in real:
                    raise RegistryError(f"alias {e.type}/{a.name} of {e.name} collides with a real plugin name")
                k = (e.type, a.name, repr(sorted(a.when.items())))
                if k in seen:
                    raise RegistryError(
                        f"duplicate alias {e.type}/{a.name} with when={dict(a.when)}: {seen[k]} and {e.name}"
                    )
                seen[k] = e.name

    def check_classes(self) -> None:
        """enabled 항목 전부를 import 해 Protocol 형태·name·version 을 검사한다. 하나라도 실패하면 RegistryError."""
        problems: list[str] = []
        for entry in self._entries.values():
            if not entry.enabled:
                continue
            try:
                self.plugin_class(entry.type, entry.name)
            except RegistryError as e:
                problems.append(str(e))
        if problems:
            raise RegistryError("plugin class check failed:\n  " + "\n  ".join(problems))

    # ---- 조회 ----
    def entries(self, plugin_type: str | None = None, *, enabled_only: bool = False) -> list[PluginEntry]:
        out = [e for e in self._entries.values()
               if (plugin_type is None or e.type == plugin_type) and (e.enabled or not enabled_only)]
        return sorted(out, key=lambda e: (PLUGIN_TYPES.index(e.type), e.name))

    def names(self, plugin_type: str, *, enabled_only: bool = True) -> list[str]:
        return [e.name for e in self.entries(plugin_type, enabled_only=enabled_only)]

    def __contains__(self, item: object) -> bool:
        return item in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, plugin_type: str, name: str, context: Mapping[str, Any] | None = None) -> PluginEntry:
        """이름(또는 별칭)으로 항목을 찾는다. 없으면 `UnknownPluginError`(활성 여부는 호출자가 확인)."""
        if plugin_type not in PLUGIN_TYPES:
            raise ValueError(f"unknown plugin type {plugin_type!r}")
        found = self._entries.get((plugin_type, name))
        if found is not None:
            return found
        ctx = context or {}
        for e in self.entries(plugin_type):
            for a in e.aliases:
                if a.name == name and all(k in ctx and ctx[k] == v for k, v in a.when.items()):
                    return e
        raise UnknownPluginError(f"unknown {plugin_type} {name!r}", plugin_type=plugin_type, name=name)

    def resolve_name(self, plugin_type: str, name: str, context: Mapping[str, Any] | None = None) -> str:
        """별칭을 실제 이름으로 (`mat024` + {plasticity_mode: table} → `mat024_table`)."""
        return self.entry(plugin_type, name, context).name

    def enabled_entry(self, plugin_type: str, name: str, context: Mapping[str, Any] | None = None) -> PluginEntry:
        e = self.entry(plugin_type, name, context)
        if not e.enabled:
            raise UnknownPluginError(
                f"{plugin_type} {e.name!r} is disabled", plugin_type=plugin_type, name=name, disabled=True
            )
        return e

    # ---- 인스턴스화 ----
    def plugin_class(self, plugin_type: str, name: str) -> type:
        """클래스를 import 하고(캐시) 형태를 검사해 돌려준다. 실패는 RegistryError."""
        key = (plugin_type, name)
        cached = self._classes.get(key)
        if cached is not None:
            return cached
        entry = self._entries[key]
        module_name, _, cls_name = entry.class_path.partition(":")
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            raise RegistryError(f"{entry.source}: {plugin_type}/{name}: cannot import {module_name!r}: {e!r}") from e
        obj: Any = module
        try:
            for part in cls_name.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            raise RegistryError(
                f"{entry.source}: {plugin_type}/{name}: {module_name!r} has no attribute {cls_name!r}"
            ) from None
        problems = check_plugin_class(entry, obj)
        if problems:
            raise RegistryError(f"{entry.source}: {plugin_type}/{name} ({entry.class_path}): " + "; ".join(problems))
        self._classes[key] = obj
        return obj

    def merged_options(self, plugin_type: str, name: str, options: Mapping[str, Any] | None = None,
                       context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """`defaults` ← 요청 `options` (1단계 병합)."""
        entry = self.enabled_entry(plugin_type, name, context)
        merged = dict(entry.defaults)
        merged.update(options or {})
        return merged

    def create(self, plugin_type: str, name: str, options: Mapping[str, Any] | None = None, *,
               context: Mapping[str, Any] | None = None) -> Any:
        """플러그인 인스턴스를 만든다: `Cls(merged_options)`. 별칭은 context 로 해석."""
        entry = self.enabled_entry(plugin_type, name, context)
        cls = self.plugin_class(entry.type, entry.name)
        return cls(self.merged_options(entry.type, entry.name, options))

    def versions(self) -> dict[str, dict[str, str]]:
        """type → {name: version} (enabled 항목). 결과 traceability.plugins 후보."""
        out: dict[str, dict[str, str]] = {}
        for e in self.entries(enabled_only=True):
            out.setdefault(e.type, {})[e.name] = e.version
        return out
