"""`mat001`: 탄성 전용 재료(MAT_001 등가) — 유리·실리콘·IC PKG 등가. plugins.md §4.3."""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

from matobserver.core.types import MaterialCapabilities, NeutralMaterial, ParamSpace

from ._base import MaterialBase


class Mat001(MaterialBase):
    name: ClassVar[str] = "mat001"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)

    def param_space(self) -> ParamSpace:
        return ParamSpace(params=(self.E_spec(),))

    def to_neutral(self, theta_local: Mapping[str, float]) -> NeutralMaterial:
        return NeutralMaterial(elastic=self.elastic_block(float(theta_local["E"])), table=None)

    def capabilities(self) -> MaterialCapabilities:
        return MaterialCapabilities(
            solvers=frozenset({"lsdyna", "surrogate_beam"}),
            solver_keyword={"lsdyna": "MAT_001", "surrogate_beam": "MAT_001"},
            stages=frozenset({"elastic"}),
            produces_table=False,
            rate_dependent=False,
        )

    def describe(self, theta_local: Mapping[str, float]) -> Mapping[str, Any]:
        return {"hardening": {"law": None, "coefficients": None}, "elastic_only": True}
