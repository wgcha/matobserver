"""시편 메시 .k(module-boundaries.md §2 규격)에서 파트별 바운딩 박스를 읽는 최소 리더 (대리모델 단면 구성용).

`*NODE`: NID i8, X·Y·Z e16.9 (mm). `*ELEMENT_SOLID`: 요소당 2줄(EID PID / 절점 i8×최대 10). 1줄 형식(EID PID N1..N8)과
쉼표 자유 형식도 너그럽게 받는다. 결과는 SI(m).
"""

from __future__ import annotations

from pathlib import Path

from matobserver.core.units import mm_to_m


def _split(line: str, widths: tuple[int, ...]) -> list[str]:
    if "," in line:
        return [p.strip() for p in line.split(",")]
    out, pos = [], 0
    for w in widths:
        out.append(line[pos : pos + w].strip())
        pos += w
    return out


def part_bboxes(path: Path) -> dict[int, tuple[float, float, float, float, float, float]]:
    """PID → (xmin, xmax, ymin, ymax, zmin, zmax) [m]."""
    nodes: dict[int, tuple[float, float, float]] = {}
    elem_nodes: dict[int, list[int]] = {}
    section = None
    pending: tuple[int, int] | None = None
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("$"):
                continue
            if line.startswith("*"):
                kw = line.strip().upper()
                section = "node" if kw == "*NODE" else ("solid" if kw.startswith("*ELEMENT_SOLID") else None)
                if kw.startswith("*ELEMENT_SHELL"):
                    section = "shell"
                pending = None
                continue
            if section == "node":
                parts = _split(line, (8, 16, 16, 16))
                try:
                    nid = int(parts[0])
                    nodes[nid] = (float(parts[1]), float(parts[2]), float(parts[3]))
                except (ValueError, IndexError):
                    parts = line.split()
                    nid = int(parts[0])
                    nodes[nid] = (float(parts[1]), float(parts[2]), float(parts[3]))
            elif section in ("solid", "shell"):
                if "," in line:
                    ints = [int(p) for p in line.split(",") if p.strip()]
                else:
                    ints = [int(line[i : i + 8]) for i in range(0, len(line.rstrip()), 8) if line[i : i + 8].strip()]
                if pending is None:
                    if len(ints) == 2:
                        pending = (ints[0], ints[1])
                    elif len(ints) >= 3:  # 1줄 형식
                        elem_nodes.setdefault(ints[1], []).extend(ints[2:])
                else:
                    elem_nodes.setdefault(pending[1], []).extend(ints)
                    pending = None
    out: dict[int, tuple[float, float, float, float, float, float]] = {}
    for pid, nids in elem_nodes.items():
        pts = [nodes[n] for n in set(nids) if n in nodes]
        if not pts:
            continue
        xs, ys, zs = zip(*pts)
        out[pid] = (mm_to_m(min(xs)), mm_to_m(max(xs)), mm_to_m(min(ys)), mm_to_m(max(ys)),
                    mm_to_m(min(zs)), mm_to_m(max(zs)))
    return out
