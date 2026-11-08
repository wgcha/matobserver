"""재료 모델 공통 부분 (plugins.md §4.3).

* `bind(reference)`: ν·ρ·기준 E를 받는다. reference 가 없으면 ν·ρ는 옵션 `poisson_ratio`·`density` 또는 기본값
  (ν 0.3, ρ = 옵션 `default_density_kg_m3` 또는 1000 kg/m³). 잡에서는 Backend 검증(POISSON_RATIO_REQUIRED, DENSITY_DEFAULTED)이 먼저다.
* `initial_guess(hints)`: TestSpec 힌트(SI: `E0`, `sigma_y0`, `eps_p_max0`)를 **저장**하고 초기값을 돌려준다.
  `param_space()`의 기본 범위가 힌트에 의존하므로(E [E0/10, 10·E0] 등) 엔진은 bind → initial_guess → param_space 순으로 부른다.
  힌트가 없으면(STP) `default_E_range_pa`·`default_sigma_y_range_pa` 옵션(설정 `material.*`를 엔진이 주입) 범위를 쓴다.
* 옵션의 설정 유래 키(엔진 주입): `default_E_range_pa`, `default_sigma_y_range_pa`, `default_density_kg_m3`.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from matobserver.core.types import ElasticBlock, NeutralMaterial, ParamSpec

DEFAULT_E_RANGE = (1e6, 1e12)
DEFAULT_SIGY_RANGE = (1e5, 5e9)


def _pair(v: Any, default: tuple[float, float]) -> tuple[float, float]:
    if v is None:
        return default
    lo, hi = float(v[0]), float(v[1])
    return (lo, hi)


def geo_mid(lo: float, hi: float) -> float:
    return math.sqrt(lo * hi)


def clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


class MaterialBase:
    """bind·힌트·탄성 블록·E 변수 공통 처리."""

    name = "base"
    version = "0.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self._reference: NeutralMaterial | None = None
        self._hints: dict[str, float] = {}
        self._E_range_default = _pair(self.options.get("default_E_range_pa"), DEFAULT_E_RANGE)
        self._sigy_range_default = _pair(self.options.get("default_sigma_y_range_pa"), DEFAULT_SIGY_RANGE)

    # ---- 계약 ----
    def bind(self, reference: NeutralMaterial | None) -> None:
        self._reference = reference

    def initial_guess(self, hints: Mapping[str, float]) -> Mapping[str, float]:
        self._hints = {k: float(v) for k, v in (hints or {}).items()
                       if k in ("E0", "sigma_y0", "eps_p_max0") and v is not None and math.isfinite(float(v)) and float(v) > 0}
        return {p.name: float(p.initial) for p in self.param_space().params if p.initial is not None}

    # ---- 보조 ----
    @property
    def nu(self) -> float:
        if self._reference is not None:
            return float(self._reference.elastic.poisson_ratio)
        return float(self.options.get("poisson_ratio", 0.3))

    @property
    def rho(self) -> float:
        if self._reference is not None:
            return float(self._reference.elastic.density)
        return float(self.options.get("density", self.options.get("default_density_kg_m3", 1000.0)))

    @property
    def E_ref(self) -> float | None:
        if self._reference is not None and self._reference.elastic.youngs_modulus > 0:
            return float(self._reference.elastic.youngs_modulus)
        return None

    def elastic_block(self, E: float) -> ElasticBlock:
        return ElasticBlock(youngs_modulus=float(E), poisson_ratio=self.nu, density=self.rho)

    def E_spec(self) -> ParamSpec:
        E0 = self._hints.get("E0")
        if E0 is not None:
            lo, hi = E0 / 10.0, E0 * 10.0
            init = E0
        else:
            lo, hi = self._E_range_default
            init = self.E_ref if self.E_ref is not None else geo_mid(lo, hi)
        init = clamp(init, lo, hi)
        return ParamSpec("E", "Pa", lo, hi, init, "log", "elastic", "Young's modulus")

    def sigma_y0(self) -> float | None:
        s = self._hints.get("sigma_y0")
        if s is not None:
            return s
        if self._reference is not None and self._reference.table:
            return float(self._reference.table[0].true_stress)
        return None

    def reference_neutral(self, E: float, table) -> NeutralMaterial:
        return NeutralMaterial(elastic=self.elastic_block(E), table=table)
