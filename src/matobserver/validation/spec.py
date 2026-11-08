"""검증된 요청 스냅샷 <-> `CalibrationSpec` (module-boundaries.md §4.1).

잡 생성 시 `snapshot`(JSON)과 정규화 RunRecord 파일을 저장하고, 워커는 같은 `build_spec` 으로 `CalibrationSpec` 을 만든다.
검증 단계의 `engine.preflight` 도 같은 함수로 만든 `spec_draft` 를 받는다 -> 두 경로의 입력이 같다.

해석한 계약 모호점:
* `ResolvedTest.recipe_params_si` = 요청 `recipe_params` 덮어쓰기만 (SI). 시편 치수·기본값과의 우선순위 결정은 엔진 몫(api.md §4.2).
* `ResolvedTest.geometry["dimensions"]` = 대표 run(첫 run)의 치수 위에 요청 `geometry.dimensions` 를 얹은 값, `dimension_overrides` = 요청 값만.
* `ResolvedTest.user_window` = `{"x_min", "x_max"}` 또는 빈 매핑.
* `ResolvedMaterials.slots[i]` = `{slot, role, material_model(별칭 해석 후 레지스트리 이름), options(defaults 병합), params, member_position,
  reference(NeutralMaterial | None)}`.
* `CalibrationSpec.budget` = `{max_evaluations, max_wall_time_s, run_timeout_s}` 기본값 채움.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from ..core.types import (
    CalibrationSpec,
    GeometrySpec,
    ReferencePointRequest,
    ResolvedMaterials,
    ResolvedRecipe,
    ResolvedTest,
    RunRecord,
)
from ..recipes.cards import neutral_from_card_blocks


def run_to_json(rec: RunRecord) -> dict[str, Any]:
    return {
        "run_id": rec.run_id,
        "test_type_key": rec.test_type_key,
        "channels": {k: [float(x) for x in np.asarray(v).tolist()] for k, v in rec.channels.items()},
        "specimen_dimensions": dict(rec.specimen_dimensions),
        "conditions": dict(rec.conditions),
        "instrument": rec.instrument,
        "specimen_id": rec.specimen_id,
        "material_id": rec.material_id,
        "source_sha256": rec.source_sha256,
        "fetched_hash": rec.fetched_hash,
        "scalars": dict(rec.scalars),
    }


def run_from_json(d: Mapping[str, Any]) -> RunRecord:
    return RunRecord(
        run_id=d["run_id"],
        test_type_key=d["test_type_key"],
        channels={k: np.asarray(v, dtype=float) for k, v in d["channels"].items()},
        specimen_dimensions=dict(d.get("specimen_dimensions") or {}),
        conditions=dict(d.get("conditions") or {}),
        instrument=d.get("instrument"),
        specimen_id=d.get("specimen_id"),
        material_id=d.get("material_id"),
        source_sha256=d.get("source_sha256"),
        fetched_hash=d["fetched_hash"],
        scalars=dict(d.get("scalars") or {}),
    )


def geometry_spec_from_dict(g: Mapping[str, Any]) -> GeometrySpec:
    stp = g.get("stp_path")
    return GeometrySpec(
        kind=g["kind"],
        dimensions=dict(g.get("dimensions") or {}),
        layers=tuple((str(r), float(t)) for r, t in (g.get("layers") or ())),
        stack_axis=g.get("stack_axis") or "z",
        stp_path=None if not stp else Path(stp),
        placement=dict(g.get("placement") or {}),
        region_part_ids={str(k): int(v) for k, v in (g.get("region_part_ids") or {}).items()},
        reference_points=tuple(ReferencePointRequest(name=r["name"], locate=dict(r["locate"]))
                               for r in (g.get("reference_points") or ())),
    )


def build_spec(
    snapshot: Mapping[str, Any],
    *,
    job_id: str,
    runs: Mapping[str, RunRecord],
    recipe_resolver: Callable[[str], ResolvedRecipe],
    stp_paths: Mapping[str, Path] | None = None,
) -> CalibrationSpec:
    tests: list[ResolvedTest] = []
    for t in snapshot["tests"]:
        geometry = dict(t["geometry"])
        fid = geometry.get("file_id")
        if fid and stp_paths and fid in stp_paths:
            geometry["stp_path"] = str(stp_paths[fid])
        recipe = recipe_resolver(t["recipe_ref"])
        tests.append(ResolvedTest(
            test_key=t["test_key"], test_type=t["test_type"], recipe=recipe, geometry=geometry,
            runs=tuple(runs[rid] for rid in t["run_ids"]), displacement_channel=t["displacement_channel"],
            recipe_params_si=dict(t.get("recipe_params_si") or {}), user_window=dict(t.get("user_window") or {}),
            weight=float(t.get("weight", 1.0)),
        ))
    slots: list[Mapping[str, Any]] = []
    for s in snapshot["materials"]["slots"]:
        d = dict(s)
        ref = d.get("reference")
        d["reference"] = None if ref is None else neutral_from_card_blocks(ref)
        slots.append(d)
    return CalibrationSpec(
        job_id=job_id,
        schema_version=snapshot["schema_version"],
        tests=tuple(tests),
        materials=ResolvedMaterials(mode=snapshot["materials"]["mode"], slots=tuple(slots)),
        optimizer=(snapshot["optimizer"]["name"], dict(snapshot["optimizer"].get("options") or {})),
        elastic_optimizer=(snapshot["elastic_optimizer"]["name"], dict(snapshot["elastic_optimizer"].get("options") or {})),
        solver=(snapshot["solver"]["name"], dict(snapshot["solver"].get("options") or {})),
        friction=dict(snapshot.get("friction") or {}),
        budget=dict(snapshot["budget"]),
        seed=int(snapshot["seed"]),
        request_hash=snapshot["request_hash"],
    )
