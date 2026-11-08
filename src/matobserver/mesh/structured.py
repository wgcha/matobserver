"""정렬 격자(육면체) 생성 + hex→tet(Kuhn 6분할, 적합) + tet10 중간절점. box·layered_box 공용."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .errors import MeshError

# 큐브 꼭짓점 비트 (dx, dy, dz) → hex 로컬 절점 (LS-DYNA 순서)
_BIT_TO_LOCAL = {(0, 0, 0): 0, (1, 0, 0): 1, (1, 1, 0): 2, (0, 1, 0): 3,
                 (0, 0, 1): 4, (1, 0, 1): 5, (1, 1, 1): 6, (0, 1, 1): 7}


def _kuhn_table() -> np.ndarray:
    rows = []
    for perm in itertools.permutations(range(3)):
        v = [0, 0, 0]
        path = [tuple(v)]
        for ax in perm:
            v[ax] = 1
            path.append(tuple(v))
        t = [_BIT_TO_LOCAL[p] for p in path]
        # 짝·홀 순열에 따라 방향 맞춤: det(e_p0, e_p1, e_p2) = sign(perm)
        inv = sum(1 for i in range(3) for j in range(i + 1, 3) if perm[i] > perm[j])
        if inv % 2:
            t[1], t[2] = t[2], t[1]
        rows.append(t)
    return np.array(rows)


KUHN = _kuhn_table()


def axis_points(a: float, b: float, n: int) -> np.ndarray:
    pts = np.linspace(a, b, n + 1)
    pts[0], pts[-1] = a, b
    return pts


def grid_hex(xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """노드 (N,3), hex (E,8), 셀 인덱스 (E,3)=(i,j,k). 노드 번호 = i + nx*(j + ny*k)."""
    nx, ny, nz = len(xs), len(ys), len(zs)
    K, J, I = np.meshgrid(np.arange(nz), np.arange(ny), np.arange(nx), indexing="ij")
    nodes = np.stack([xs[I.ravel()], ys[J.ravel()], zs[K.ravel()]], axis=1)

    def idx(i, j, k):
        return i + nx * (j + ny * k)

    K, J, I = (a.ravel() for a in np.meshgrid(np.arange(nz - 1), np.arange(ny - 1), np.arange(nx - 1), indexing="ij"))
    hexes = np.stack([idx(I, J, K), idx(I + 1, J, K), idx(I + 1, J + 1, K), idx(I, J + 1, K),
                      idx(I, J, K + 1), idx(I + 1, J, K + 1), idx(I + 1, J + 1, K + 1), idx(I, J + 1, K + 1)], axis=1)
    return nodes, hexes, np.stack([I, J, K], axis=1)


def hex_to_tet4(hexes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(6E, 4) tet, 부모 hex 인덱스 (6E,)."""
    tets = hexes[:, KUHN].reshape(-1, 4)
    parent = np.repeat(np.arange(len(hexes)), len(KUHN))
    return tets, parent


def add_midside(nodes: np.ndarray, tets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """tet4 → tet10: 변 중점 노드를 추가(변 사전순 정렬, 결정적). 반환 (nodes', tet10)."""
    from .elements import TET10_EDGES
    e = tets[:, TET10_EDGES]  # (E, 6, 2)
    key = np.sort(e.reshape(-1, 2), axis=1)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    mids = 0.5 * (nodes[uniq[:, 0]] + nodes[uniq[:, 1]])
    mid_idx = len(nodes) + inv.reshape(-1, 6)
    return np.vstack([nodes, mids]), np.hstack([tets, mid_idx])


@dataclass(frozen=True)
class AxisPlan:
    """한 축의 분할: 구간 경계(물리) + 구간별 요소 수."""
    breaks: tuple[float, ...]
    counts: tuple[int, ...]

    def points(self) -> np.ndarray:
        parts = [axis_points(self.breaks[i], self.breaks[i + 1], self.counts[i]) for i in range(len(self.counts))]
        out = [parts[0]] + [p[1:] for p in parts[1:]]
        return np.concatenate(out)

    @property
    def n(self) -> int:
        return int(sum(self.counts))

    def cell_segment(self) -> np.ndarray:
        """셀 → 구간 인덱스."""
        return np.repeat(np.arange(len(self.counts)), self.counts)

    def min_cell(self) -> float:
        return min((self.breaks[i + 1] - self.breaks[i]) / self.counts[i] for i in range(len(self.counts)))


def n_cells(length: float, h: float, *, minimum: int = 1, even: bool = False) -> int:
    n = max(minimum, int(math.ceil(length / h - 1e-9)))
    if even and n % 2:
        n += 1
    return n


def elements_per_cell(topology: str) -> int:
    return 6 if topology in ("tet4", "tet10") else 1


def build(topology: str, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(nodes, elems(LS-DYNA 순서), elem→cell(i,j,k))."""
    nodes, hexes, cells = grid_hex(xs, ys, zs)
    if topology == "hex8":
        return nodes, hexes, cells
    tets, parent = hex_to_tet4(hexes)
    if topology == "tet4":
        return nodes, tets, cells[parent]
    if topology == "tet10":
        nodes2, t10 = add_midside(nodes, tets)
        return nodes2, t10, cells[parent]
    raise MeshError(f"unsupported topology {topology!r}")


def aspect_cap(topology: str, max_aspect: float) -> float:
    """면내 셀 크기 상한 배율(두께 방향 셀 크기 대비): hex 는 변 비, tet(Kuhn)은 대각선 포함."""
    return 0.999 * max_aspect / (1.0 if topology == "hex8" else math.sqrt(3.0))


def plan_counts(lengths: Sequence[float], *, h: float, mins: Sequence[int], evens: Sequence[bool]) -> list[int]:
    return [n_cells(L, h, minimum=m, even=e) for L, m, e in zip(lengths, mins, evens)]
