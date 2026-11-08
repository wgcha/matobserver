"""요소 기하: 체적·scaled Jacobian·aspect ratio (벡터화, 단위 무관).

정의(계약에 지표 정의가 없어 아래로 고정 — 반환 보고에 기재):

* scaled Jacobian: 각 꼭짓점에서 인접 세 변 벡터 (a, b, c)의 det[a b c] / (|a||b||c|)의 최솟값.
  hex8 = 8 꼭짓점 최소(직육면체 1). tet = 4 꼭짓점 최소 × √2 (정사면체 1, 상한 1로 자름).
  tet10 = min(꼭짓점 사면체 값, Jacobian 비 min detJ / max detJ (꼭짓점·중간절점·중심 11점)).
* aspect ratio: 요소 최장 변 / 최단 변 (tet10은 꼭짓점 변).
* 체적: hex8 2×2×2 Gauss(삼선형 detJ 정확), tet4 det/6, tet10 Keast 5점(3차, 2차 사면체 detJ 정확).

LS-DYNA 절점 순서(module-boundaries.md §2): hex8 N1–N4 아랫면, N5–N8 윗면(오른손 법칙으로 양의 체적),
tet N1–N4 꼭짓점, tet10 N5=1-2, N6=2-3, N7=3-1, N8=1-4, N9=2-4, N10=3-4.
"""

from __future__ import annotations

import numpy as np

# hex 꼭짓점별 (a, b, c) 인접 절점 (양의 방향)
_HEX_CORNERS = np.array([
    [0, 1, 3, 4], [1, 2, 0, 5], [2, 3, 1, 6], [3, 0, 2, 7],
    [4, 7, 5, 0], [5, 4, 6, 1], [6, 5, 7, 2], [7, 6, 4, 3],
])
_HEX_EDGES = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
                       [0, 4], [1, 5], [2, 6], [3, 7]])
_TET_CORNERS = np.array([[0, 1, 2, 3], [1, 2, 0, 3], [2, 0, 1, 3], [3, 0, 2, 1]])
_TET_EDGES = np.array([[0, 1], [1, 2], [2, 0], [0, 3], [1, 3], [2, 3]])
#: tet10 중간절점(0-기준 인덱스 4..9)의 변 = LS-DYNA N5..N10
TET10_EDGES = _TET_EDGES

# tet 자연좌표 (xi, eta, zeta); L1 = 1-xi-eta-zeta, L2 = xi, L3 = eta, L4 = zeta
TET10_NODE_NATURAL = np.array([
    [0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
    [0.5, 0, 0], [0.5, 0.5, 0], [0, 0.5, 0], [0, 0, 0.5], [0.5, 0, 0.5], [0, 0.5, 0.5],
], dtype=float)
_KEAST5 = (np.array([[0.25, 0.25, 0.25], [1 / 6, 1 / 6, 1 / 6], [0.5, 1 / 6, 1 / 6], [1 / 6, 0.5, 1 / 6],
                     [1 / 6, 1 / 6, 0.5]]),
           np.array([-4 / 5, 9 / 20, 9 / 20, 9 / 20, 9 / 20]) / 6.0)


def _det3(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    return np.einsum("...i,...i->...", a, np.cross(b, c))


def _corner_sj(X: np.ndarray, table: np.ndarray) -> np.ndarray:
    o = X[:, table[:, 0]]
    a, b, c = X[:, table[:, 1]] - o, X[:, table[:, 2]] - o, X[:, table[:, 3]] - o
    la, lb, lc = (np.linalg.norm(v, axis=-1) for v in (a, b, c))
    with np.errstate(divide="ignore", invalid="ignore"):
        sj = _det3(a, b, c) / (la * lb * lc)
    return np.nan_to_num(sj, nan=-1.0).min(axis=1)


def _edge_ratio(X: np.ndarray, edges: np.ndarray) -> np.ndarray:
    L = np.linalg.norm(X[:, edges[:, 1]] - X[:, edges[:, 0]], axis=-1)
    with np.errstate(divide="ignore"):
        return L.max(axis=1) / L.min(axis=1)


# ---- hex8 ----
_G = 1.0 / np.sqrt(3.0)
_HEX_REF = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                     [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], dtype=float)


def _hex_dN(p: np.ndarray) -> np.ndarray:
    """(8, 3) 형상함수 미분 at 자연좌표 p."""
    r = _HEX_REF
    xi, eta, zeta = p
    d = np.empty((8, 3))
    d[:, 0] = r[:, 0] * (1 + r[:, 1] * eta) * (1 + r[:, 2] * zeta) / 8
    d[:, 1] = r[:, 1] * (1 + r[:, 0] * xi) * (1 + r[:, 2] * zeta) / 8
    d[:, 2] = r[:, 2] * (1 + r[:, 0] * xi) * (1 + r[:, 1] * eta) / 8
    return d


_HEX_GAUSS_DN = [_hex_dN(np.array([a, b, c]) * _G) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]


def hex_volume(X: np.ndarray) -> np.ndarray:
    vol = np.zeros(X.shape[0])
    for dN in _HEX_GAUSS_DN:
        J = np.einsum("eai,aj->eij", X, dN)
        vol += np.linalg.det(J)
    return vol


def hex_quality(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return _corner_sj(X, _HEX_CORNERS), _edge_ratio(X, _HEX_EDGES)


# ---- tet4 ----
def tet4_volume(X: np.ndarray) -> np.ndarray:
    return _det3(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0], X[:, 3] - X[:, 0]) / 6.0


def tet4_quality(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sj = np.minimum(_corner_sj(X[:, :4], _TET_CORNERS) * np.sqrt(2.0), 1.0)
    return sj, _edge_ratio(X[:, :4], _TET_EDGES)


# ---- tet10 ----
def _tet10_dN(p: np.ndarray) -> np.ndarray:
    xi, eta, zeta = p
    L1, L2, L3, L4 = 1 - xi - eta - zeta, xi, eta, zeta
    d = np.zeros((10, 3))
    d[0] = -(4 * L1 - 1)
    d[1, 0] = 4 * L2 - 1
    d[2, 1] = 4 * L3 - 1
    d[3, 2] = 4 * L4 - 1
    d[4] = [4 * (L1 - L2), -4 * L2, -4 * L2]
    d[5] = [4 * L3, 4 * L2, 0]
    d[6] = [-4 * L3, 4 * (L1 - L3), -4 * L3]
    d[7] = [-4 * L4, -4 * L4, 4 * (L1 - L4)]
    d[8] = [4 * L4, 0, 4 * L2]
    d[9] = [0, 4 * L4, 4 * L3]
    return d


_TET10_SAMPLE_DN = [_tet10_dN(p) for p in np.vstack([TET10_NODE_NATURAL, [[0.25, 0.25, 0.25]]])]
_TET10_KEAST_DN = [_tet10_dN(p) for p in _KEAST5[0]]


def tet10_detj(X: np.ndarray) -> np.ndarray:
    """(E, 11) detJ at 10 절점 + 중심."""
    return np.stack([np.linalg.det(np.einsum("eai,aj->eij", X, dN)) for dN in _TET10_SAMPLE_DN], axis=1)


def tet10_volume(X: np.ndarray) -> np.ndarray:
    vol = np.zeros(X.shape[0])
    for w, dN in zip(_KEAST5[1], _TET10_KEAST_DN):
        vol += w * np.linalg.det(np.einsum("eai,aj->eij", X, dN))
    return vol


def tet10_quality(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    sj, ar = tet4_quality(X[:, :4])
    dj = tet10_detj(X)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(dj.max(axis=1) > 0, dj.min(axis=1) / dj.max(axis=1), -1.0)
    return np.minimum(sj, ratio), ar


def volumes(topology: str, X: np.ndarray) -> np.ndarray:
    if topology == "hex8":
        return hex_volume(X)
    if topology == "tet4":
        return tet4_volume(X)
    if topology == "tet10":
        return tet10_volume(X)
    raise ValueError(topology)


def quality(topology: str, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if topology == "hex8":
        return hex_quality(X)
    if topology == "tet4":
        return tet4_quality(X)
    if topology == "tet10":
        return tet10_quality(X)
    raise ValueError(topology)


def min_detj(topology: str, X: np.ndarray) -> np.ndarray:
    """요소별 최소 detJ 근사(부호 검사용)."""
    if topology == "hex8":
        return np.stack([np.linalg.det(np.einsum("eai,aj->eij", X, dN)) for dN in _HEX_GAUSS_DN]
                        + [np.linalg.det(np.einsum("eai,aj->eij", X, _hex_dN(p))) for p in _HEX_REF], axis=1).min(1)
    if topology == "tet4":
        return tet4_volume(X)
    return tet10_detj(X).min(axis=1)
