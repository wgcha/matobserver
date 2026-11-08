"""메시 .k(include) writer·reader — module-boundaries.md §2 규격.

* ASCII, LF, 첫 줄 `*KEYWORD`, 끝 `*END`, 데이터 줄 ≤ 80칸(`$` 주석 줄은 ≤ 256칸).
* 머리 주석 3줄: `$ matobserver mesh` / `$ generator=... version=... topology=... target_size_mm=... units=mm-N-s-tonne`
  / `$ input_sha256=<hex>`.
* `*NODE`: NID i8, X·Y·Z e16.9, TC·RC i8.
* `*ELEMENT_SOLID`: 옵션 없는 키워드 하나, 요소당 2줄(1줄 EID·PID i8 / 2줄 절점 i8 × 8 또는 10).
  hex8 N1..N8, tet4 N1 N2 N3 N4 N4 N4 N4 N4, tet10 N1..N10(N5=1-2, N6=2-3, N7=3-1, N8=1-4, N9=2-4, N10=3-4).
* 지그: `*ELEMENT_SHELL` EID·PID·N1..N4 i8 한 줄, `*PART`, `*SECTION_SHELL`, `*MAT_RIGID`.
* 세트·파트·단면·재료 카드는 LS-DYNA 표준 10칸 필드.

단위 변환(SI → mm·tonne·MPa)은 이 모듈의 writer 안에서만 한다(module-boundaries.md §3).
reader(`read_k`)는 Impl-Solver `.k` 리더가 준비되기 전 M-8 자체 검사용 고정 폭 파서다(엄격 모드: 규격 위반이면 `KFormatError`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from ..core import units

UNITS_TAG = "units=" + units.UNIT_SYSTEM
LINE_MAX = 80
#: 주석(`$`) 줄 상한 — §2 머리 주석 2줄째가 80칸을 넘을 수 있다(LS-DYNA 는 `$` 줄 전체를 무시).
COMMENT_LINE_MAX = 256

SPECIMEN_KEYWORDS = frozenset({"*NODE", "*ELEMENT_SOLID", "*SET_NODE_LIST", "*SET_NODE_LIST_TITLE", "*SET_PART_LIST"})
JIG_KEYWORDS = frozenset({"*NODE", "*ELEMENT_SHELL", "*PART", "*SECTION_SHELL", "*MAT_RIGID",
                          "*SET_NODE_LIST", "*SET_NODE_LIST_TITLE"})
#: 2차 예약: 축대칭 quad4_axisym 시편은 *ELEMENT_SHELL 을 쓴다(module-boundaries.md §2). 1차 writer 는 거절.
RESERVED_SPECIMEN_TOPOLOGIES = frozenset({"quad4_axisym", "shell4"})

SOLID_NODES_PER_LINE = {"hex8": 8, "tet4": 8, "tet10": 10}


class KFormatError(ValueError):
    """.k 파일이 §2 규격을 위반."""


# ----------------------------------------------------------------------------------------------
# 고정 폭 필드
# ----------------------------------------------------------------------------------------------
def i8(v: int) -> str:
    s = f"{int(v):8d}"
    if len(s) != 8:
        raise KFormatError(f"integer {v} does not fit i8")
    return s


def i10(v: int) -> str:
    s = f"{int(v):10d}"
    if len(s) != 10:
        raise KFormatError(f"integer {v} does not fit i10")
    return s


def e16(v: float) -> str:
    v = float(v)
    if not math.isfinite(v):
        raise KFormatError(f"non-finite coordinate {v!r}")
    if v == 0.0:
        v = 0.0  # -0.0 → 0.0 (대칭면 좌표 정확히 0, 부호 없는 표기)
    s = f"{v:16.9e}"
    if len(s) != 16:
        raise KFormatError(f"coordinate {v!r} does not fit e16.9")
    return s


def f10(v: float) -> str:
    """10칸 실수(재료·단면 카드): 오른쪽 정렬, 앞 1칸 공백 확보, 9자 안에서 유효숫자 최대."""
    v = float(v)
    if not math.isfinite(v):
        raise KFormatError(f"non-finite value {v!r}")
    if v == 0.0:
        return f"{'0.0':>10}"
    for prec in range(9, 0, -1):
        s = f"{v:.{prec}g}"
        if "e" not in s and "." not in s:
            s += "."
        s = s.replace("e+0", "e").replace("e-0", "e-").replace("e+", "e")  # 지수 축약(Fortran 판독 가능)
        if "e" in s and "." not in s.split("e")[0]:
            m, ex = s.split("e")
            s = f"{m}.e{ex}"
        if len(s) <= 9:
            return f"{s:>10}"
    raise KFormatError(f"value {v!r} does not fit 10 columns")


def quantize_mm(coords_m: np.ndarray) -> np.ndarray:
    """SI 좌표를 파일 표기(e16.9 mm)로 반올림한 값을 mm로 돌려준다(메모리 값 = 파일 값)."""
    mm = np.asarray(coords_m, dtype=float) * units.factor("length")
    flat = [float(e16(v)) for v in mm.ravel()]
    return np.asarray(flat, dtype=float).reshape(mm.shape)


def header_lines(generator: str, version: str, topology: str, target_size_m: float, input_sha256: str) -> list[str]:
    h_mm = float(target_size_m) * units.factor("length")
    return [
        "$ matobserver mesh",
        f"$ generator={generator} version={version} topology={topology} target_size_mm={h_mm:.9g} {UNITS_TAG}",
        f"$ input_sha256={input_sha256}",
    ]


# ----------------------------------------------------------------------------------------------
# writer
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeSet:
    sid: int
    title: str
    node_ids: tuple[int, ...]


def _node_block(node_ids: Sequence[int], coords_mm: np.ndarray) -> list[str]:
    out = ["*NODE"]
    for nid, (x, y, z) in zip(node_ids, coords_mm):
        out.append(i8(nid) + e16(x) + e16(y) + e16(z) + i8(0) + i8(0))
    return out


def _set_node_blocks(sets: Iterable[NodeSet]) -> list[str]:
    out: list[str] = []
    for s in sets:
        out.append("*SET_NODE_LIST_TITLE")
        out.append(s.title[:80])
        out.append(i10(s.sid) + f10(0.0) * 4 + f"{'MECH':>10}")
        ids = list(s.node_ids)
        for k in range(0, len(ids), 8):
            out.append("".join(i10(n) for n in ids[k:k + 8]))
    return out


def specimen_text(*, header: Sequence[str], topology: str, node_ids: Sequence[int], coords_mm: np.ndarray,
                  elem_ids: Sequence[int], elem_pids: Sequence[int], elem_nodes: np.ndarray,
                  node_sets: Sequence[NodeSet], part_set: tuple[int, Sequence[int]] | None) -> str:
    """시편 `mo_specimen.k` 본문. elem_nodes 는 노드 ID(전역) 배열 (E, 8|4|10)."""
    if topology in RESERVED_SPECIMEN_TOPOLOGIES:
        raise KFormatError(f"topology {topology!r} is reserved for 2nd phase (*ELEMENT_SHELL axisym)")
    if topology not in SOLID_NODES_PER_LINE:
        raise KFormatError(f"unknown solid topology {topology!r}")
    lines = ["*KEYWORD", *header]
    lines += _node_block(node_ids, coords_mm)
    lines.append("*ELEMENT_SOLID")
    for eid, pid, conn in zip(elem_ids, elem_pids, elem_nodes):
        conn = [int(c) for c in conn]
        if topology == "tet4":
            conn = conn[:4] + [conn[3]] * 4
        lines.append(i8(eid) + i8(pid))
        lines.append("".join(i8(c) for c in conn))
    lines += _set_node_blocks(node_sets)
    if part_set is not None:
        sid, pids = part_set
        lines.append("*SET_PART_LIST")
        lines.append(i10(sid) + f10(0.0) * 4 + f"{'MECH':>10}")
        pl = list(pids)
        for k in range(0, len(pl), 8):
            lines.append("".join(i10(p) for p in pl[k:k + 8]))
    lines.append("*END")
    return _join(lines)


@dataclass(frozen=True)
class RigidPart:
    title: str
    pid: int
    secid: int
    mid: int
    thickness_m: float
    density_kg_m3: float
    E_pa: float
    poisson: float


def jig_text(*, header: Sequence[str], node_ids: Sequence[int], coords_mm: np.ndarray, elem_ids: Sequence[int],
             elem_pids: Sequence[int], elem_nodes: np.ndarray, parts: Sequence[RigidPart],
             node_sets: Sequence[NodeSet] = ()) -> str:
    """지그 `mo_jig.k` 본문 (강체 셸)."""
    lines = ["*KEYWORD", *header]
    lines += _node_block(node_ids, coords_mm)
    lines.append("*ELEMENT_SHELL")
    for eid, pid, conn in zip(elem_ids, elem_pids, elem_nodes):
        lines.append(i8(eid) + i8(pid) + "".join(i8(c) for c in conn))
    for p in parts:
        lines += ["*PART", p.title[:70],
                  i10(p.pid) + i10(p.secid) + i10(p.mid) + i10(0) + i10(0) + i10(0) + i10(0) + i10(0)]
    for p in parts:
        t_mm = units.to_file("length", p.thickness_m)
        # SECID ELFORM SHRF NIP PROPT QR/IRID ICOMP SETYP / T1..T4 NLOC MAREA IDOF EDGSET
        lines += ["*SECTION_SHELL",
                  i10(p.secid) + i10(2) + f10(1.0) + i10(2) + f10(0.0) + f10(0.0) + i10(0) + i10(1),
                  f10(t_mm) * 4 + f10(0.0) + f10(0.0) + f10(0.0) + i10(0)]
    for p in parts:
        # MID RO E PR N COUPLE M ALIAS / CMO CON1 CON2 / LCO|A1..A6
        lines += ["*MAT_RIGID",
                  i10(p.mid) + f10(units.to_file("density", p.density_kg_m3)) + f10(units.to_file("stress", p.E_pa))
                  + f10(p.poisson) + f10(0.0) + f10(0.0) + f10(0.0),
                  f10(0.0) + f10(0.0) + f10(0.0),
                  f10(0.0) * 6]
    lines += _set_node_blocks(node_sets)
    lines.append("*END")
    return _join(lines)


def _join(lines: Sequence[str]) -> str:
    for ln in lines:
        if len(ln) > (COMMENT_LINE_MAX if ln.startswith("$") else LINE_MAX):
            raise KFormatError(f"line exceeds {LINE_MAX} columns: {ln!r}")
        if not ln.isascii():
            raise KFormatError(f"non-ASCII line: {ln!r}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------------
# reader (엄격, M-8 자체 파서)
# ----------------------------------------------------------------------------------------------
@dataclass
class KFile:
    header: list[str] = field(default_factory=list)  # 머리 주석 줄
    keywords: list[str] = field(default_factory=list)  # 등장 순서
    nodes: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # mm
    solids: dict[int, tuple[int, tuple[int, ...]]] = field(default_factory=dict)  # eid → (pid, 8|10 절점)
    shells: dict[int, tuple[int, tuple[int, ...]]] = field(default_factory=dict)
    node_sets: dict[int, tuple[str | None, tuple[int, ...]]] = field(default_factory=dict)
    part_sets: dict[int, tuple[int, ...]] = field(default_factory=dict)
    parts: dict[int, tuple[str, int, int]] = field(default_factory=dict)  # pid → (title, secid, mid)
    sections: dict[int, tuple[int, float]] = field(default_factory=dict)  # secid → (elform, t1 mm)
    rigid_mats: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # mid → (ro, E, pr) 파일 단위

    def header_fields(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for ln in self.header:
            for tok in ln.lstrip("$").split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    out[k] = v
        return out


def _int(s: str, what: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise KFormatError(f"bad integer field for {what}: {s!r}") from None


def _flt(s: str, what: str) -> float:
    try:
        return float(s)
    except ValueError:
        raise KFormatError(f"bad real field for {what}: {s!r}") from None


def _fields(line: str, width: int) -> list[str]:
    return [line[k:k + width] for k in range(0, len(line), width)]


def read_k(data: bytes | str, kind: str = "specimen") -> KFile:
    """규격 검사 + 파싱. kind = "specimen" | "jig"."""
    if isinstance(data, bytes):
        if not data.isascii():
            raise KFormatError("file is not ASCII")
        if b"\r" in data:
            raise KFormatError("CR found: line endings must be LF")
        text = data.decode("ascii")
    else:
        text = data
    if not text.endswith("\n"):
        raise KFormatError("file must end with newline")
    lines = text[:-1].split("\n")
    if not lines or lines[0] != "*KEYWORD":
        raise KFormatError("first line must be *KEYWORD")
    if lines[-1] != "*END":
        raise KFormatError("last line must be *END")
    allowed = SPECIMEN_KEYWORDS if kind == "specimen" else JIG_KEYWORDS
    kf = KFile()
    for ln in lines:
        if len(ln) > (COMMENT_LINE_MAX if ln.startswith("$") else LINE_MAX):
            raise KFormatError(f"line exceeds 80 columns: {ln!r}")
    # 블록 분할
    blocks: list[tuple[str, list[str]]] = []
    i = 1
    while i < len(lines) - 1:
        ln = lines[i]
        if ln.startswith("$"):
            if not blocks:
                kf.header.append(ln)
            i += 1
            continue
        if not ln.startswith("*"):
            raise KFormatError(f"data line outside keyword: {ln!r}")
        kw = ln.strip().upper()
        body: list[str] = []
        i += 1
        while i < len(lines) - 1 and not lines[i].startswith("*"):
            if not lines[i].startswith("$"):
                body.append(lines[i])
            i += 1
        blocks.append((kw, body))
    if len(kf.header) < 3 or kf.header[0] != "$ matobserver mesh":
        raise KFormatError("missing matobserver mesh header comments")
    hf = kf.header_fields()
    for key in ("generator", "version", "topology", "target_size_mm", "units", "input_sha256"):
        if key not in hf:
            raise KFormatError(f"header field {key!r} missing")
    if hf["units"] != units.UNIT_SYSTEM:
        raise KFormatError(f"units must be {units.UNIT_SYSTEM}")
    solid_seen = 0
    for kw, body in blocks:
        if kw not in allowed:
            raise KFormatError(f"keyword {kw} not allowed in {kind} mesh file")
        kf.keywords.append(kw)
        if kw == "*NODE":
            for ln in body:
                nid = _int(ln[0:8], "NID")
                xyz = (_flt(ln[8:24], "X"), _flt(ln[24:40], "Y"), _flt(ln[40:56], "Z"))
                if len(ln) != 72:
                    raise KFormatError(f"*NODE line must be 72 columns (NID i8, XYZ e16, TC RC i8): {ln!r}")
                _int(ln[56:64], "TC")
                _int(ln[64:72], "RC")
                if nid in kf.nodes:
                    raise KFormatError(f"duplicate node {nid}")
                kf.nodes[nid] = xyz
        elif kw == "*ELEMENT_SOLID":
            solid_seen += 1
            if solid_seen > 1:
                raise KFormatError("only one *ELEMENT_SOLID block allowed")
            if len(body) % 2:
                raise KFormatError("*ELEMENT_SOLID must have 2 lines per element")
            widths = set()
            for k in range(0, len(body), 2):
                l1, l2 = body[k], body[k + 1]
                if len(l1) != 16:
                    raise KFormatError(f"*ELEMENT_SOLID line 1 must be EID i8 PID i8: {l1!r}")
                eid, pid = _int(l1[0:8], "EID"), _int(l1[8:16], "PID")
                if len(l2) % 8:
                    raise KFormatError(f"*ELEMENT_SOLID line 2 must be i8 fields: {l2!r}")
                conn = tuple(_int(f, "N") for f in _fields(l2, 8))
                if len(conn) not in (8, 10):
                    raise KFormatError(f"element {eid}: {len(conn)} node fields (8 or 10 expected)")
                widths.add(len(conn))
                if eid in kf.solids:
                    raise KFormatError(f"duplicate element {eid}")
                kf.solids[eid] = (pid, conn)
            if len(widths) > 1:
                raise KFormatError("mixed solid element formats in one file")
        elif kw == "*ELEMENT_SHELL":
            for ln in body:
                if len(ln) != 48:
                    raise KFormatError(f"*ELEMENT_SHELL line must be EID PID N1..N4 i8: {ln!r}")
                f = [_int(x, "shell") for x in _fields(ln, 8)]
                kf.shells[f[0]] = (f[1], tuple(f[2:]))
        elif kw in ("*SET_NODE_LIST", "*SET_NODE_LIST_TITLE"):
            title = None
            rows = list(body)
            if kw.endswith("_TITLE"):
                title = rows.pop(0).strip()
            sid = _int(rows[0][0:10], "SID")
            ids: list[int] = []
            for ln in rows[1:]:
                ids += [_int(x, "set node") for x in _fields(ln, 10) if x.strip()]
            kf.node_sets[sid] = (title, tuple(ids))
        elif kw == "*SET_PART_LIST":
            sid = _int(body[0][0:10], "SID")
            ids = []
            for ln in body[1:]:
                ids += [_int(x, "set part") for x in _fields(ln, 10) if x.strip()]
            kf.part_sets[sid] = tuple(ids)
        elif kw == "*PART":
            title, card = body[0], body[1]
            f = _fields(card, 10)
            kf.parts[_int(f[0], "PID")] = (title.strip(), _int(f[1], "SECID"), _int(f[2], "MID"))
        elif kw == "*SECTION_SHELL":
            f1, f2 = _fields(body[0], 10), _fields(body[1], 10)
            kf.sections[_int(f1[0], "SECID")] = (_int(f1[1], "ELFORM"), _flt(f2[0], "T1"))
        elif kw == "*MAT_RIGID":
            f = _fields(body[0], 10)
            kf.rigid_mats[_int(f[0], "MID")] = (_flt(f[1], "RO"), _flt(f[2], "E"), _flt(f[3], "PR"))
    # 참조 무결성
    for eid, (_pid, conn) in list(kf.solids.items()) + list(kf.shells.items()):
        for n in conn:
            if n not in kf.nodes:
                raise KFormatError(f"element {eid} references undefined node {n}")
    for sid, (_t, ids) in kf.node_sets.items():
        for n in ids:
            if n not in kf.nodes:
                raise KFormatError(f"node set {sid} references undefined node {n}")
    return kf


def solid_topology(conn: tuple[int, ...]) -> str:
    """2줄 형식 절점 칸에서 토폴로지 판별."""
    if len(conn) == 10:
        return "tet10"
    if conn[3:] == (conn[3],) * 5:
        return "tet4"
    return "hex8"


def mapping_str(m: Mapping[str, object]) -> str:  # pragma: no cover - 디버그용
    return ", ".join(f"{k}={v}" for k, v in sorted(m.items()))
