"""카드 블록(MatNexus 어휘, result.md §3) <-> NeutralMaterial 변환.

`reference_card.yaml`(레시피 기준 카드)과 요청 `materials.slots[].reference` 모두 같은 `card_blocks` 형식이다.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.types import ElasticBlock, NeutralMaterial, RateTableBlock, RateTableRow, TableRow


class CardBlocksError(ValueError):
    """card_blocks 형식 오류 (키·타입). 불변식 위반은 `neutral_material_violations` 가 따로 잡는다."""


def _num(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CardBlocksError(f"{where}: number expected, got {v!r}")
    return float(v)


def neutral_from_card_blocks(blocks: Mapping[str, Any]) -> NeutralMaterial:
    """`{elastic: {youngs_modulus, poisson_ratio, density}, table: [{plastic_strain, true_stress}]|null, rate_table}` -> NeutralMaterial."""
    if not isinstance(blocks, Mapping):
        raise CardBlocksError("card_blocks must be a mapping")
    extra = set(blocks) - {"elastic", "table", "rate_table"}
    if extra:
        raise CardBlocksError(f"unknown card block keys: {sorted(extra)}")
    el = blocks.get("elastic")
    if not isinstance(el, Mapping):
        raise CardBlocksError("card_blocks.elastic is required")
    elastic = ElasticBlock(
        youngs_modulus=_num(el.get("youngs_modulus"), "elastic.youngs_modulus"),
        poisson_ratio=_num(el.get("poisson_ratio"), "elastic.poisson_ratio"),
        density=_num(el.get("density"), "elastic.density"),
    )
    table = None
    raw_table = blocks.get("table")
    if raw_table is not None:
        if not isinstance(raw_table, list):
            raise CardBlocksError("card_blocks.table must be a list or null")
        rows = []
        for i, r in enumerate(raw_table):
            if not isinstance(r, Mapping):
                raise CardBlocksError(f"table[{i}] must be a mapping")
            rows.append(TableRow(_num(r.get("plastic_strain"), f"table[{i}].plastic_strain"),
                                 _num(r.get("true_stress"), f"table[{i}].true_stress")))
        table = tuple(rows)
    rate = None
    raw_rate = blocks.get("rate_table")
    if raw_rate is not None:
        if not isinstance(raw_rate, Mapping):
            raise CardBlocksError("card_blocks.rate_table must be a mapping or null")
        rrows = tuple(
            RateTableRow(_num(r.get("strain_rate"), "rate_table.rows.strain_rate"),
                         _num(r.get("plastic_strain"), "rate_table.rows.plastic_strain"),
                         _num(r.get("true_stress"), "rate_table.rows.true_stress"))
            for r in raw_rate.get("rows", [])
        )
        rate = RateTableBlock(values=dict(raw_rate.get("values", {})), rows=rrows)
    return NeutralMaterial(elastic=elastic, table=table, rate_table=rate)
