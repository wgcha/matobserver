"""재료 키워드 writer·reader (plugins.md §4.2: 키워드 작성은 어댑터 책임, result.md §5 `card_lsdyna`).

* `NeutralMaterial.table is None` → `*MAT_ELASTIC`(MAT_001). table 있음 → `*MAT_PIECEWISE_LINEAR_PLASTICITY`(MAT_024)
  + `*DEFINE_CURVE`(LCSS, 진응력–유효소성변형률). `mat024_coeff`·`mat024_table` 둘 다 to_neutral 결과가 테이블이므로
  같은 경로(LCSS 곡선)로 쓴다. 강체 `*MAT_RIGID`도 제공(지그·1요소 덱용).
* 단위: SI → mm·N·s·tonne (응력 MPa, 밀도 tonne/mm³) — 변환은 이 파일 writer·reader 안에서만(module-boundaries.md §3).
* 형식: 표준 고정 10칸(MatNexus `dyna.py`와 같은 카드 배치, 카드 건너뛰지 않음). 카드 1의 실수가 10칸에서
  상대오차 `LONG_FORMAT_REL_TOL`을 넘게 잘리면(예: 임의 밀도 1e-9대) 그 키워드만 long 형식(키워드 뒤 ` +`, 20칸)으로 쓴다.
  곡선 점은 20칸 둘(`%.12E`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from ..core import units
from ..core.errors import IncompatibleMaterial
from ..core.types import ElasticBlock, NeutralMaterial, SlotBinding, TableRow
from . import kfile
from .kfile import LONG_WIDTH, STANDARD_WIDTH, card, comment, fit_rel_error, parse_k, split_fields, to_float, to_int

LONG_FORMAT_REL_TOL = 1e-7

#: 지원 extensions 키 → 허용 필드 (그 외 키·필드는 IncompatibleMaterial)
SUPPORTED_EXTENSIONS: Mapping[str, frozenset[str]] = {
    "lsdyna.mat024": frozenset({"fail", "tdel", "vp"}),
}

LongFormat = Literal["auto", "never", "always"]


def material_keyword(material: NeutralMaterial) -> str:
    """결과 `derived.lsdyna_keyword` 값: `MAT_001`(탄성 전용) | `MAT_024`."""
    return "MAT_001" if material.table is None else "MAT_024"


def check_extensions(material: NeutralMaterial) -> None:
    for key, values in material.extensions.items():
        allowed = SUPPORTED_EXTENSIONS.get(key)
        if allowed is None:
            raise IncompatibleMaterial(f"unsupported NeutralMaterial extension {key!r} for lsdyna")
        extra = set(values) - allowed
        if extra:
            raise IncompatibleMaterial(f"unsupported fields in extension {key!r}: {sorted(extra)}")
        if key == "lsdyna.mat024" and material.table is None:
            raise IncompatibleMaterial("extension 'lsdyna.mat024' requires a plastic table (MAT_024)")


def _use_long(values: Sequence[float], mode: LongFormat) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return any(fit_rel_error(v, STANDARD_WIDTH) > LONG_FORMAT_REL_TOL for v in values)


def _kw(name: str, long_format: bool) -> str:
    return f"*{name} +" if long_format else f"*{name}"


def _zeros(width: int, count: int = 8) -> str:
    return "".join("0".rjust(width) for _ in range(count))


def mat_lines(material: NeutralMaterial, mid: int, lcid: int | None, *, title: str | None = None,
              long_format: LongFormat = "auto") -> list[str]:
    """`*MAT_ELASTIC` 또는 `*MAT_PIECEWISE_LINEAR_PLASTICITY` + `*DEFINE_CURVE` 줄 목록(*KEYWORD/*END 없음)."""
    check_extensions(material)
    e = material.elastic
    ro = units.kg_m3_to_tonne_mm3(float(e.density))
    em = units.pa_to_mpa(float(e.youngs_modulus))
    pr = float(e.poisson_ratio)
    lines: list[str] = []
    if material.table is None:
        long_ = _use_long([ro, em, pr], long_format)
        w = LONG_WIDTH if long_ else STANDARD_WIDTH
        lines.append(_kw("MAT_ELASTIC_TITLE" if title else "MAT_ELASTIC", long_))
        if title:
            lines.append(title[:80])
        lines.append(comment("mid", "ro", "e", "pr", "da", "db", "k", width=w))
        lines.append(card([int(mid), ro, em, pr], w))
        return lines

    if lcid is None:
        raise IncompatibleMaterial("MAT_024 needs an LCSS curve id (manifest material_slots[].lcid)")
    ext = dict(material.extensions.get("lsdyna.mat024", {}))
    rows = list(material.table)
    sigy = units.pa_to_mpa(float(rows[0].true_stress))
    fail = ext.get("fail")
    tdel = ext.get("tdel")
    vp = ext.get("vp")
    card1_vals = [ro, em, pr, sigy] + [float(v) for v in (fail, tdel) if v is not None]
    long_ = _use_long(card1_vals, long_format)
    w = LONG_WIDTH if long_ else STANDARD_WIDTH
    name = "MAT_PIECEWISE_LINEAR_PLASTICITY"
    lines.append(_kw(name + ("_TITLE" if title else ""), long_))
    if title:
        lines.append(title[:80])
    lines.append(comment("mid", "ro", "e", "pr", "sigy", "etan", "fail", "tdel", width=w))
    c1: list[Any] = [int(mid), ro, em, pr, sigy, 0.0]
    if fail is not None or tdel is not None:
        c1 += [float(fail) if fail is not None else 0.0, float(tdel) if tdel is not None else 0.0]
    lines.append(card(c1, w))
    lines.append(comment("c", "p", "lcss", "lcsr", "vp", width=w))
    lines.append(card([0.0, 0.0, int(lcid), 0, float(vp) if vp is not None else 0.0], w))
    lines.append(comment("eps1", "eps2", "eps3", "eps4", "eps5", "eps6", "eps7", "eps8", width=w))
    lines.append(_zeros(w))
    lines.append(comment("es1", "es2", "es3", "es4", "es5", "es6", "es7", "es8", width=w))
    lines.append(_zeros(w))
    lines += curve_lines(int(lcid), [(float(r.plastic_strain), units.pa_to_mpa(float(r.true_stress))) for r in rows],
                         title="true stress [MPa] vs effective plastic strain")
    return lines


def curve_lines(lcid: int, points: Sequence[tuple[float, float]], *, title: str | None = None) -> list[str]:
    """`*DEFINE_CURVE` — (가로, 세로) 20칸 둘."""
    lines = ["*DEFINE_CURVE_TITLE" if title else "*DEFINE_CURVE"]
    if title:
        lines.append(title[:80])
    lines.append(comment("lcid", "sidr", "sfa", "sfo", "offa", "offo", "dattyp"))
    lines.append(card([int(lcid), 0, 1.0, 1.0, 0.0, 0.0, 0]))
    lines.append("$#" + "a1".rjust(18) + "o1".rjust(20))
    for x, y in points:
        lines.append(f"{x:>20.12E}{y:>20.12E}")
    return lines


def mat_rigid_lines(mid: int, e_pa: float, poisson: float, density_kg_m3: float, *, title: str | None = None) -> list[str]:
    """`*MAT_RIGID`(MAT_020) 카드 3장 (CMO=0: 구속은 덱의 경계조건이 준다)."""
    ro = units.kg_m3_to_tonne_mm3(float(density_kg_m3))
    em = units.pa_to_mpa(float(e_pa))
    lines = ["*MAT_RIGID_TITLE" if title else "*MAT_RIGID"]
    if title:
        lines.append(title[:80])
    lines.append(comment("mid", "ro", "e", "pr", "n", "couple", "m", "alias"))
    lines.append(card([int(mid), ro, em, float(poisson)]))
    lines.append(comment("cmo", "con1", "con2"))
    lines.append(card([0.0, 0, 0]))
    lines.append(comment("lco/a1", "a2", "a3", "v1", "v2", "v3"))
    lines.append(card([0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
    return lines


def slot_file_text(binding: SlotBinding, material: NeutralMaterial, *,
                   section_override: Mapping[str, Any] | None = None,
                   long_format: LongFormat = "auto") -> str:
    """`mo_mat_<slot>.k` 전체 텍스트: *PART(슬롯 part_ids마다) · *SECTION_SOLID · (덮어쓰기면) *HOURGLASS · *MAT · *DEFINE_CURVE.

    `section_override`(RunOverrides.section_by_slot[slot]): {elform, hourglass: {ihq, qm}} — ELFORM 교체,
    `*HOURGLASS`(HGID = secid) 생성, PART HGID 기록.
    """
    section = dict(binding.section)
    keyword = str(section.get("keyword", "SECTION_SOLID")).upper().lstrip("*")
    if keyword != "SECTION_SOLID":
        raise IncompatibleMaterial(f"section keyword {keyword!r} is not supported in 1.0 (SECTION_SOLID only)")
    elform = int(section.get("elform", -2))
    aet = int(section.get("aet", 0))
    hourglass: Mapping[str, Any] | None = None
    if section_override:
        if "elform" in section_override:
            elform = int(section_override["elform"])
        hourglass = section_override.get("hourglass")
    hgid = int(binding.secid) if hourglass else 0

    lines = ["*KEYWORD", f"$ matobserver slot={binding.slot} keyword={material_keyword(material)} units=mm-N-s-tonne"]
    for pid in binding.part_ids:
        lines.append("*PART")
        lines.append(f"matobserver slot {binding.slot} pid {pid}")
        lines.append(comment("pid", "secid", "mid", "eosid", "hgid", "grav", "adpopt", "tmid"))
        lines.append(card([int(pid), int(binding.secid), int(binding.mid), 0, hgid, 0, 0, 0]))
    lines.append("*SECTION_SOLID")
    lines.append(comment("secid", "elform", "aet"))
    lines.append(card([int(binding.secid), elform, aet]))
    if hourglass:
        lines.append("*HOURGLASS")
        lines.append(comment("hgid", "ihq", "qm", "ibq", "q1", "q2", "qb/vdc", "qw"))
        lines.append(card([hgid, int(hourglass.get("ihq", 6)), float(hourglass.get("qm", 0.1))]))
    lines += mat_lines(material, int(binding.mid), binding.lcid, long_format=long_format)
    lines.append("*END")
    return "\n".join(lines) + "\n"


def card_text(material: NeutralMaterial, mid: int, lcid: int | None, *, slot: str, long_format: LongFormat = "auto") -> str:
    """결과 artifact `card_lsdyna`(슬롯별 2차 산출물) 텍스트."""
    lines = [
        "*KEYWORD",
        f"$ matobserver card slot={slot} keyword={material_keyword(material)}",
        "$ Consistent units: mm - N - s - tonne (stress MPa, density tonne/mm^3)",
    ]
    lines += mat_lines(material, mid, lcid, title=f"matobserver {slot}", long_format=long_format)
    lines.append("*END")
    return "\n".join(lines) + "\n"


# ======================================================================================
# reader (왕복 검사 S6·S-3)
# ======================================================================================
@dataclass(frozen=True)
class ParsedMaterial:
    mid: int
    keyword: str  # "MAT_001" | "MAT_024" | "MAT_020"
    material: NeutralMaterial | None  # MAT_020이면 None
    raw: Mapping[str, float]  # 파일 단위 원값 (ro, e, pr, sigy, etan, lcss ...)


def _data_lines(block: kfile.KeywordBlock) -> list[str]:
    return block.lines[1:] if block.has_title() else block.lines


def read_curves(blocks: Sequence[kfile.KeywordBlock], params: Mapping[str, float] | None = None) -> dict[int, list[tuple[float, float]]]:
    """`*DEFINE_CURVE(_TITLE)` → {lcid: [(a, o)]} (SFA·SFO 배율, OFFA·OFFO 오프셋 적용: a' = sfa·a + offa)."""
    out: dict[int, list[tuple[float, float]]] = {}
    for b in blocks:
        if b.name not in ("DEFINE_CURVE", "DEFINE_CURVE_TITLE"):
            continue
        lines = _data_lines(b)
        head = split_fields(lines[0], b.width)
        head += [""] * (8 - len(head))
        lcid = to_int(head[0], params)
        sfa = to_float(head[2], params, 1.0) or 1.0
        sfo = to_float(head[3], params, 1.0) or 1.0
        offa = to_float(head[4], params, 0.0)
        offo = to_float(head[5], params, 0.0)
        pts = []
        for line in lines[1:]:
            f = split_fields(line, 2 * b.width)
            if len(f) < 2 or not f[0]:
                continue
            pts.append((sfa * to_float(f[0], params) + offa, sfo * to_float(f[1], params) + offo))
        out[lcid] = pts
    return out


def read_materials(text: str, params: Mapping[str, float] | None = None) -> dict[int, ParsedMaterial]:
    """덱 텍스트의 *MAT_ELASTIC / *MAT_PIECEWISE_LINEAR_PLASTICITY / *MAT_RIGID → SI NeutralMaterial (mid별)."""
    blocks = parse_k(text)
    if params is None:
        params = kfile.read_parameters(blocks)
    curves = read_curves(blocks, params)
    out: dict[int, ParsedMaterial] = {}
    for b in blocks:
        name = b.name.removesuffix("_TITLE")
        if name not in ("MAT_ELASTIC", "MAT_001", "MAT_PIECEWISE_LINEAR_PLASTICITY", "MAT_024", "MAT_RIGID", "MAT_020"):
            continue
        lines = _data_lines(b)
        c1 = split_fields(lines[0], b.width) + [""] * 8
        mid = to_int(c1[0], params)
        ro, e, pr = (to_float(c1[i], params) for i in (1, 2, 3))
        elastic = ElasticBlock(
            youngs_modulus=units.mpa_to_pa(e), poisson_ratio=pr, density=units.tonne_mm3_to_kg_m3(ro)
        )
        if name in ("MAT_RIGID", "MAT_020"):
            out[mid] = ParsedMaterial(mid, "MAT_020", None, {"ro": ro, "e": e, "pr": pr})
        elif name in ("MAT_ELASTIC", "MAT_001"):
            out[mid] = ParsedMaterial(mid, "MAT_001", NeutralMaterial(elastic=elastic), {"ro": ro, "e": e, "pr": pr})
        else:
            sigy, etan = to_float(c1[4], params), to_float(c1[5], params)
            c2 = split_fields(lines[1], b.width) + [""] * 8 if len(lines) > 1 else [""] * 8
            lcss = to_int(c2[2], params)
            raw = {"ro": ro, "e": e, "pr": pr, "sigy": sigy, "etan": etan, "lcss": float(lcss)}
            ext: dict[str, Any] = {}
            fail, tdel, vp = to_float(c1[6], params), to_float(c1[7], params), to_float(c2[4], params)
            if fail or tdel or vp:
                ext["lsdyna.mat024"] = {k: v for k, v in (("fail", fail), ("tdel", tdel), ("vp", vp)) if v}
            if lcss:
                if lcss not in curves:
                    raise ValueError(f"MAT_024 mid={mid}: LCSS {lcss} curve not found")
                rows = tuple(TableRow(plastic_strain=a, true_stress=units.mpa_to_pa(o)) for a, o in curves[lcss])
            else:
                ep = e * etan / (e - etan) if etan else 0.0
                rows = (TableRow(0.0, units.mpa_to_pa(sigy)), TableRow(1.0, units.mpa_to_pa(sigy + ep)))
            out[mid] = ParsedMaterial(mid, "MAT_024", NeutralMaterial(elastic=elastic, table=rows, extensions=ext), raw)
    return out
