"""레시피 정적 검사 (recipe.md §7.1) — 동기, 등록 요청·CLI 로컬 검사에서 같은 코드를 쓴다.

입력은 "폴더"(압축을 푼 스테이징 폴더 또는 CLI 입력 폴더)다. 오류는 한꺼번에 모아 돌려준다.
매니페스트 스키마가 깨지면(`RECIPE_MANIFEST_INVALID`) 그 뒤 의존 검사는 의미가 없어 건너뛴다.
레지스트리를 주면 `RECIPE_TEST_TYPE_UNKNOWN`·`RECIPE_SOLVER_UNKNOWN`·`RECIPE_GENERATOR_UNKNOWN` 도 검사한다.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..core import errors as E
from ..core.config import safe_load_yaml
from ..core.registry import Registry
from ..core.types import neutral_material_violations
from . import kdeck
from .bundle import FileEntry, check_rel_path, scan_tree
from .cards import CardBlocksError, neutral_from_card_blocks
from .manifest import parse_manifest_text

RECIPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,7}$")
UNIT_SYSTEM = "mm-N-s-tonne"
RESERVED_PARAMS = frozenset({"mo_dend", "mo_tend", "mo_dtout", "mo_dtplt", "mo_dtmax", "mo_htop", "mo_hbot", "mo_v0", "mo_mass"})
FORCE_SCALE = {"none": 1.0, "half": 2.0, "quarter": 4.0}
SYM_PLANES = {"none": 0, "half": 1, "quarter": 2}
#: recipe.md §4.4: topology -> 허용 elform (SECTION_SOLID)
ELFORMS = {"hex8": {-2, -1, 1, 2}, "tet10": {16, 17}, "tet4": {10, 13}}
#: recipe.md §3.3: 레시피 파일에 두면 안 되는 출력 키워드(접두)
FORBIDDEN_OUTPUT = (
    "DATABASE_RCFORC", "DATABASE_NODOUT", "DATABASE_GLSTAT", "DATABASE_MATSUM", "DATABASE_BINARY_D3PLOT",
    "DATABASE_HISTORY_NODE",
)
RESERVED_FILE_RE = re.compile(r"^mo_[^/]*\.k$", re.IGNORECASE)
RECIPE_WARN_OUTPUT_INTERVAL = "RECIPE_OUTPUT_INTERVAL_HARDCODED"  # recipe.md §3.3 (경고)


@dataclass(frozen=True)
class StaticIssue:
    code: str
    message: str
    severity: str = "error"  # error | warning
    field: str | None = None

    def to_detail(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            d["field"] = self.field
        return d


@dataclass
class StaticResult:
    manifest: dict[str, Any] | None
    issues: list[StaticIssue] = field(default_factory=list)
    entries: list[FileEntry] = field(default_factory=list)

    @property
    def errors(self) -> list[StaticIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[StaticIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def check_folder(folder: Path, registry: Registry | None = None, *, entries: list[FileEntry] | None = None) -> StaticResult:
    """폴더를 정적 검사한다 (recipe.md §7.1 전 코드)."""
    folder = Path(folder)
    res = StaticResult(manifest=None)
    if entries is None:
        entries, bad = scan_tree(folder)
        for rel, why in bad:
            res.issues.append(StaticIssue(E.RECIPE_PATH_UNSAFE, f"허용되지 않는 항목({why}): {rel}", field=rel))
    res.entries = list(entries)
    files = {e.path for e in entries}
    if "manifest.yaml" not in files:
        res.issues.append(StaticIssue(E.RECIPE_BUNDLE_INVALID, "최상위에 manifest.yaml 이 없습니다.", field="manifest.yaml"))
        return res
    for e in entries:
        why = check_rel_path(e.path)
        if why:
            res.issues.append(StaticIssue(E.RECIPE_PATH_UNSAFE, f"안전하지 않은 경로: {why}", field=e.path))
    manifest, merrs = parse_manifest_text((folder / "manifest.yaml").read_text(encoding="utf-8", errors="replace"))
    res.manifest = manifest
    for me in merrs:
        res.issues.append(StaticIssue(E.RECIPE_MANIFEST_INVALID, me["message"], field=me["field"] or None))
    if merrs or manifest is None:
        return res
    _Checker(folder, files, manifest, registry, res).run()
    return res


class _Checker:
    def __init__(self, folder: Path, files: set[str], m: dict[str, Any], registry: Registry | None, res: StaticResult) -> None:
        self.folder, self.files, self.m, self.registry, self.res = folder, files, m, registry, res
        self.kind: str = m["kind"]
        self.slots: list[dict[str, Any]] = m["material_slots"]
        self.params: list[dict[str, Any]] = m["parameters"]

    def add(self, code: str, message: str, field: str | None = None, severity: str = "error") -> None:
        self.res.issues.append(StaticIssue(code, message, severity, field))

    # ------------------------------------------------------------------ 전체
    def run(self) -> None:
        self.check_header()
        self.check_registry()
        self.check_kind()
        self.check_params()
        self.check_symmetry()
        self.check_compliance()
        self.check_sections()
        self.check_manifest_paths()
        self.check_reference_card()
        self.check_decks()

    # ------------------------------------------------------------------ 헤더·스키마 값 규칙
    def check_header(self) -> None:
        m = self.m
        if not RECIPE_ID_RE.match(m["recipe_id"]):
            self.add(E.RECIPE_MANIFEST_INVALID, f"recipe_id 형식 위반: {m['recipe_id']!r}", "recipe_id")
        if not re.match(r"^1\.\d+$", m["schema_version"]):
            self.add(E.RECIPE_MANIFEST_INVALID, f"schema_version 은 1.x 여야 합니다: {m['schema_version']!r}", "schema_version")
        if m["unit_system"] != UNIT_SYSTEM:
            self.add(E.RECIPE_UNIT_SYSTEM_UNSUPPORTED, f"unit_system 은 {UNIT_SYSTEM!r} 만 지원합니다: {m['unit_system']!r}", "unit_system")
        seen: set[str] = set()
        for i, s in enumerate(self.slots):
            if not SLOT_RE.match(s["slot"]):
                self.add(E.RECIPE_MANIFEST_INVALID, f"슬롯 이름 형식 위반: {s['slot']!r}", f"material_slots[{i}].slot")
            if s["slot"] in seen:
                self.add(E.RECIPE_MANIFEST_INVALID, f"슬롯 이름 중복: {s['slot']!r}", f"material_slots[{i}].slot")
            seen.add(s["slot"])
        keys: set[str] = set()
        for i, r in enumerate(m["responses"]):
            if r["response_key"] in keys:
                self.add(E.RECIPE_MANIFEST_INVALID, f"response_key 중복: {r['response_key']!r}", f"responses[{i}].response_key")
            keys.add(r["response_key"])
        for name, rng in (("specimen.id_ranges.node", m["specimen"]["id_ranges"]["node"]),
                          ("specimen.id_ranges.element", m["specimen"]["id_ranges"]["element"])):
            if rng[0] > rng[1]:
                self.add(E.RECIPE_MANIFEST_INVALID, f"{name} 범위가 뒤집혔습니다.", name)

    def check_registry(self) -> None:
        reg = self.registry
        if reg is None:
            return
        if self.m["test_type"] not in reg.names("test_spec"):
            self.add(E.RECIPE_TEST_TYPE_UNKNOWN, f"등록된 TestSpec 이 아닙니다: {self.m['test_type']!r}", "test_type")
        if self.m["solver"]["name"] not in reg.names("solver"):
            self.add(E.RECIPE_SOLVER_UNKNOWN, f"등록된 솔버 어댑터가 아닙니다: {self.m['solver']['name']!r}", "solver.name")
        jig = self.m.get("jig")
        if jig is not None and jig["generator"] not in reg.names("geometry_mesher"):
            self.add(E.RECIPE_GENERATOR_UNKNOWN, f"등록된 지그 생성기가 아닙니다: {jig['generator']!r}", "jig.generator")

    def check_kind(self) -> None:
        if self.kind == "parametric" and self.m.get("jig") is None:
            self.add(E.RECIPE_KIND_MISMATCH, "kind=parametric 인데 jig 가 없습니다.", "jig")
        if self.kind == "fixed" and self.m.get("jig") is not None:
            self.add(E.RECIPE_KIND_MISMATCH, "kind=fixed 인데 jig 가 있습니다 (지그는 deck.files 에 포함).", "jig")

    def check_params(self) -> None:
        seen: set[str] = set()
        for i, p in enumerate(self.params):
            f = f"parameters[{i}]"
            name = p["name"]
            if not PARAM_RE.match(name):
                self.add(E.RECIPE_PARAM_NAME_INVALID, f"파라미터 이름 규칙 위반(^[a-z][a-z0-9_]{{0,7}}$): {name!r}", f"{f}.name")
            elif name.startswith("mo_"):
                self.add(E.RECIPE_PARAM_NAME_INVALID, f"mo_ 접두사는 엔진 예약입니다: {name!r}", f"{f}.name")
            if name in seen:
                self.add(E.RECIPE_PARAM_NAME_INVALID, f"파라미터 이름 중복: {name!r}", f"{f}.name")
            seen.add(name)
            lo, hi = p["range"]
            if lo > hi:
                self.add(E.RECIPE_PARAM_NAME_INVALID, f"range 가 뒤집혔습니다: {p['range']}", f"{f}.range")
            elif not (lo <= p["default"] <= hi):
                self.add(E.RECIPE_PARAM_NAME_INVALID, f"default {p['default']} 가 range {p['range']} 밖입니다.", f"{f}.default")

    def check_symmetry(self) -> None:
        sym = self.m["symmetry"]
        t = sym["type"]
        if float(sym["force_scale"]) != FORCE_SCALE[t]:
            self.add(E.RECIPE_SYMMETRY_INVALID, f"force_scale 은 type={t} 일 때 {FORCE_SCALE[t]:g} 여야 합니다: {sym['force_scale']}",
                     "symmetry.force_scale")
        planes = sym.get("planes", [])
        if len(planes) != SYM_PLANES[t]:
            self.add(E.RECIPE_SYMMETRY_INVALID, f"type={t} 의 대칭면은 {SYM_PLANES[t]}개여야 합니다: {planes}", "symmetry.planes")
        node_sets = self.m["specimen"].get("node_sets", {})
        for p in planes:
            if p not in ("x0", "y0"):
                self.add(E.RECIPE_SYMMETRY_INVALID, f"알 수 없는 대칭면: {p!r}", "symmetry.planes")
            elif f"sym_{p}" not in node_sets:
                self.add(E.RECIPE_SYMMETRY_INVALID, f"대칭면 {p} 의 노드 세트 ID(specimen.node_sets.sym_{p}) 가 없습니다.",
                         f"specimen.node_sets.sym_{p}")

    def check_compliance(self) -> None:
        c = self.m.get("compliance")
        if c is not None and c.get("value_m_per_n") is not None and not c["value_m_per_n"] > 0:
            self.add(E.RECIPE_COMPLIANCE_INVALID, f"compliance.value_m_per_n 은 0 보다 커야 합니다: {c['value_m_per_n']}",
                     "compliance.value_m_per_n")

    def check_sections(self) -> None:
        topo = self.m["specimen"]["element"]["topology"]
        for i, s in enumerate(self.slots):
            sec = s["section"]
            f = f"material_slots[{i}].section"
            if sec["keyword"] not in ("SECTION_SOLID", "SECTION_SHELL"):
                self.add(E.RECIPE_SECTION_INVALID, f"지원하지 않는 섹션 키워드: {sec['keyword']!r}", f"{f}.keyword")
            elif sec["keyword"] == "SECTION_SOLID" and topo in ELFORMS and sec["elform"] not in ELFORMS[topo]:
                self.add(E.RECIPE_SECTION_INVALID,
                         f"요소 {topo} 에 elform {sec['elform']} 은 쓸 수 없습니다 (허용: {sorted(ELFORMS[topo])}).", f"{f}.elform")

    # ------------------------------------------------------------------ 파일 참조
    def _declared_paths(self) -> list[tuple[str, str, bool]]:
        """(필드, 경로, 필수) — 모드에 따라 필수 제어 파일이 달라진다."""
        m = self.m
        out: list[tuple[str, str | None, bool]] = [("deck.main", m["deck"]["main"], True)]
        an = m["solver"]["analysis"]
        needed = {an["primary"]} | ({an["fallback"]} if an["fallback"] != "none" else set())
        ctrl = m["deck"]["controls"]
        for mode in ("implicit", "explicit"):
            out.append((f"deck.controls.{mode}", ctrl.get(mode), mode in needed))
        for i, p in enumerate(m["deck"].get("files", [])):
            out.append((f"deck.files[{i}]", p, True))
        out.append(("verification.reference_card", m["verification"]["reference_card"], True))
        out.append(("verification.reference_curve", m["verification"].get("reference_curve"), False))
        return out  # type: ignore[return-value]

    def check_manifest_paths(self) -> None:
        for fld, path, required in self._declared_paths():
            if path is None:
                if required:
                    self.add(E.RECIPE_FILE_MISSING, f"필수 파일이 매니페스트에 선언되지 않았습니다: {fld}", fld)
                continue
            why = check_rel_path(path)
            if why:
                self.add(E.RECIPE_PATH_UNSAFE, f"안전하지 않은 경로({why}): {path}", fld)
            elif path not in self.files:
                self.add(E.RECIPE_FILE_MISSING, f"매니페스트가 가리키는 파일이 없습니다: {path}", fld)

    def check_reference_card(self) -> None:
        path = self.m["verification"]["reference_card"]
        if check_rel_path(path) or path not in self.files:
            return
        try:
            data = safe_load_yaml((self.folder / path).read_text(encoding="utf-8", errors="replace"))
        except yaml.YAMLError as e:
            self.add(E.RECIPE_REFERENCE_CARD_INVALID, f"기준 카드 YAML 오류: {str(e).splitlines()[0]}", path)
            return
        if not isinstance(data, Mapping):
            self.add(E.RECIPE_REFERENCE_CARD_INVALID, "기준 카드는 {슬롯: card_blocks} 매핑이어야 합니다.", path)
            return
        for s in self.slots:
            name = s["slot"]
            if name not in data:
                self.add(E.RECIPE_REFERENCE_CARD_INVALID, f"기준 카드에 슬롯 {name!r} 이 없습니다.", f"{path}:{name}")
                continue
            try:
                nm = neutral_from_card_blocks(data[name])
            except CardBlocksError as e:
                self.add(E.RECIPE_REFERENCE_CARD_INVALID, f"슬롯 {name!r}: {e}", f"{path}:{name}")
                continue
            for v in neutral_material_violations(nm):
                self.add(E.RECIPE_REFERENCE_CARD_INVALID, f"슬롯 {name!r}: {v}", f"{path}:{name}")

    # ------------------------------------------------------------------ 덱 검사
    def _read(self, rel: str) -> str:
        return (self.folder / rel).read_text(encoding="utf-8", errors="replace")

    def check_decks(self) -> None:
        m = self.m
        k_files = sorted(f for f in self.files if f.lower().endswith(".k"))
        for f in k_files:
            if RESERVED_FILE_RE.match(posixpath.basename(f)):
                self.add(E.RECIPE_RESERVED_FILE_PRESENT, f"예약 파일 이름(mo_*.k)은 레시피 폴더에 둘 수 없습니다: {f}", f)
        cards_by_file = {f: kdeck.parse_cards(self._read(f)) for f in k_files}

        include_roots = [m["deck"]["main"], *m["deck"].get("files", []), *[c for c in m["deck"]["controls"].values() if c]]
        for rel in dict.fromkeys(include_roots):
            if rel in cards_by_file:
                self.check_includes(rel, cards_by_file[rel])

        main = m["deck"]["main"]
        if main in cards_by_file:
            self.check_reserved_includes(main, cards_by_file[main])

        # 선언 파일 + 덱 폴더 안의 모든 .k 를 한 덱으로 본다.
        exposed = {p["name"].lower() for p in self.params}
        known = exposed | RESERVED_PARAMS
        deck_defined: set[str] = set()
        for f, cards in cards_by_file.items():
            for c in cards:
                for no, name in kdeck.parameter_defs(c):
                    deck_defined.add(name)
                    if name in known:
                        self.add(E.RECIPE_RESERVED_PARAM_REDEFINED,
                                 f"노출·예약 파라미터 {name!r} 를 덱이 다시 정의합니다 (값은 mo_params.k 에서만 온다).", f"{f}:{no}")
        reported: set[tuple[str, str]] = set()
        for f, cards in cards_by_file.items():
            for c in cards:
                for no, name in kdeck.param_refs(c):
                    if name not in known and name not in deck_defined and (f, name) not in reported:
                        reported.add((f, name))
                        self.add(E.RECIPE_PARAM_UNDECLARED, f"&{name} 가 매니페스트·예약·덱 *PARAMETER 어디에도 선언되지 않았습니다.", f"{f}:{no}")

        self.check_outputs(cards_by_file)
        facts = self.collect_ids(cards_by_file)
        self.check_slot_ids(facts)
        self.check_id_ranges(facts)
        self.check_responses(facts)
        self.check_implicit_dtmax(cards_by_file)

    def resolve_include(self, including: str, target: str) -> str | None:
        """'reserved' | 'ok' | None."""
        t = target.replace("\\", "/")
        base = posixpath.basename(t)
        if RESERVED_FILE_RE.match(base):
            return "reserved"
        if t.startswith("/") or re.match(r"^[A-Za-z]:", t):
            return None
        cands = [posixpath.normpath(posixpath.join(posixpath.dirname(including), t)), posixpath.normpath(t),
                 posixpath.normpath("deck/" + t)]
        for c in cands:
            if not c.startswith("..") and c in self.files:
                return "ok"
        return None

    def check_includes(self, rel: str, cards: list[kdeck.Card]) -> None:
        for c in cards:
            t = kdeck.include_target(c)
            if t is None:
                continue
            if self.resolve_include(rel, t) is None:
                self.add(E.RECIPE_INCLUDE_UNRESOLVED, f"*INCLUDE 대상을 폴더 안에서 찾을 수 없습니다: {t}", f"{rel}:{c.lineno}")

    def check_reserved_includes(self, rel: str, cards: list[kdeck.Card]) -> None:
        targets = [t for c in cards if (t := kdeck.include_target(c)) is not None]
        names = [posixpath.basename(t.replace("\\", "/")).lower() for t in targets]
        required = ["mo_params.k", "mo_specimen.k", "mo_controls.k", "mo_output.k"]
        required += [f"mo_mat_{s['slot']}.k" for s in self.slots]
        if self.kind == "parametric":
            required.append("mo_jig.k")
        missing = [r for r in required if r not in names]
        if missing:
            self.add(E.RECIPE_RESERVED_INCLUDE_MISSING, "main.k 에 필수 예약 include 가 없습니다: " + ", ".join(missing), rel)
        if names and names[0] != "mo_params.k" and "mo_params.k" in names:
            self.add(E.RECIPE_RESERVED_INCLUDE_MISSING, "mo_params.k 는 *KEYWORD 직후 첫 *INCLUDE 여야 합니다.", rel)
        if self.kind == "fixed" and "mo_jig.k" in names:
            self.add(E.RECIPE_KIND_MISMATCH, "kind=fixed 레시피는 mo_jig.k 를 include 할 수 없습니다.", rel)

    def check_outputs(self, cards_by_file: Mapping[str, list[kdeck.Card]]) -> None:
        for f, cards in cards_by_file.items():
            for c in cards:
                if c.keyword.startswith(FORBIDDEN_OUTPUT):
                    self.add(E.RECIPE_OUTPUT_KEYWORD_PRESENT,
                             f"*{c.keyword} 는 엔진이 mo_output.k 로 생성합니다. 레시피 파일에 둘 수 없습니다.", f"{f}:{c.lineno}")
                elif c.keyword.startswith("DATABASE_") and not c.keyword.startswith("DATABASE_EXTENT"):
                    if not any("&mo_dtout" in line.lower() for _, line in c.data):
                        self.add(RECIPE_WARN_OUTPUT_INTERVAL, f"*{c.keyword} 출력 간격은 &mo_dtout 사용을 권장합니다.",
                                 f"{f}:{c.lineno}", severity="warning")

    def collect_ids(self, cards_by_file: Mapping[str, list[kdeck.Card]]) -> "_Facts":
        facts = _Facts()
        for f, cards in cards_by_file.items():
            for c in cards:
                kw = c.keyword
                if kw == "NODE":
                    for no, line in c.data:
                        v = kdeck.first_int(line)
                        if v is not None:
                            facts.nodes[v] = f"{f}:{no}"
                elif kw.startswith("ELEMENT_"):
                    for v in kdeck.element_ids(c):
                        facts.elements[v] = f"{f}:{c.lineno}"
                elif kw.startswith("CONTACT_") and re.search(r"_ID(_|$)", kw):
                    v = kdeck.card_id(c, title_skip=False)
                    if v is not None:
                        facts.contacts.add(v)
                elif kw == "PART":
                    pid, sid, mid = kdeck.part_ids(c)
                    if pid is not None:
                        facts.pids[pid] = f"{f}:{c.lineno}"
                elif kw.startswith("SECTION_"):
                    v = kdeck.card_id(c)
                    if v is not None:
                        facts.secids[v] = f"{f}:{c.lineno}"
                elif kw.startswith("MAT_"):
                    v = kdeck.card_id(c)
                    if v is not None:
                        facts.mids[v] = f"{f}:{c.lineno}"
                elif kw.startswith("DEFINE_CURVE"):
                    v = kdeck.card_id(c)
                    if v is not None:
                        facts.lcids[v] = f"{f}:{c.lineno}"
        return facts

    def check_slot_ids(self, facts: "_Facts") -> None:
        for s in self.slots:
            for label, val, table in (("part_id", s["part_id"], facts.pids), ("mid", s["mid"], facts.mids),
                                      ("secid", s["secid"], facts.secids), ("lcid", s.get("lcid"), facts.lcids)):
                if val is not None and val in table:
                    self.add(E.RECIPE_SLOT_ID_CONFLICT,
                             f"슬롯 {s['slot']!r} 의 {label}={val} 를 덱이 정의합니다 (엔진 생성 파일 소유).", table[val])

    def check_id_ranges(self, facts: "_Facts") -> None:
        spec, jig = self.m["specimen"]["id_ranges"], self.m.get("jig")
        ranges = [("specimen", spec)] + ([("jig", jig["id_ranges"])] if jig else [])
        for kind, table in (("node", facts.nodes), ("element", facts.elements)):
            for owner, rg in ranges:
                lo, hi = rg[kind]
                hit = next((i for i in sorted(table) if lo <= i <= hi), None)
                if hit is not None:
                    self.add(E.RECIPE_ID_RANGE_CONFLICT,
                             f"덱의 {kind} ID {hit} 가 엔진 {owner} 범위 [{lo}, {hi}] 와 겹칩니다.", table[hit])

    def _testspec_refs(self) -> set[str] | None:
        if self.registry is None:
            return None
        try:
            spec = self.registry.create("test_spec", self.m["test_type"], {})
            return {r.name for r in spec.reference_points(self.m)}
        except Exception:  # 플러그인 로드 실패/미등록은 다른 코드가 이미 보고
            return None

    def check_responses(self, facts: "_Facts") -> None:
        jig = self.m.get("jig")
        jig_refs = set(jig["reference_nodes"]) if jig else set()
        spec_refs = self._testspec_refs()
        for i, r in enumerate(self.m["responses"]):
            for fld, node in _walk_extractor(r.get("extractor", {}), f"responses[{i}].extractor"):
                self._check_extractor_node(fld, node, facts, jig_refs, spec_refs)
            for fld, node in _walk_extractor(r.get("alt_axes") or {}, f"responses[{i}].alt_axes"):
                self._check_extractor_node(fld, node, facts, jig_refs, spec_refs)

    def _check_extractor_node(self, fld: str, node: Mapping[str, Any], facts: "_Facts", jig_refs: set[str],
                              spec_refs: set[str] | None) -> None:
        if "contact_id" in node:
            cid = node["contact_id"]
            if not isinstance(cid, int) or cid not in facts.contacts:
                self.add(E.RECIPE_RESPONSE_ID_MISSING, f"contact_id={cid!r} 가 덱의 *CONTACT_*_ID 에 없습니다.", fld)
        ref = node.get("ref")
        if ref is not None and not isinstance(ref, (dict, list)):
            known = set(jig_refs) | (spec_refs if spec_refs is not None else set())
            if spec_refs is not None and ref not in known:
                self.add(E.RECIPE_RESPONSE_ID_MISSING, f"node.ref={ref!r} 가 지그 생성기·TestSpec 참조점에 없습니다.", fld)
        if "id" in node:
            nid = node["id"]
            if not isinstance(nid, int) or nid not in facts.nodes:
                self.add(E.RECIPE_RESPONSE_ID_MISSING, f"node.id={nid!r} 가 덱의 *NODE 에 없습니다.", fld)

    def check_implicit_dtmax(self, cards_by_file: Mapping[str, list[kdeck.Card]]) -> None:
        if self.m["solver"]["analysis"]["primary"] != "implicit":
            return
        path = self.m["deck"]["controls"].get("implicit")
        if not path or path not in cards_by_file:
            return
        for c in cards_by_file[path]:
            if c.keyword == "CONTROL_IMPLICIT_AUTO" and c.data:
                fields = kdeck.split_fields(c.data[0][1])
                if len(fields) > 4 and fields[4].lower() == "&mo_dtmax":
                    return
        self.add(E.RECIPE_IMPLICIT_DTMAX_MISSING,
                 "implicit 제어 파일에 *CONTROL_IMPLICIT_AUTO 의 DTMAX=&mo_dtmax 가 없습니다.", path)


@dataclass
class _Facts:
    nodes: dict[int, str] = field(default_factory=dict)
    elements: dict[int, str] = field(default_factory=dict)
    contacts: set[int] = field(default_factory=set)
    pids: dict[int, str] = field(default_factory=dict)
    secids: dict[int, str] = field(default_factory=dict)
    mids: dict[int, str] = field(default_factory=dict)
    lcids: dict[int, str] = field(default_factory=dict)


def _walk_extractor(obj: Any, path: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """추출기 정의에서 `contact_id`/`node`/`ref_nodes` 를 담은 매핑을 재귀적으로 낸다. `node`·`ref_nodes` 원소는 {ref|id} 매핑."""
    if isinstance(obj, Mapping):
        if "contact_id" in obj:
            yield path, {"contact_id": obj["contact_id"]}
        node = obj.get("node")
        if isinstance(node, Mapping):
            yield f"{path}.node", node
        refs = obj.get("ref_nodes")
        if isinstance(refs, list):
            for j, n in enumerate(refs):
                if isinstance(n, Mapping):
                    yield f"{path}.ref_nodes[{j}]", n
        for k, v in obj.items():
            if k in ("node", "ref_nodes"):
                continue
            yield from _walk_extractor(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for j, v in enumerate(obj):
            yield from _walk_extractor(v, f"{path}[{j}]")
