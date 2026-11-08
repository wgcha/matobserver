"""대리모델 물리: 보 이론 + 섬유 단면 탄소성 굽힘 (3점굽힘, 변위 제어, 단조 하중).

* 단축 응력–변형률(인장·압축 대칭, J2 등방경화의 단축 등가): 탄성 E, 항복 후 테이블 σ(εp)을 구간 선형 보간,
  마지막 구간 기울기로 외삽(MAT_024 기본 동작과 같음). 총변형률 ε = σ/E + εp 이므로 σ(ε)도 구간 선형.
* 단면: (y, z) 직사각형 셀들의 섬유(z 방향 세분). 평면 유지, ε(z) = κ·(z − z_na), 축력 0으로 중립축을 이분 탐색.
* 처짐(단위 하중법): M(s) = F·s/2 (s = 지지점에서 거리), δ = (L²/4)·∫₀¹ κ(μ·M_mid)·μ dμ, F = 4·M_mid/L.
  중앙 곡률 κ_mid 격자 → (δ, F) 단조 곡선 → 출력 δ에서 F 보간.
* 단조 하중이므로 변형 이론(전변형률)과 증분 이론이 같다(각 단면 모멘트가 단조 증가).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Uniaxial:
    E: float
    e_nodes: np.ndarray | None  # 총변형률 노드(항복점 포함)
    s_nodes: np.ndarray | None  # 응력 노드
    slope_last: float  # 마지막 구간 dσ/dε

    @classmethod
    def from_card(cls, E: float, table: list[tuple[float, float]] | None) -> "Uniaxial":
        if not table:
            return cls(E=float(E), e_nodes=None, s_nodes=None, slope_last=float(E))
        ep = np.array([r[0] for r in table], dtype=float)
        sg = np.array([r[1] for r in table], dtype=float)
        e = sg / E + ep
        if len(e) >= 2 and e[-1] > e[-2]:
            slope = float((sg[-1] - sg[-2]) / (e[-1] - e[-2]))
        else:
            slope = 0.0
        return cls(E=float(E), e_nodes=e, s_nodes=sg, slope_last=slope)

    def stress(self, eps: np.ndarray) -> np.ndarray:
        eps = np.asarray(eps, dtype=float)
        if self.e_nodes is None:
            return self.E * eps
        a = np.abs(eps)
        e0 = self.e_nodes[0]
        s = np.where(a <= e0, self.E * a, np.interp(a, self.e_nodes, self.s_nodes))
        beyond = a > self.e_nodes[-1]
        if np.any(beyond):
            s = np.where(beyond, self.s_nodes[-1] + self.slope_last * (a - self.e_nodes[-1]), s)
        return np.sign(eps) * s

    def plastic_strain(self, eps: np.ndarray) -> np.ndarray:
        eps = np.abs(np.asarray(eps, dtype=float))
        return np.maximum(eps - self.stress(eps) / self.E, 0.0)

    def stress_at_plastic_strain(self, ep: np.ndarray) -> np.ndarray:
        """단축 1요소 응답: σ(εp) (테이블 보간 + 마지막 기울기 외삽)."""
        ep = np.asarray(ep, dtype=float)
        if self.e_nodes is None:
            return np.full_like(ep, np.nan)
        ep_nodes = self.e_nodes - self.s_nodes / self.E
        s = np.interp(ep, ep_nodes, self.s_nodes)
        if len(ep_nodes) >= 2:
            dsde = (self.s_nodes[-1] - self.s_nodes[-2]) / max(ep_nodes[-1] - ep_nodes[-2], 1e-300)
            s = np.where(ep > ep_nodes[-1], self.s_nodes[-1] + dsde * (ep - ep_nodes[-1]), s)
        return s


@dataclass(frozen=True)
class Section:
    z: np.ndarray  # 섬유 중심 z [m]
    area: np.ndarray  # 섬유 면적 [m²]
    mat: np.ndarray  # 섬유 재료 인덱스 (slots 순서)
    slots: tuple[str, ...]
    width: float
    height: float
    cells: tuple[tuple[int, float, float, float, float], ...] = ()  # (재료, y0, y1, z0, z1) — 탄성 EI 정확 계산용

    @classmethod
    def from_boxes(cls, boxes: list[tuple[str, float, float, float, float]], n_fibers: int = 240) -> "Section":
        """boxes: (slot, ymin, ymax, zmin, zmax). 셀 격자를 만들고 각 셀 중심을 포함하는 첫 박스의 슬롯으로 배정."""
        if not boxes:
            raise ValueError("section needs at least one box")
        slots: list[str] = []
        for b in boxes:
            if b[0] not in slots:
                slots.append(b[0])
        ys = np.unique(np.round([v for b in boxes for v in (b[1], b[2])], 15))
        zs = np.unique(np.round([v for b in boxes for v in (b[3], b[4])], 15))
        zmin, zmax = float(zs[0]), float(zs[-1])
        H = zmax - zmin
        Z, A, M = [], [], []
        cells: list[tuple[int, float, float, float, float]] = []
        for k in range(len(zs) - 1):
            z0, z1 = float(zs[k]), float(zs[k + 1])
            if z1 - z0 <= 1e-15:
                continue
            nz = max(4, int(round(n_fibers * (z1 - z0) / H)))
            edges = np.linspace(z0, z1, nz + 1)
            zc = 0.5 * (edges[:-1] + edges[1:])
            dz = np.diff(edges)
            for j in range(len(ys) - 1):
                y0, y1 = float(ys[j]), float(ys[j + 1])
                if y1 - y0 <= 1e-15:
                    continue
                yc, zcc = 0.5 * (y0 + y1), 0.5 * (z0 + z1)
                owner = None
                for b in boxes:
                    if b[1] - 1e-12 <= yc <= b[2] + 1e-12 and b[3] - 1e-12 <= zcc <= b[4] + 1e-12:
                        owner = b[0]
                        break
                if owner is None:
                    continue
                cells.append((slots.index(owner), y0, y1, z0, z1))
                Z.append(zc)
                A.append(dz * (y1 - y0))
                M.append(np.full(len(zc), slots.index(owner)))
        width = float(ys[-1] - ys[0])
        return cls(z=np.concatenate(Z), area=np.concatenate(A), mat=np.concatenate(M).astype(int),
                   slots=tuple(slots), width=width, height=H, cells=tuple(cells))


def _neutral_axis(sec: Section, mats: list[Uniaxial], kappa: np.ndarray) -> np.ndarray:
    """κ 배열마다 축력 0 중립축 z_na (이분 탐색, 벡터화)."""
    if len(mats) == 1 or all(m.e_nodes is None for m in mats):
        # 단일 재료(인장·압축 대칭) 또는 전부 탄성: 탄성 도심 = 중립축
        Ew = np.array([mats[i].E for i in sec.mat]) * sec.area
        zna = float(np.sum(Ew * sec.z) / np.sum(Ew))
        return np.full(len(kappa), zna)
    lo = np.full(len(kappa), float(sec.z.min()))
    hi = np.full(len(kappa), float(sec.z.max()))
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        eps = kappa[:, None] * (sec.z[None, :] - mid[:, None])
        N = np.zeros(len(kappa))
        for i, m in enumerate(mats):
            sel = sec.mat == i
            if np.any(sel):
                N += np.sum(m.stress(eps[:, sel]) * sec.area[sel], axis=1)
        # N(z_na) 는 z_na 증가에 따라 감소
        lo = np.where(N > 0, mid, lo)
        hi = np.where(N > 0, hi, mid)
    return 0.5 * (lo + hi)


def moment_curvature(sec: Section, mats: list[Uniaxial], kappa: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    zna = _neutral_axis(sec, mats, kappa)
    eps = kappa[:, None] * (sec.z[None, :] - zna[:, None])
    M = np.zeros(len(kappa))
    for i, m in enumerate(mats):
        sel = sec.mat == i
        if np.any(sel):
            M += np.sum(m.stress(eps[:, sel]) * sec.area[sel] * (sec.z[sel][None, :] - zna[:, None]), axis=1)
    return M, zna


def bending_ei(sec: Section, mats: list[Uniaxial]) -> float:
    if sec.cells:  # 직사각형 셀의 정확한 2차 모멘트
        Ec = np.array([mats[c[0]].E for c in sec.cells])
        b = np.array([c[2] - c[1] for c in sec.cells])
        h = np.array([c[4] - c[3] for c in sec.cells])
        zc = np.array([0.5 * (c[3] + c[4]) for c in sec.cells])
        zna = float(np.sum(Ec * b * h * zc) / np.sum(Ec * b * h))
        return float(np.sum(Ec * (b * h**3 / 12.0 + b * h * (zc - zna) ** 2)))
    E = np.array([mats[i].E for i in sec.mat])
    zna = float(np.sum(E * sec.area * sec.z) / np.sum(E * sec.area))
    return float(np.sum(E * sec.area * (sec.z - zna) ** 2))


@dataclass(frozen=True)
class BendingResult:
    delta: np.ndarray
    force: np.ndarray
    eps_p_max: dict[str, float]  # 슬롯별 최종 최대 유효소성변형률


def three_point_bending(sec: Section, mats: list[Uniaxial], span: float, delta_out: np.ndarray,
                        n_kappa: int = 320, n_mu: int = 800) -> BendingResult:
    delta_out = np.asarray(delta_out, dtype=float)
    dmax = float(np.max(delta_out)) if delta_out.size else 0.0
    L = float(span)
    plastic = any(m.e_nodes is not None for m in mats)
    if not plastic or dmax <= 0:
        EI = bending_ei(sec, mats)
        F = 48.0 * EI * delta_out / L**3
        return BendingResult(delta=delta_out, force=F, eps_p_max={s: 0.0 for s in sec.slots})
    mu = 1.0 - (1.0 - np.linspace(0.0, 1.0, n_mu)) ** 2  # μ=1(중앙) 근처 밀집
    kappa_hi = 12.0 * dmax / L**2 * 1.5
    for _ in range(12):
        t = np.linspace(0.0, 1.0, n_kappa)
        kgrid = kappa_hi * t**1.5
        M, _ = moment_curvature(sec, mats, kgrid)
        M = np.maximum.accumulate(M + 1e-300 * np.arange(len(M)))  # 단조(평탄부 보호)
        # δ(κ_mid) — κ(M) 역보간
        Mq = mu[None, :] * M[:, None]  # (n_kappa, n_mu)
        kq = np.interp(Mq.ravel(), M, kgrid).reshape(Mq.shape)
        integrand = kq * mu[None, :]
        delta = (L**2 / 4.0) * np.trapezoid(integrand, mu, axis=1)
        if delta[-1] >= dmax:
            break
        kappa_hi *= 2.0
    F = 4.0 * M / L
    order = np.argsort(delta, kind="stable")
    d_s, F_s = delta[order], F[order]
    d_u, idx = np.unique(d_s, return_index=True)
    F_out = np.interp(delta_out, d_u, F_s[idx])
    # 최종 상태 슬롯별 최대 소성변형률 (중앙 단면)
    k_end = float(np.interp(dmax, d_u, kgrid[order][idx]))
    _, zna = moment_curvature(sec, mats, np.array([k_end]))
    eps = k_end * (sec.z - zna[0])
    epm = {}
    for i, s in enumerate(sec.slots):
        sel = sec.mat == i
        epm[s] = float(np.max(mats[i].plastic_strain(eps[sel]))) if np.any(sel) else 0.0
    return BendingResult(delta=delta_out, force=F_out, eps_p_max=epm)
