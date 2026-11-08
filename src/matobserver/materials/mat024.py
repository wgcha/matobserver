"""MAT_024 등가 탄소성 모델 — `mat024_coeff`(경화식 계수), `mat024_table`(테이블 노드). plugins.md §4.3, result.md §3.1.

`mat024_coeff` 경화식 (εp = 진소성변형률, σ = 진응력, SI):

* swift:       σ = K(ε0 + εp)^n,          SIGY = K·ε0^n
* voce:        σ = σy + Q(1 − e^(−b·εp))
* swift_voce:  σ = α·Swift + (1 − α)·Voce

`to_neutral`은 εp ∈ [0, `max_strain`]에서 `table_points`점을 샘플(첫 점 0, `table_spacing` geometric이면 첫 간격
`table_first_step`부터 기하 간격). 엔진은 결과 테이블을 εp_valid까지 다시 샘플하기 위해 같은 옵션 키로 인스턴스를 다시 만든다.

`mat024_table`: 노드 εp = 0 < εp_1 < … < εp_m(= `max_strain`), 노드 응력 = σy + Σ_{j≤i} dsig_j (dsig ≥ 0 → 단조성은 변수 정의로 보장, P-4).
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Mapping

import numpy as np

from matobserver.core.types import MaterialCapabilities, NeutralMaterial, ParamSpace, ParamSpec, TableRow

from ._base import MaterialBase, clamp, geo_mid

_LAWS = ("swift", "voce", "swift_voce")


def swift(eps_p: np.ndarray, K: float, eps0: float, n: float) -> np.ndarray:
    return K * np.power(eps0 + eps_p, n)


def voce(eps_p: np.ndarray, sigma_y: float, Q: float, b: float) -> np.ndarray:
    return sigma_y + Q * (1.0 - np.exp(-b * eps_p))


def strain_samples(max_strain: float, points: int, spacing: str, first_step: float) -> np.ndarray:
    """εp 샘플: 0 으로 시작, 엄격 증가, 마지막 = max_strain."""
    points = max(2, int(points))
    max_strain = float(max_strain)
    if max_strain <= 0:
        raise ValueError("max_strain must be > 0")
    if spacing == "linear" or points == 2 or first_step >= max_strain:
        return np.linspace(0.0, max_strain, points)
    tail = np.geomspace(first_step, max_strain, points - 1)
    tail[-1] = max_strain
    return np.concatenate([[0.0], tail])


class _Mat024Common(MaterialBase):
    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)

    def capabilities(self) -> MaterialCapabilities:
        return MaterialCapabilities(
            solvers=frozenset({"lsdyna", "surrogate_beam"}),
            solver_keyword={"lsdyna": "MAT_024", "surrogate_beam": "MAT_024"},
            stages=frozenset({"elastic", "plastic"}),
            produces_table=True,
            rate_dependent=False,
        )

    def _sigy_hint(self) -> float | None:
        return self._hints.get("sigma_y0")

    def _sigy_ref(self) -> float | None:
        if self._reference is not None and self._reference.table:
            return float(self._reference.table[0].true_stress)
        return None


class Mat024Coeff(_Mat024Common):
    name: ClassVar[str] = "mat024_coeff"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        law = self.options.get("hardening_law") or self.options.get("default_law") or "swift"
        if law not in _LAWS:
            raise ValueError(f"mat024_coeff: hardening_law must be one of {_LAWS}, got {law!r}")
        self.law = law
        self.table_points = int(self.options.get("table_points", 50))
        self.max_strain = float(self.options.get("max_strain", 0.2))
        self.table_spacing = str(self.options.get("table_spacing", "geometric"))
        self.first_step = float(self.options.get("table_first_step", 1e-4))
        if self.max_strain <= 0:
            raise ValueError("mat024_coeff: max_strain must be > 0")

    # ---- 변수 ----
    def _swift_specs(self) -> list[ParamSpec]:
        s0 = self._sigy_hint()
        eps0_i, n_i = 0.005, 0.2
        if s0 is not None:
            K_lo, K_hi = s0, 20.0 * s0
            K_i = s0 / eps0_i**n_i
        else:
            K_lo, K_hi = self._sigy_range_default
            ref = self._sigy_ref()
            K_i = ref / eps0_i**n_i if ref is not None else geo_mid(K_lo, K_hi)
        return [
            ParamSpec("K", "Pa", K_lo, K_hi, clamp(K_i, K_lo, K_hi), "log", "plastic", "Swift strength coefficient"),
            ParamSpec("eps0", "1", 1e-4, 0.05, eps0_i, "log", "plastic", "Swift strain offset"),
            ParamSpec("n", "1", 0.01, 0.8, n_i, "linear", "plastic", "Swift hardening exponent"),
        ]

    def _voce_specs(self) -> list[ParamSpec]:
        s0 = self._sigy_hint()
        if s0 is not None:
            lo, hi, init = 0.3 * s0, 3.0 * s0, s0
            q_hi = 5.0 * s0
        else:
            lo, hi = self._sigy_range_default
            ref = self._sigy_ref()
            init = ref if ref is not None else geo_mid(lo, hi)
            q_hi = 5.0 * init
        return [
            ParamSpec("sigma_y", "Pa", lo, hi, clamp(init, lo, hi), "linear" if s0 is not None else "log", "plastic",
                      "Voce initial yield stress"),
            ParamSpec("Q", "Pa", 0.0, q_hi, 0.5 * clamp(init, lo, hi), "linear", "plastic", "Voce saturation increment"),
            ParamSpec("b", "1", 1.0, 500.0, 20.0, "log", "plastic", "Voce rate"),
        ]

    def param_space(self) -> ParamSpace:
        specs = [self.E_spec()]
        if self.law in ("swift", "swift_voce"):
            specs += self._swift_specs()
        if self.law in ("voce", "swift_voce"):
            specs += self._voce_specs()
        if self.law == "swift_voce":
            specs.append(ParamSpec("alpha", "1", 0.0, 1.0, 0.5, "linear", "plastic", "Swift weight"))
        return ParamSpace(params=tuple(specs))

    # ---- 응력 ----
    def stress(self, eps_p: np.ndarray, theta: Mapping[str, float]) -> np.ndarray:
        eps_p = np.asarray(eps_p, dtype=float)
        if self.law == "swift":
            return swift(eps_p, theta["K"], theta["eps0"], theta["n"])
        if self.law == "voce":
            return voce(eps_p, theta["sigma_y"], theta["Q"], theta["b"])
        a = float(theta["alpha"])
        return a * swift(eps_p, theta["K"], theta["eps0"], theta["n"]) + (1.0 - a) * voce(
            eps_p, theta["sigma_y"], theta["Q"], theta["b"])

    def strains(self) -> np.ndarray:
        return strain_samples(self.max_strain, self.table_points, self.table_spacing, self.first_step)

    def to_neutral(self, theta_local: Mapping[str, float]) -> NeutralMaterial:
        eps = self.strains()
        sig = self.stress(eps, theta_local)
        sig = np.maximum.accumulate(sig)  # 반올림 보호(해석식은 이미 비감소)
        rows = tuple(TableRow(float(e), float(s)) for e, s in zip(eps, sig))
        return NeutralMaterial(elastic=self.elastic_block(float(theta_local["E"])), table=rows)

    def describe(self, theta_local: Mapping[str, float]) -> Mapping[str, Any]:
        keys = {"swift": ("K", "eps0", "n"), "voce": ("sigma_y", "Q", "b"),
                "swift_voce": ("K", "eps0", "n", "sigma_y", "Q", "b", "alpha")}[self.law]
        coeffs = {k: float(theta_local[k]) for k in keys if k in theta_local}
        if self.law == "swift" and all(k in coeffs for k in keys):
            coeffs_derived = {"sigy": coeffs["K"] * coeffs["eps0"] ** coeffs["n"]}
        else:
            coeffs_derived = {}
        return {"hardening": {"law": self.law, "coefficients": coeffs}, "derived": coeffs_derived,
                "table_points": self.table_points, "max_strain": self.max_strain}


class Mat024Table(_Mat024Common):
    name: ClassVar[str] = "mat024_table"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)
        self.table_nodes = int(self.options.get("table_nodes", 8))
        if self.table_nodes < 2:
            raise ValueError("mat024_table: table_nodes must be >= 2")
        self.node_spacing = str(self.options.get("node_spacing", "geometric"))
        self.max_strain = float(self.options.get("max_strain", 0.2))
        if self.node_spacing == "explicit":
            nodes = [float(v) for v in self.options.get("strain_nodes") or []]
            if len(nodes) < 2 or nodes[0] != 0.0 or any(b <= a for a, b in zip(nodes, nodes[1:])):
                raise ValueError("mat024_table: strain_nodes must start at 0 and be strictly increasing (>= 2 nodes)")
            self.nodes = np.asarray(nodes)
            self.table_nodes = len(nodes)
            self.max_strain = nodes[-1]
        else:
            m = self.table_nodes - 1
            first = float(self.options.get("first_node_strain", self.max_strain / 100.0))
            if self.node_spacing == "linear" or m == 1:
                self.nodes = np.linspace(0.0, self.max_strain, m + 1)
            elif self.node_spacing == "geometric":
                tail = np.geomspace(first, self.max_strain, m)
                tail[-1] = self.max_strain
                self.nodes = np.concatenate([[0.0], tail])
            else:
                raise ValueError(f"mat024_table: node_spacing must be geometric|linear|explicit, got {self.node_spacing!r}")

    @property
    def m(self) -> int:
        return len(self.nodes) - 1

    def param_space(self) -> ParamSpace:
        s0 = self._sigy_hint()
        if s0 is not None:
            lo, hi, init, scale = 0.3 * s0, 3.0 * s0, s0, "linear"
        else:
            lo, hi = self._sigy_range_default
            ref = self._sigy_ref()
            init = ref if ref is not None else geo_mid(lo, hi)
            scale = "log"
        init = clamp(init, lo, hi)
        d_hi = 2.0 * init
        specs = [self.E_spec(), ParamSpec("sigma_y", "Pa", lo, hi, init, scale, "plastic", "table yield stress (eps_p=0)")]
        # 초기 증분: Swift 모양 σ = σy(1 + εp/0.002)^0.15
        shape = init * np.power(1.0 + self.nodes / 0.002, 0.15)
        d0 = np.diff(shape)
        for i in range(1, self.m + 1):
            specs.append(ParamSpec(f"dsig_{i}", "Pa", 0.0, d_hi, float(clamp(d0[i - 1], 0.0, d_hi)), "linear", "plastic",
                                   f"stress increment to node eps_p={self.nodes[i]:.6g}"))
        return ParamSpace(params=tuple(specs))

    def node_stresses(self, theta: Mapping[str, float]) -> np.ndarray:
        d = np.array([max(0.0, float(theta[f"dsig_{i}"])) for i in range(1, self.m + 1)])
        return float(theta["sigma_y"]) + np.concatenate([[0.0], np.cumsum(d)])

    def to_neutral(self, theta_local: Mapping[str, float]) -> NeutralMaterial:
        sig = self.node_stresses(theta_local)
        rows = tuple(TableRow(float(e), float(s)) for e, s in zip(self.nodes, sig))
        return NeutralMaterial(elastic=self.elastic_block(float(theta_local["E"])), table=rows)

    def describe(self, theta_local: Mapping[str, float]) -> Mapping[str, Any]:
        return {"hardening": {"law": None, "coefficients": None}, "node_strains": [float(v) for v in self.nodes],
                "table_nodes": self.table_nodes, "max_strain": float(self.max_strain)}
