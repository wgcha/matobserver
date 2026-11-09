"""LS-DYNA 키워드(.k) 리더와 고정 폭 형식 도구 (Impl-Solver 소유, module-boundaries.md §1 `deck/`).

* 리더: 키워드 블록 분해(`parse_k`), 필드 분리(고정 폭 10칸/20칸 long 형식 `+`, 쉼표 자유 형식),
  메시 파일 파싱(`read_mesh`, module-boundaries.md §2 규격 — Impl-Mesh M-8 재파싱용 공개 함수),
  `*PARAMETER` 읽기, `*INCLUDE` 목록.
* 형식 도구: 칸에 드는 가장 정밀한 숫자(`fit`), 정수 칸, 주석 줄.

단위 변환은 하지 않는다(파일 값 그대로). SI 환산은 호출자(writer/reader 경계)가 `core.units`로 한다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

STANDARD_WIDTH = 10
LONG_WIDTH = 20

_PARAM_REF_RE = re.compile(r"^-?&([A-Za-z][A-Za-z0-9_]*)$")


# ======================================================================================
# 형식 도구
# ======================================================================================
def fit(value: float, width: int = STANDARD_WIDTH) -> str:
    """`width` 칸에 드는 가장 정밀한 숫자(오른쪽 맞춤). 되읽어 같은 값이 되는 가장 짧은 표기를 우선한다.

    MatNexus `matcore/export/template.fit`과 같은 규칙(참조만, import 하지 않음): 지수의 앞 0을 떼어
    (`E-09`→`E-9`) 유효숫자 한 자리를 더 얻는다.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} cannot be written to a deck field")
    if number == 0.0:
        return "0.0".rjust(width)
    shortest = repr(number)
    if "e" not in shortest and len(shortest) <= width:
        return shortest.rjust(width)
    found: list[str] = []
    for digits in range(0, 17):
        mantissa, exponent = f"{number:.{digits}E}".split("E")
        text = f"{mantissa}E{int(exponent):+d}"
        if len(text) > width:
            break
        found = [text]
        if float(text) == number:
            break
    for decimals in range(width, 0, -1):
        text = f"{number:.{decimals}f}"
        if len(text) <= width:
            if float(text) != 0.0:
                found.append(text)
            break
    if not found:
        raise ValueError(f"{number!r} does not fit in {width} columns")
    best = min(found, key=lambda t: (abs(float(t) - number), len(t)))
    return best.rjust(width)


def fit_rel_error(value: float, width: int = STANDARD_WIDTH) -> float:
    """`fit(value, width)` 표기의 상대 오차."""
    v = float(value)
    if v == 0.0:
        return 0.0
    return abs(float(fit(v, width)) - v) / abs(v)


def i_field(value: int, width: int = STANDARD_WIDTH) -> str:
    text = f"{int(value):d}"
    if len(text) > width:
        raise ValueError(f"integer {value} does not fit in {width} columns")
    return text.rjust(width)


def s_field(text: str, width: int = STANDARD_WIDTH) -> str:
    """문자열 칸(예: `&mo_dtout` 파라미터 참조). 칸보다 길면 오류."""
    if len(text) > width:
        raise ValueError(f"field {text!r} does not fit in {width} columns")
    return text.rjust(width)


def card(fields: Sequence[str | int | float | None], width: int = STANDARD_WIDTH) -> str:
    """한 줄 카드. int → 정수 칸, float → fit, str → 그대로(오른쪽 맞춤), None → 빈 칸. 끝 공백은 지운다."""
    out = []
    for f in fields:
        if f is None:
            out.append(" " * width)
        elif isinstance(f, bool):
            raise TypeError("bool is not a deck field")
        elif isinstance(f, int):
            out.append(i_field(f, width))
        elif isinstance(f, float):
            out.append(fit(f, width))
        else:
            out.append(s_field(str(f), width))
    return "".join(out).rstrip()


def comment(*names: str, width: int = STANDARD_WIDTH) -> str:
    """`$#` 열 이름 주석 줄."""
    body = "".join(n.rjust(width) for n in names)
    return "$#" + body[2:] if len(body) >= 2 else "$#"


# ======================================================================================
# 리더
# ======================================================================================
def normalize_newlines(text: str) -> str:
    """CRLF·CR → LF. 읽기는 줄바꿈 종류와 무관하게 같은 결과를 낸다(Windows 덱·로그 허용)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str) -> list[str]:
    """줄 목록 (CRLF·CR 허용). str.splitlines 와 달리 \f·\v 등에서는 나누지 않는다."""
    lines = normalize_newlines(text).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_text(path: Path) -> str:
    """덱·로그 파일 텍스트 (줄바꿈 LF 정규화, 인코딩 오류는 대체 문자)."""
    return normalize_newlines(Path(path).read_bytes().decode("utf-8", errors="replace"))
@dataclass
class KeywordBlock:
    """키워드 하나와 데이터 줄(주석 제외). `name`은 `*` 없이 대문자, 옵션 접미사 포함(예 `SET_NODE_LIST_TITLE`)."""

    name: str
    raw: str  # 원래 키워드 줄
    lines: list[str] = field(default_factory=list)
    line_no: int = 0  # 키워드 줄 번호(1부터)
    long_format: bool = False  # 키워드 끝 `+` (20칸)
    source: str = ""

    @property
    def width(self) -> int:
        return LONG_WIDTH if self.long_format else STANDARD_WIDTH

    def fields(self, index: int, widths: Sequence[int] | None = None) -> list[str]:
        return split_fields(self.lines[index], widths or self.width)

    def has_title(self) -> bool:
        return self.name.endswith("_TITLE")


def _keyword_name(line: str) -> tuple[str, bool]:
    head = line[1:].strip()
    long_format = False
    token = head.split()[0] if head.split() else ""
    rest = head[len(token):].strip()
    if token.endswith("+"):
        long_format = True
        token = token[:-1]
    elif rest.startswith("+"):
        long_format = True
    elif token.endswith("-"):
        token = token[:-1]
    return token.upper(), long_format


def parse_k(text: str, source: str = "") -> list[KeywordBlock]:
    """키워드 블록 목록. `$` 주석 줄과 빈 줄은 버린다. `*END` 뒤는 읽지 않는다. `*KEYWORD` 자체도 블록으로 남긴다."""
    blocks: list[KeywordBlock] = []
    current: KeywordBlock | None = None
    for no, line in enumerate(split_lines(text), start=1):
        if not line.strip() or line.lstrip().startswith("$"):
            continue
        if line.startswith("*"):
            name, long_format = _keyword_name(line)
            current = KeywordBlock(name=name, raw=line, line_no=no, long_format=long_format, source=source)
            blocks.append(current)
            if name == "END":
                break
            continue
        if current is None:
            continue  # 첫 키워드 전 데이터는 무시(LS-DYNA도 무시)
        current.lines.append(line)
    return blocks


def read_k(path: Path) -> list[KeywordBlock]:
    return parse_k(read_text(path), source=str(path))


def split_fields(line: str, widths: int | Sequence[int] = STANDARD_WIDTH) -> list[str]:
    """필드 문자열 목록(공백 제거). 쉼표가 있으면 자유 형식, 아니면 고정 폭."""
    if "," in line:
        return [f.strip() for f in line.split(",")]
    out: list[str] = []
    if isinstance(widths, int):
        pos = 0
        while pos < len(line):
            out.append(line[pos:pos + widths].strip())
            pos += widths
        return out
    pos = 0
    for w in widths:
        if pos >= len(line):
            break
        out.append(line[pos:pos + w].strip())
        pos += w
    return out


def is_param_ref(text: str) -> bool:
    return bool(_PARAM_REF_RE.match(text.strip()))


def param_ref_name(text: str) -> str | None:
    m = _PARAM_REF_RE.match(text.strip())
    return m.group(1).lower() if m else None


def to_float(text: str, params: Mapping[str, float] | None = None, default: float = 0.0) -> float:
    """필드 → float. 빈 칸은 default, `&name`/`-&name`은 params에서(없으면 KeyError)."""
    t = text.strip()
    if not t:
        return default
    name = param_ref_name(t)
    if name is not None:
        if params is None or name not in params:
            raise KeyError(f"unresolved parameter reference {t!r}")
        v = float(params[name])
        return -v if t.startswith("-") else v
    # LS-DYNA는 `1.0-3` 같은 Fortran 지수도 허용한다
    m = re.match(r"^([-+]?(?:\d+\.?\d*|\.\d+))([-+]\d+)$", t)
    if m:
        return float(f"{m.group(1)}e{m.group(2)}")
    return float(t)


def to_int(text: str, params: Mapping[str, float] | None = None, default: int = 0) -> int:
    t = text.strip()
    if not t:
        return default
    return int(round(to_float(t, params, float(default))))


def read_parameters(blocks: Iterable[KeywordBlock]) -> dict[str, float]:
    """`*PARAMETER`(쉼표 `R name,value` 또는 고정 폭 `Rname     value`) → {소문자 이름: 값}. EXPRESSION은 읽지 않는다."""
    out: dict[str, float] = {}
    for b in blocks:
        if b.name not in ("PARAMETER", "PARAMETER_LOCAL", "PARAMETER_GLOBAL", "PARAMETER_NOECHO"):
            continue
        for line in b.lines:
            for typ, name, val in _parameter_entries(line):
                out[name] = float(val) if typ == "R" else float(int(float(val)))
    return out


def parameter_names_defined(blocks: Iterable[KeywordBlock]) -> list[str]:
    """`*PARAMETER*`·`*PARAMETER_EXPRESSION*`이 정의하는 이름(소문자)."""
    names: list[str] = []
    for b in blocks:
        if not b.name.startswith("PARAMETER"):
            continue
        for line in b.lines:
            if b.name.startswith("PARAMETER_EXPRESSION"):
                m = re.match(r"^\s*([RIC])?\s*([A-Za-z][A-Za-z0-9_]*)", line[:10] if "," not in line else line.split(",")[0])
                if m:
                    names.append(m.group(2).lower())
            else:
                names.extend(n for _, n, _ in _parameter_entries(line))
    return names


def _parameter_entries(line: str) -> Iterator[tuple[str, str, str]]:
    if "," in line:
        parts = [p.strip() for p in line.split(",")]
        for i in range(0, len(parts) - 1, 2):
            head, val = parts[i], parts[i + 1]
            m = re.match(r"^([RIC])?\s*([A-Za-z][A-Za-z0-9_]*)$", head)
            if m and val:
                yield (m.group(1) or "R").upper(), m.group(2).lower(), val
        return
    for i in range(0, len(line), 20):
        chunk = line[i:i + 20]
        head, val = chunk[:10].strip(), chunk[10:20].strip()
        m = re.match(r"^([RIC])?\s*([A-Za-z][A-Za-z0-9_]*)$", head)
        if m and val:
            yield (m.group(1) or "R").upper(), m.group(2).lower(), val


def includes(blocks: Iterable[KeywordBlock]) -> list[str]:
    """`*INCLUDE` 파일명 목록(첫 데이터 줄)."""
    out = []
    for b in blocks:
        if b.name == "INCLUDE" and b.lines:
            out.append(b.lines[0].strip())
    return out


def param_refs(text: str) -> set[str]:
    """덱 텍스트의 `&name` 참조(주석 제외, 소문자)."""
    refs: set[str] = set()
    for line in split_lines(text):
        if line.lstrip().startswith("$"):
            continue
        for m in re.finditer(r"&([A-Za-z][A-Za-z0-9_]*)", line):
            refs.add(m.group(1).lower())
    return refs


# ======================================================================================
# 메시 파일 (module-boundaries.md §2)
# ======================================================================================
@dataclass
class MeshData:
    """파일 단위(mm) 그대로의 메시 내용."""

    nodes: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    solids: dict[int, tuple[int, tuple[int, ...]]] = field(default_factory=dict)  # eid → (pid, 절점)
    shells: dict[int, tuple[int, tuple[int, ...]]] = field(default_factory=dict)
    node_sets: dict[int, tuple[int, ...]] = field(default_factory=dict)
    part_sets: dict[int, tuple[int, ...]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    header_comments: list[str] = field(default_factory=list)

    def solid_kind(self) -> str | None:
        kinds = set()
        for _, ns in self.solids.values():
            if len(ns) == 10:
                kinds.add("tet10")
            elif len(ns) == 8 and len(set(ns[3:])) == 1:
                kinds.add("tet4")
            else:
                kinds.add("hex8")
        if not kinds:
            return None
        if len(kinds) > 1:
            raise ValueError(f"mixed solid element formats in one file: {sorted(kinds)}")
        return kinds.pop()


def _ints(fields: Sequence[str]) -> list[int]:
    return [int(f) for f in fields if f.strip()]


def read_mesh(path: Path) -> MeshData:
    """메시 .k(시편·지그) 파싱. `*ELEMENT_SOLID`는 요소당 2줄 형식(1줄 = EID·PID)과 1줄 형식(EID PID N1..N8) 모두 읽는다."""
    text = read_text(path)
    md = MeshData(header_comments=[ln for ln in split_lines(text)[:6] if ln.startswith("$")])
    blocks = parse_k(text, source=str(path))
    for b in blocks:
        md.keywords.append(b.name)
        if b.name == "NODE":
            w = b.width
            for line in b.lines:
                if "," in line:
                    f = split_fields(line)
                else:
                    f = split_fields(line, (8, 16, 16, 16, 8, 8) if w == STANDARD_WIDTH else (20, 20, 20, 20, 20, 20))
                nid = int(f[0])
                md.nodes[nid] = (to_float(f[1]), to_float(f[2]), to_float(f[3]))
        elif b.name == "ELEMENT_SOLID":
            i = 0
            lines = b.lines
            while i < len(lines):
                f = _ints(split_fields(lines[i], 8))
                if len(f) == 2 and i + 1 < len(lines):
                    nodes = _ints(split_fields(lines[i + 1], 8))
                    md.solids[f[0]] = (f[1], tuple(nodes))
                    i += 2
                else:
                    md.solids[f[0]] = (f[1], tuple(f[2:]))
                    i += 1
        elif b.name == "ELEMENT_SHELL":
            for line in b.lines:
                f = _ints(split_fields(line, 8))
                md.shells[f[0]] = (f[1], tuple(f[2:6]))
        elif b.name in ("SET_NODE_LIST", "SET_NODE_LIST_TITLE", "SET_PART_LIST", "SET_PART_LIST_TITLE"):
            lines = b.lines[1:] if b.has_title() else b.lines
            if not lines:
                continue
            sid = int(split_fields(lines[0])[0])
            members: list[int] = []
            for line in lines[1:]:
                members.extend(_ints(split_fields(line)))
            target = md.node_sets if b.name.startswith("SET_NODE") else md.part_sets
            target[sid] = tuple(m for m in members if m != 0)
    return md
