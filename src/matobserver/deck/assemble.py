"""덱 조립기 — 레시피 + 파라미터 → 실행 폴더 (recipe.md §3, module-boundaries.md §4.4).

실행 폴더(`workdir`)에 쓰는 것:

| 파일 | 내용 |
| --- | --- |
| 레시피 덱 사본 | `deck.main`, `deck.files`, 모드별 제어 파일 — `deck.main`이 있는 폴더 기준 상대 경로 그대로 |
| `mo_params.k` | `*PARAMETER` `R name,value`(`%.10e`, 덱 단위): 노출 파라미터(매니페스트 순) + 예약 mo_*(고정 순) |
| `mo_specimen.k` | `case.specimen_mesh.mesh_path` 바이트 사본 |
| `mo_mat_<slot>.k` | 슬롯별 *PART·*SECTION·(*HOURGLASS)·*MAT·*DEFINE_CURVE (`deck.materials`) |
| `mo_jig.k` | parametric: `case.jig_mesh.mesh_path` 사본 / fixed: 쓰지 않음 |
| `mo_controls.k` | `deck.controls[<mode>]` 바이트 사본 |
| `mo_output.k` | *DATABASE_RCFORC/NODOUT/GLSTAT/MATSUM(`&mo_dtout`), *DATABASE_BINARY_D3PLOT(`&mo_dtplt`), *DATABASE_HISTORY_NODE, *DATABASE_EXTENT_BINARY |

레시피 저장소는 읽기만 한다. 같은 입력이면 같은 바이트(시각·경로를 파일에 쓰지 않는다).
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from ..core import units
from ..core.errors import IncompatibleOverride
from ..core.types import NeutralMaterial, RunOverrides, SimulationCase
from . import kfile
from .materials import LongFormat, slot_file_text

Mode = Literal["implicit", "explicit"]

PARAMS_FILE = "mo_params.k"
SPECIMEN_FILE = "mo_specimen.k"
JIG_FILE = "mo_jig.k"
CONTROLS_FILE = "mo_controls.k"
OUTPUT_FILE = "mo_output.k"


def slot_file(slot: str) -> str:
    return f"mo_mat_{slot}.k"


#: 예약 파라미터 (recipe.md §3.2) → 물리량(덱 단위 환산), 고정 출력 순서
RESERVED_PARAMS: Mapping[str, str] = {
    "mo_dend": "length",
    "mo_tend": "time",
    "mo_dtout": "time",
    "mo_dtplt": "time",
    "mo_dtmax": "time",
    "mo_htop": "length",
    "mo_hbot": "length",
    "mo_v0": "velocity",  # n차 예약
    "mo_mass": "mass",  # n차 예약
}
#: core.units 표에 없는 물리량 (n차 예약 mo_mass: kg → tonne)
_EXTRA_FACTORS = {"dimensionless": 1.0, "mass": 1e-3}


def dimension_factor(dimension: str) -> float:
    if dimension in _EXTRA_FACTORS:
        return _EXTRA_FACTORS[dimension]
    return units.factor(dimension)


@dataclass(frozen=True)
class AssembledDeck:
    workdir: Path
    main_file: Path
    mode: Mode
    files: tuple[str, ...]  # workdir 기준 상대 경로(정렬)


# ======================================================================================
# mo_params.k
# ======================================================================================
def deck_parameter_values(case: SimulationCase, mode: Mode, overrides: RunOverrides | None = None) -> dict[str, tuple[str, float]]:
    """이름 → (타입 R|I, 덱 단위 값). 순서 = 매니페스트 노출 파라미터 → 예약 파라미터(고정 순)."""
    declared = {p["name"]: p for p in case.manifest.get("parameters", []) or []}
    unknown = sorted(set(case.params_si) - set(declared))
    if unknown:
        raise ValueError(f"params_si has names not declared in manifest parameters: {unknown}")
    out: dict[str, tuple[str, float]] = {}
    for name, p in declared.items():
        if name not in case.params_si:
            if p.get("default") is None:
                raise ValueError(f"parameter {name!r} has no value and no manifest default")
            value_si = float(p["default"])
        else:
            value_si = float(case.params_si[name])
        typ = str(p.get("type", "R")).upper()
        v = value_si * dimension_factor(str(p.get("dimension", "dimensionless")))
        out[name] = (typ, float(int(round(v))) if typ == "I" else v)

    reserved: dict[str, float] = {}
    reserved.update({"mo_dtout": case.output.dt_out_s, "mo_dtplt": case.output.dt_plot_s, "mo_dtmax": case.output.dt_max_s})
    reserved.update({k: float(v) for k, v in (case.engine_params_si.get(mode) or {}).items()})
    if overrides is not None and overrides.engine_params_si:
        reserved.update({k: float(v) for k, v in overrides.engine_params_si.items()})
    bad = sorted(k for k in reserved if k not in RESERVED_PARAMS)
    if bad:
        raise ValueError(f"unknown reserved engine parameters: {bad}")
    for name, dim in RESERVED_PARAMS.items():
        if name in reserved:
            out[name] = ("R", reserved[name] * dimension_factor(dim))
    return out


def params_text(values: Mapping[str, tuple[str, float]]) -> str:
    lines = ["*KEYWORD", "$ matobserver mo_params.k (deck units mm-N-s-tonne)", "*PARAMETER"]
    for name, (typ, v) in values.items():
        if typ == "I":
            lines.append(f"I {name},{int(v)}")
        else:
            lines.append(f"R {name},{v:.10e}")
    lines.append("*END")
    return "\n".join(lines) + "\n"


# ======================================================================================
# mo_output.k
# ======================================================================================
def resolve_node(spec: Mapping[str, Any], case: SimulationCase) -> int:
    """응답 `node` 지정({id} 또는 {ref}) → 노드 ID. ref는 시편 reference_nodes, 지그 reference_nodes 순으로 찾는다."""
    if "id" in spec:
        return int(spec["id"])
    ref = spec.get("ref")
    if ref is None:
        raise ValueError(f"node spec needs 'id' or 'ref': {dict(spec)!r}")
    if ref in case.specimen_mesh.reference_nodes:
        return int(case.specimen_mesh.reference_nodes[ref])
    if case.jig_mesh is not None and ref in case.jig_mesh.reference_nodes:
        return int(case.jig_mesh.reference_nodes[ref])
    raise ValueError(f"reference node {ref!r} is not defined by the specimen or jig mesh")


def response_nodes(case: SimulationCase) -> list[int]:
    """모든 응답 추출기가 가리키는 노드 + OutputRequest.history_nodes (정렬·중복 제거)."""
    nodes: set[int] = {int(n) for n in case.output.history_nodes}
    for r in case.responses:
        for ex in r.extractor.values():
            args = ex.args
            if "node" in args:
                nodes.add(resolve_node(args["node"], case))
            for ref in args.get("ref_nodes", ()) or ():
                nodes.add(resolve_node(ref, case))
    return sorted(nodes)


def output_text(case: SimulationCase) -> str:
    lines = ["*KEYWORD", "$ matobserver mo_output.k (intervals from &mo_dtout / &mo_dtplt)"]
    for kw in ("DATABASE_RCFORC", "DATABASE_NODOUT", "DATABASE_GLSTAT", "DATABASE_MATSUM"):
        lines.append(f"*{kw}")
        lines.append(kfile.comment("dt", "binary", "lcur", "ioopt"))
        lines.append(kfile.card(["&mo_dtout", 2, 0, 1]))
    lines.append("*DATABASE_BINARY_D3PLOT")
    lines.append(kfile.comment("dt", "lcdt", "beam", "npltc", "psetid"))
    lines.append(kfile.card(["&mo_dtplt"]))
    nodes = response_nodes(case)
    if nodes:
        lines.append("*DATABASE_HISTORY_NODE")
        lines.append(kfile.comment("id1", "id2", "id3", "id4", "id5", "id6", "id7", "id8"))
        for i in range(0, len(nodes), 8):
            lines.append(kfile.card(nodes[i:i + 8]))
    lines.append("*DATABASE_EXTENT_BINARY")
    lines.append(kfile.comment("neiph", "neips", "maxint", "strflg", "sigflg", "epsflg", "rltflg", "engflg"))
    lines.append(kfile.card([0, 0, 3, 0, 1, 1, 1, 1]))
    lines.append("*END")
    return "\n".join(lines) + "\n"


# ======================================================================================
# 덱 검사 도우미 (prepare 의 IncompatibleOverride, verify 의 WARN_* 공용)
# ======================================================================================
def find_mass_scaling(texts: Mapping[str, str], params: Mapping[str, float] | None = None) -> list[str]:
    """`*CONTROL_TIMESTEP` DT2MS ≠ 0(파라미터 참조는 값으로 풀고, 못 풀면 ≠0로 본다), 이름에 MASS가 든
    `*CONTROL_IMPLICIT_*` → 설명 목록 ("<file>: ...")."""
    found: list[str] = []
    for fname, text in texts.items():
        for b in kfile.parse_k(text, source=fname):
            if b.name.startswith("CONTROL_TIMESTEP") and b.lines:
                f = kfile.split_fields(b.lines[0], b.width) + [""] * 8
                raw = f[4]
                try:
                    v = kfile.to_float(raw, params)
                except (KeyError, ValueError):
                    found.append(f"{fname}: *CONTROL_TIMESTEP DT2MS={raw!r} (unresolved, treated as non-zero)")
                    continue
                if v != 0.0:
                    found.append(f"{fname}: *CONTROL_TIMESTEP DT2MS={raw}")
            elif b.name.startswith("CONTROL_IMPLICIT_") and "MASS" in b.name:
                found.append(f"{fname}: *{b.name}")
    return found


def find_damping(texts: Mapping[str, str]) -> list[str]:
    return [f"{fname}: *{b.name}" for fname, text in texts.items() for b in kfile.parse_k(text) if b.name.startswith("DAMPING_")]


def find_hardcoded_materials(texts: Mapping[str, str]) -> list[str]:
    """레시피 소유 파일의 `*MAT_RIGID`가 아닌 `*MAT_*` (슬롯 재료는 mo_mat 파일에서만 온다)."""
    out = []
    for fname, text in texts.items():
        for b in kfile.parse_k(text):
            if b.name.startswith("MAT_") and not b.name.removesuffix("_TITLE") in ("MAT_RIGID", "MAT_020"):
                if b.name.startswith("MAT_ADD_") or b.name.startswith("MAT_THERMAL"):
                    continue
                out.append(f"{fname}: *{b.name}")
    return out


# ======================================================================================
# 조립
# ======================================================================================
def _rel_under(base: PurePosixPath, path: str) -> str:
    rel = posixpath.relpath(posixpath.normpath(path), str(base) if str(base) else ".")
    if rel.startswith("..") or posixpath.isabs(rel):
        raise ValueError(f"recipe file {path!r} is outside the main deck folder {str(base)!r}")
    return rel


def recipe_deck_files(manifest: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """(main 상대 경로(workdir 기준), {레시피 경로: workdir 상대 경로}) — 모든 모드의 제어 파일 포함."""
    deck = manifest["deck"]
    main = str(deck["main"])
    base = PurePosixPath(main).parent
    mapping: dict[str, str] = {main: _rel_under(base, main)}
    for p in deck.get("files", []) or []:
        mapping[str(p)] = _rel_under(base, str(p))
    for p in (deck.get("controls") or {}).values():
        mapping[str(p)] = _rel_under(base, str(p))
    for dst in mapping.values():
        if PurePosixPath(dst).name.startswith("mo_") and PurePosixPath(dst).parent == PurePosixPath("."):
            raise ValueError(f"recipe file {dst!r} collides with a reserved mo_* include name")
    return mapping[main], mapping


def read_recipe_texts(case: SimulationCase) -> dict[str, str]:
    _, mapping = recipe_deck_files(case.manifest)
    return {src: (Path(case.recipe_dir) / src).read_text(encoding="utf-8", errors="replace") for src in mapping}


def check_overrides(case: SimulationCase, overrides: RunOverrides, params: Mapping[str, tuple[str, float]]) -> None:
    """RunOverrides 사전 검사: 슬롯 이름, 질량 스케일링(forbid_mass_scaling) → IncompatibleOverride. 덱은 고치지 않는다."""
    slots = {b.slot for b in case.slots}
    unknown = sorted(set(overrides.section_by_slot) - slots)
    if unknown:
        raise IncompatibleOverride(f"section_by_slot names unknown slots: {unknown}")
    if overrides.forbid_mass_scaling:
        deck = case.manifest["deck"]
        controls = (deck.get("controls") or {}).get("explicit")
        if controls is None:
            raise IncompatibleOverride("recipe has no explicit controls file (deck.controls.explicit)")
        texts = read_recipe_texts(case)
        pvals = {k: v for k, (_, v) in params.items()}
        found = find_mass_scaling(texts, pvals)
        if found:
            raise IncompatibleOverride("mass scaling present while forbid_mass_scaling=True: " + "; ".join(found))


def assemble(case: SimulationCase, materials: Mapping[str, NeutralMaterial], workdir: Path, mode: Mode,
             overrides: RunOverrides | None = None, *, long_format: LongFormat = "auto") -> AssembledDeck:
    """실행 폴더를 만든다. workdir 밖에는 쓰지 않는다."""
    workdir = Path(workdir)
    manifest = case.manifest
    deck = manifest["deck"]
    controls = (deck.get("controls") or {}).get(mode)
    if controls is None:
        raise ValueError(f"recipe has no controls file for mode {mode!r} (deck.controls.{mode})")
    kind = manifest.get("kind")
    if kind == "parametric" and case.jig_mesh is None:
        raise ValueError("parametric recipe needs case.jig_mesh")
    if kind == "fixed" and case.jig_mesh is not None:
        raise ValueError("fixed recipe must not have a generated jig mesh")
    missing = sorted(b.slot for b in case.slots if b.slot not in materials)
    if missing:
        raise ValueError(f"materials missing for slots: {missing}")

    params = deck_parameter_values(case, mode, overrides)
    if overrides is not None:
        check_overrides(case, overrides, params)

    main_rel, mapping = recipe_deck_files(manifest)
    workdir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def put(rel: str, data: bytes) -> None:
        dst = workdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        written.append(rel)

    recipe_dir = Path(case.recipe_dir)
    for src, rel in mapping.items():
        put(rel, (recipe_dir / src).read_bytes())
    put(PARAMS_FILE, params_text(params).encode("ascii"))
    put(SPECIMEN_FILE, Path(case.specimen_mesh.mesh_path).read_bytes())
    if case.jig_mesh is not None:
        put(JIG_FILE, Path(case.jig_mesh.mesh_path).read_bytes())
    sec = overrides.section_by_slot if overrides is not None else {}
    for b in case.slots:
        text = slot_file_text(b, materials[b.slot], section_override=sec.get(b.slot), long_format=long_format)
        put(slot_file(b.slot), text.encode("ascii"))
    put(CONTROLS_FILE, (recipe_dir / str(controls)).read_bytes())
    put(OUTPUT_FILE, output_text(case).encode("ascii"))
    return AssembledDeck(workdir=workdir, main_file=workdir / main_rel, mode=mode, files=tuple(sorted(set(written))))


def reserved_files_for(manifest: Mapping[str, Any], slots: Sequence[str]) -> list[str]:
    files = [PARAMS_FILE, SPECIMEN_FILE, CONTROLS_FILE, OUTPUT_FILE] + [slot_file(s) for s in slots]
    if manifest.get("kind") == "parametric":
        files.append(JIG_FILE)
    return files
