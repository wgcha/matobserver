"""LS-DYNA 키워드(.k) 덱의 가벼운 스캐너 — 레시피 정적 검사(recipe.md §7.1)용.

완전한 파서가 아니다. 키워드 카드 경계와 데이터 줄만 나누고, 검사에 필요한 몇 가지 카드(`*INCLUDE`, `*PARAMETER`,
`*NODE`, `*ELEMENT_*`, `*CONTACT_*_ID`, `*PART`, `*SECTION_*`, `*MAT_*`, `*DEFINE_CURVE`, `*DATABASE_*`)의 첫 필드를 읽는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

_REF_RE = re.compile(r"&([A-Za-z_][A-Za-z0-9_]*)")
_INT_RE = re.compile(r"^[+-]?\d+$")


@dataclass
class Card:
    keyword: str  # '*' 뒤 대문자 이름 (예 "INCLUDE", "DATABASE_RCFORC")
    lineno: int
    data: list[tuple[int, str]] = field(default_factory=list)  # (줄 번호, 주석 아닌 데이터 줄)


def parse_cards(text: str) -> list[Card]:
    cards: list[Card] = []
    cur: Card | None = None
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("$"):
            continue
        if line.startswith("*"):
            kw = re.split(r"[\s,]", line[1:].strip(), maxsplit=1)[0].upper().rstrip("+-")
            cur = Card(keyword=kw, lineno=no)
            cards.append(cur)
        elif cur is not None:
            cur.data.append((no, line))
    return cards


def split_fields(line: str) -> list[str]:
    """데이터 줄 -> 필드. 쉼표가 있으면 쉼표 분리, 아니면 10칸 고정 폭(토큰이 칸 경계에 걸리면 공백 분리)."""
    if "," in line:
        return [f.strip() for f in line.split(",")]
    tokens = line.split()
    fixed = [line[i:i + 10].strip() for i in range(0, len(line), 10)]
    fixed_nonempty = [f for f in fixed if f]
    if fixed_nonempty == tokens:  # 모든 토큰이 한 칸 안에 들어간다 -> 고정 폭으로 읽어 빈 칸 위치를 보존
        return fixed
    return tokens


def first_int(line: str) -> int | None:
    if "," not in line and len(line) > 10 and line[:10].strip().lstrip("+-").isdigit():
        return int(line[:10])  # 고정 형식: 첫 필드는 1~10열 (뒤에 제목이 붙어도 읽는다)
    toks = line.replace(",", " ").split()
    if toks and _INT_RE.match(toks[0]):
        return int(toks[0])
    return None


def include_target(card: Card) -> str | None:
    """`*INCLUDE` 류 카드의 파일 경로 (첫 데이터 줄 전체). `*INCLUDE_PATH*`는 제외."""
    if not card.keyword.startswith("INCLUDE") or card.keyword.startswith("INCLUDE_PATH"):
        return None
    if not card.data:
        return None
    return card.data[0][1].strip().strip("'\"")


def parameter_defs(card: Card) -> Iterator[tuple[int, str]]:
    """`*PARAMETER`·`*PARAMETER_EXPRESSION` 에서 정의된 이름 (줄 번호, 소문자 이름)."""
    expr = card.keyword.startswith("PARAMETER_EXPRESSION")
    if card.keyword != "PARAMETER" and not expr:
        return
    for no, line in card.data:
        toks = line.replace(",", " ").split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if len(t) == 1 and t.upper() in "RICL":
                if i + 1 >= len(toks):
                    break
                yield no, toks[i + 1].lower()
                i += 3
            elif len(t) > 1 and t[0].upper() in "RICL" and not t[0].isdigit():
                yield no, t[1:].lower()
                i += 2
            else:
                i += 1
            if expr:
                break  # 수식 줄은 이름 하나뿐


def param_refs(card: Card) -> Iterator[tuple[int, str]]:
    """데이터 줄의 `&name` 참조 (줄 번호, 소문자 이름). `*PARAMETER_EXPRESSION` 수식은 제외."""
    if card.keyword.startswith("PARAMETER"):
        return
    for no, line in card.data:
        for m in _REF_RE.finditer(line):
            yield no, m.group(1).lower()


def card_id(card: Card, *, title_skip: bool | None = None) -> int | None:
    """카드의 첫 ID 필드 (MAT_/SECTION_/DEFINE_CURVE/CONTACT_*_ID). `_TITLE` 카드는 제목 줄을 건너뛴다."""
    lines = card.data
    skip = card.keyword.endswith("_TITLE") if title_skip is None else title_skip
    if skip:
        lines = lines[1:]
    if not lines:
        return None
    return first_int(lines[0][1])


def part_ids(card: Card) -> tuple[int | None, int | None, int | None]:
    """`*PART`: 제목 줄 다음 줄의 (PID, SECID, MID)."""
    if card.keyword != "PART" or len(card.data) < 2:
        return None, None, None
    toks = card.data[1][1].replace(",", " ").split()
    vals = [int(t) if _INT_RE.match(t) else None for t in toks[:3]]
    vals += [None] * (3 - len(vals))
    return vals[0], vals[1], vals[2]  # type: ignore[return-value]


def element_ids(card: Card) -> Iterator[int]:
    """`*ELEMENT_*` 의 요소 ID. `*ELEMENT_SOLID` 가 2줄 형식(EID PID / 절점)이면 첫 줄만 센다."""
    pending_nodes = False
    for _, line in card.data:
        toks = line.replace(",", " ").split()
        if not toks:
            continue
        if pending_nodes:
            pending_nodes = False
            continue
        v = first_int(line)
        if v is None:
            continue
        yield v
        if card.keyword.startswith("ELEMENT_SOLID") and len(toks) == 2:
            pending_nodes = True
