"""tet4 슬리버 개선: 내부(체적 분류) 절점 국소 재배치 — 결정적(난수 없음, 절점 번호순).

나쁜 요소(scaled Jacobian < target)에 붙은 자유 절점마다 후보 위치
(현재, 이웃 중심 쪽 α 이동, 최악 요소 대면 법선 ±β 이동) 중 주변 요소 최소 SJ가 가장 큰 곳으로 옮긴다.
경계 절점은 움직이지 않으므로 형상(체적)은 변하지 않는다.
"""

from __future__ import annotations

import numpy as np

from .elements import tet4_quality, tet4_volume

_ALPHAS = (0.25, 0.5, 0.75, 1.0)
_BETAS = (-0.3, -0.2, -0.1, 0.1, 0.2, 0.3)


def _incidence(tets: np.ndarray, n_nodes: int) -> tuple[np.ndarray, np.ndarray]:
    flat = tets.ravel()
    order = np.argsort(flat, kind="stable")
    elem_of = order // 4
    counts = np.bincount(flat, minlength=n_nodes)
    ptr = np.concatenate([[0], np.cumsum(counts)])
    return ptr, elem_of


def improve(nodes: np.ndarray, tets: np.ndarray, free: np.ndarray, *, target: float = 0.3,
            passes: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """반환 (새 좌표, 움직인 절점 인덱스). free = bool (N,)."""
    X = nodes.copy()
    ptr, elem_of = _incidence(tets, len(X))
    moved: set[int] = set()
    for _ in range(passes):
        sj, _ar = tet4_quality(X[tets])
        bad = np.nonzero(sj < target)[0]
        if not len(bad):
            break
        cand_nodes = sorted({int(n) for n in tets[bad].ravel() if free[n]})
        improved = False
        for n in cand_nodes:
            inc = elem_of[ptr[n]:ptr[n + 1]]
            T = tets[inc]
            cur = float(tet4_quality(X[T])[0].min())
            if cur >= target:
                continue
            nb = np.unique(T.ravel())
            nb = nb[nb != n]
            centroid = X[nb].mean(axis=0)
            L = float(np.linalg.norm(X[nb] - X[n], axis=1).mean())
            sjs = tet4_quality(X[T])[0]
            worst = T[int(np.argmin(sjs))]
            others = [int(m) for m in worst if m != n]
            a, b, c = X[others]
            nrm = np.cross(b - a, c - a)
            nn = np.linalg.norm(nrm)
            cands = [X[n] + al * (centroid - X[n]) for al in _ALPHAS]
            if nn > 0:
                nrm = nrm / nn
                if np.dot(X[n] - a, nrm) < 0:
                    nrm = -nrm
                cands += [X[n] + be * L * nrm for be in _BETAS]
            best, best_q = None, cur
            old = X[n].copy()
            for p in cands:
                X[n] = p
                Y = X[T]
                if (tet4_volume(Y) <= 0).any():
                    continue
                q = float(tet4_quality(Y)[0].min())
                if q > best_q + 1e-12:
                    best, best_q = p.copy(), q
            X[n] = best if best is not None else old
            if best is not None:
                moved.add(n)
                improved = True
        if not improved:
            break
    return X, np.array(sorted(moved), dtype=np.int64)
