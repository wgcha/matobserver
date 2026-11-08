"""시편 메셔 공통: 옵션, 검증 도우미, 영역→PID, 참조점, 대칭 세트, .k·result.json 작성, MeshResult 조립.

mesh 패키지 구성(소유: Impl-Mesh, plugins.md §4.4):
* `box:BoxMesher`(box), `box:LayeredBoxMesher`(layered_box) — 정렬 격자 hex8, tet4/tet10 은 Kuhn 6분할
* `stp:StpMesher`(stp) — gmsh 파이썬 API tet4/tet10 (gmsh 는 stp 사용 시에만 import)
* `jig:Bending3pRollersJig`(jig.bending3p_rollers) — 강체 셸 압자·지지 롤러
* `kfile` — §2 .k writer·reader(SI→mm 변환은 여기서만), `elements` — 체적·품질, `smooth` — tet 슬리버 개선
* 2D 축대칭(`quad4_axisym`)·셸 시편(`shell4`)은 2차 예약: 토폴로지 이름만 인지하고 MeshError 로 거절
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from ..core import errors as E
from ..core import units
from ..core.hashing import canonical_json_bytes, sha256_hex
from ..core.types import GeometrySpec, Issue, MeshOptions, MeshQuality, MeshResult, ReferencePointRequest
from . import elements, kfile
from .errors import MeshError

SPECIMEN_FILE = "specimen.k"
JIG_FILE = "jig.k"
RESULT_FILE = "result.json"
#: 참조점 노드 세트 ID 시작값(module-boundaries.md §2)
REF_SET_ID_START = 4_900_001
#: MeshOptions.node_set_ids 에 대칭 세트 ID가 없을 때 쓰는 대체 ID(계약 미규정 — 엔진은 매니페스트 값을 넣어야 한다)
FALLBACK_SYM_SET_IDS = {"sym_x0": 4_899_001, "sym_y0": 4_899_002}
SYMMETRY_FACTOR = {"none": 1.0, "half": 2.0, "quarter": 4.0}
SYMMETRY_PLANES = {"none": (), "half": ("y0",), "quarter": ("x0", "y0")}
SOLID_TOPOLOGIES = ("hex8", "tet4", "tet10")
RESERVED_TOPOLOGIES = ("quad4_axisym", "shell4")  # 2차 예약

#: 옵션 기본값 — 설정 `mesh.*` 키와 같은 값(config/plugins.d/mesh.yaml defaults 에도 같은 값)
DEFAULT_OPTIONS: dict[str, Any] = {
    "min_scaled_jacobian": 0.2,
    "max_aspect_ratio": 10.0,
    "volume_rel_tol_parametric": 1e-9,
    "volume_rel_tol_stp": 0.01,
    "gmsh_threads": 1,
    "coarsen_step": 1.1,
    "coarsen_max_iter": 60,
}


def merged_options(options: Mapping[str, Any] | None, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out = dict(DEFAULT_OPTIONS)
    if extra:
        out.update(extra)
    if options:
        out.update(options)
    return out


def issue(code: str, message: str, field: str | None = None, severity: str = "error", **ctx: Any) -> Issue:
    return Issue(code=code, message=message, severity=severity, field=field, context=ctx)  # type: ignore[arg-type]


def is_pos_finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def check_dimensions(spec: GeometrySpec, keys: Sequence[str]) -> list[Issue]:
    out: list[Issue] = []
    dims = spec.dimensions or {}
    for k in keys:
        if k not in dims:
            out.append(issue(E.GEOMETRY_DIMENSION_MISSING, f"dimension {k!r} missing", f"geometry.dimensions.{k}"))
        elif not is_pos_finite(dims[k]):
            out.append(issue(E.GEOMETRY_DIMENSION_INVALID, f"dimension {k!r} must be a positive finite number [m]",
                             f"geometry.dimensions.{k}", value=dims[k]))
    return out


def check_reference_points(points: Sequence[ReferencePointRequest]) -> list[Issue]:
    out: list[Issue] = []
    names = set()
    for i, rp in enumerate(points):
        f = f"geometry.reference_points[{i}]"
        if not getattr(rp, "name", None):
            out.append(issue(E.GEOMETRY_DIMENSION_INVALID, "reference point name missing", f))
            continue
        if rp.name in names:
            out.append(issue(E.GEOMETRY_DIMENSION_INVALID, f"duplicate reference point {rp.name!r}", f))
        names.add(rp.name)
        loc = rp.locate if isinstance(rp.locate, Mapping) else None
        if not loc or any(k not in ("x", "y", "z") for k in loc):
            out.append(issue(E.GEOMETRY_DIMENSION_INVALID, "locate must map x/y/z to a number or 'min'/'max'", f))
            continue
        for k, v in loc.items():
            ok = v in ("min", "max") or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
            if not ok:
                out.append(issue(E.GEOMETRY_DIMENSION_INVALID, f"locate.{k} must be a number [m] or 'min'/'max'",
                                 f"{f}.locate.{k}", value=v))
    return out


def resolve_pid(region_part_ids: Mapping[str, int], region: str, single_region: bool,
                aliases: Sequence[str] = ()) -> int | None:
    """영역 → PID. 정확한 키 > 별칭 키 > "*"(나머지 전부) > 단일 영역 형상의 "specimen"."""
    for key in (region, *aliases):
        if key in region_part_ids:
            return int(region_part_ids[key])
    if "*" in region_part_ids:
        return int(region_part_ids["*"])
    if single_region and "specimen" in region_part_ids:
        return int(region_part_ids["specimen"])
    return None


def check_pid_value(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 99_999_999


def check_options(opts: MeshOptions | None, allowed_topologies: Sequence[str]) -> None:
    if opts is None:
        raise MeshError("MeshOptions required for specimen meshers")
    if opts.topology in RESERVED_TOPOLOGIES:
        raise MeshError(f"topology {opts.topology!r} is reserved (2nd phase), not implemented")
    if opts.topology not in allowed_topologies:
        raise MeshError(f"topology {opts.topology!r} not supported (allowed: {', '.join(allowed_topologies)})")
    if not is_pos_finite(opts.target_size_m) or not is_pos_finite(opts.size_factor):
        raise MeshError("target_size_m and size_factor must be positive")
    if opts.symmetry not in SYMMETRY_FACTOR:
        raise MeshError(f"unknown symmetry {opts.symmetry!r}")
    if int(opts.max_elements) < 1 or int(opts.min_elems_through_thickness) < 1:
        raise MeshError("max_elements and min_elems_through_thickness must be ≥ 1")
    for name, (lo, hi) in (("node", opts.node_id_range), ("element", opts.element_id_range)):
        if not (0 < lo <= hi <= 99_999_999):
            raise MeshError(f"invalid {name} id range {lo}..{hi}")


def input_hash(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def spec_payload(spec: GeometrySpec, opts: MeshOptions, *, stp_sha256: str | None = None) -> dict[str, Any]:
    """해시 대상 입력(경로 대신 파일 내용 해시 → 작업 폴더가 달라도 같은 바이트)."""
    return {
        "kind": spec.kind,
        "dimensions": dict(spec.dimensions or {}),
        "layers": [list(x) for x in spec.layers],
        "stack_axis": spec.stack_axis,
        "stp_sha256": stp_sha256,
        "placement": dict(spec.placement or {}),
        "region_part_ids": dict(spec.region_part_ids or {}),
        "reference_points": [{"name": r.name, "locate": dict(r.locate)} for r in spec.reference_points],
        "opts": {
            "topology": opts.topology, "target_size_m": opts.target_size_m,
            "min_elems_through_thickness": opts.min_elems_through_thickness, "max_elements": opts.max_elements,
            "size_factor": opts.size_factor, "symmetry": opts.symmetry,
            "node_id_range": list(opts.node_id_range), "element_id_range": list(opts.element_id_range),
            "part_set_id": opts.part_set_id, "node_set_ids": dict(opts.node_set_ids or {}),
        },
    }


# ----------------------------------------------------------------------------------------------
# 조립
# ----------------------------------------------------------------------------------------------
@dataclass
class RawMesh:
    """좌표 m(대칭 영역), 요소 = 0-기준 노드 인덱스, 요소별 영역 인덱스."""
    nodes: np.ndarray  # (N, 3)
    elems: np.ndarray  # (E, k) k = 8 | 4 | 10 (LS-DYNA 순서)
    elem_region: np.ndarray  # (E,) int → region_names
    region_names: list[str]
    elems_through_thickness: int | None


def locate_node(nodes: np.ndarray, locate: Mapping[str, Any], symmetry: str) -> int:
    lo, hi = nodes.min(axis=0), nodes.max(axis=0)
    planes = SYMMETRY_PLANES[symmetry]
    target = np.zeros(3)
    mask = np.zeros(3, dtype=bool)
    for ax, key in enumerate("xyz"):
        if key not in locate:
            continue
        v = locate[key]
        mask[ax] = True
        if v == "min":
            target[ax] = lo[ax]
        elif v == "max":
            target[ax] = hi[ax]
        else:
            v = float(v)
            if (key == "x" and "x0" in planes) or (key == "y" and "y0" in planes):
                v = abs(v)  # 대칭 영역으로 접기
            target[ax] = v
    d = np.linalg.norm((nodes - target)[:, mask], axis=1) if mask.any() else np.zeros(len(nodes))
    return int(np.argmin(d))  # 동률이면 가장 작은 인덱스(결정적)


def top_z_at_x0(nodes: np.ndarray, elems: np.ndarray, corner_count: int) -> float:
    """하중선 x=0 위 시편 윗면 z: x=0 노드와 x=0을 가로지르는 꼭짓점 변의 보간점 중 최대 z."""
    x = nodes[:, 0]
    on = np.abs(x) <= 1e-12
    best = nodes[on, 2].max() if on.any() else -np.inf
    if corner_count == 8:
        edges = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4],
                          [0, 4], [1, 5], [2, 6], [3, 7]])
    else:
        edges = elements.TET10_EDGES
    a = elems[:, edges[:, 0]].ravel()
    b = elems[:, edges[:, 1]].ravel()
    xa, xb = x[a], x[b]
    cross = (xa * xb) < 0
    if cross.any():
        a, b, xa, xb = a[cross], b[cross], xa[cross], xb[cross]
        t = xa / (xa - xb)
        z = nodes[a, 2] + t * (nodes[b, 2] - nodes[a, 2])
        best = max(best, float(z.max()))
    if not math.isfinite(best):
        raise MeshError("specimen does not intersect the load line x=0")
    return float(best)


def write_result_json(out_dir: Path, key: str, data: Mapping[str, Any]) -> None:
    p = out_dir / RESULT_FILE
    doc: dict[str, Any] = {}
    if p.exists():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
    doc[key] = data
    p.write_text(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def mesh_result_json(r: MeshResult) -> dict[str, Any]:
    return {
        "mesh_file": r.mesh_path.name, "node_count": r.node_count, "element_count": r.element_count,
        "regions": {k: dict(v) for k, v in r.regions.items()}, "volume_m3": r.volume_m3,
        "reference_volume_m3": r.reference_volume_m3, "bbox_m": [list(r.bbox_m[0]), list(r.bbox_m[1])],
        "reference_nodes": dict(r.reference_nodes), "node_sets": dict(r.node_sets),
        "top_z_at_load_m": r.top_z_at_load_m, "bottom_z_m": r.bottom_z_m, "symmetry": r.symmetry,
        "quality": {"min_scaled_jacobian": r.quality.min_scaled_jacobian,
                    "max_aspect_ratio": r.quality.max_aspect_ratio,
                    "elems_through_thickness": r.quality.elems_through_thickness},
        "target_size_used_m": r.target_size_used_m,
        "warnings": [{"code": w.code, "message": w.message, "severity": w.severity} for w in r.warnings],
    }


def finalize_specimen(*, raw: RawMesh, spec: GeometrySpec, opts: MeshOptions, out_dir: Path, generator: str,
                      version: str, payload_hash: str, reference_volume_m3: float, target_size_used_m: float,
                      warnings: list[Issue], options: Mapping[str, Any],
                      region_aliases: Mapping[str, Sequence[str]] | None = None) -> MeshResult:
    """좌표 양자화 → 품질·체적 → ID 부여 → .k 작성 → MeshResult."""
    topo = opts.topology
    n_nodes, n_elems = len(raw.nodes), len(raw.elems)
    nlo, nhi = opts.node_id_range
    elo, ehi = opts.element_id_range
    if nlo + n_nodes - 1 > nhi:
        raise MeshError(f"{n_nodes} nodes exceed node id range {nlo}..{nhi}")
    if elo + n_elems - 1 > ehi:
        raise MeshError(f"{n_elems} elements exceed element id range {elo}..{ehi}")

    coords_mm = kfile.quantize_mm(raw.nodes)
    nodes_m = coords_mm / units.factor("length")
    X = nodes_m[raw.elems]
    vol = elements.volumes(topo, X)
    if (elements.min_detj(topo, X) <= 0).any() or (vol <= 0).any():
        raise MeshError("mesh has inverted or degenerate elements (non-positive Jacobian)")
    sj, ar = elements.quality(topo, X)
    q = MeshQuality(min_scaled_jacobian=float(sj.min()), max_aspect_ratio=float(ar.max()),
                    elems_through_thickness=raw.elems_through_thickness)
    if q.min_scaled_jacobian < float(options["min_scaled_jacobian"]) or \
            q.max_aspect_ratio > float(options["max_aspect_ratio"]):
        warnings.append(issue("MESH_QUALITY_LOW",
                              f"mesh quality below limits: min scaled Jacobian {q.min_scaled_jacobian:.3g} "
                              f"(≥{options['min_scaled_jacobian']}), max aspect {q.max_aspect_ratio:.3g} "
                              f"(≤{options['max_aspect_ratio']})", severity="warning",
                              min_scaled_jacobian=q.min_scaled_jacobian, max_aspect_ratio=q.max_aspect_ratio))

    # 영역 → PID
    single = len(raw.region_names) == 1
    aliases = region_aliases or {}
    pid_of_region: list[int] = []
    for name in raw.region_names:
        pid = resolve_pid(spec.region_part_ids or {}, name, single, aliases.get(name, ()))
        if pid is None:
            raise MeshError(f"no part id for region {name!r}", code=E.SLOT_UNASSIGNED)
        pid_of_region.append(pid)
    elem_pid = np.asarray(pid_of_region)[raw.elem_region]

    sym_factor = SYMMETRY_FACTOR[opts.symmetry]
    regions: dict[str, dict[str, float]] = {}
    for ri, name in enumerate(raw.region_names):
        m = raw.elem_region == ri
        regions[name] = {"part_id": pid_of_region[ri], "element_count": int(m.sum()),
                         "volume_m3": float(vol[m].sum() * sym_factor)}

    node_ids = np.arange(nlo, nlo + n_nodes)
    elem_ids = np.arange(elo, elo + n_elems)

    # 세트: 대칭면 + 참조점
    sets: list[kfile.NodeSet] = []
    node_sets: dict[str, int] = {}
    for plane in SYMMETRY_PLANES[opts.symmetry]:
        key = f"sym_{plane}"
        ax = 0 if plane == "x0" else 1
        on = np.nonzero(nodes_m[:, ax] == 0.0)[0]
        sid = int((opts.node_set_ids or {}).get(key, FALLBACK_SYM_SET_IDS[key]))
        sets.append(kfile.NodeSet(sid, key, tuple(int(node_ids[i]) for i in on)))
        node_sets[key] = sid
    reference_nodes: dict[str, int] = {}
    for k, rp in enumerate(spec.reference_points):
        idx = locate_node(nodes_m, rp.locate, opts.symmetry)
        nid = int(node_ids[idx])
        reference_nodes[rp.name] = nid
        sid = REF_SET_ID_START + k
        sets.append(kfile.NodeSet(sid, rp.name, (nid,)))
        node_sets[rp.name] = sid

    pids_sorted = sorted(set(pid_of_region))
    part_set = (int(opts.part_set_id), pids_sorted) if opts.part_set_id else None
    header = kfile.header_lines(generator, version, topo, target_size_used_m, payload_hash)
    text = kfile.specimen_text(header=header, topology=topo, node_ids=node_ids, coords_mm=coords_mm,
                               elem_ids=elem_ids, elem_pids=elem_pid, elem_nodes=node_ids[raw.elems],
                               node_sets=sets, part_set=part_set)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SPECIMEN_FILE
    path.write_bytes(text.encode("ascii"))

    lo, hi = nodes_m.min(axis=0), nodes_m.max(axis=0)
    corner = 8 if topo == "hex8" else 4
    result = MeshResult(
        mesh_path=path, node_count=n_nodes, element_count=n_elems, regions=regions,
        volume_m3=float(vol.sum() * sym_factor), reference_volume_m3=float(reference_volume_m3),
        bbox_m=(tuple(float(v) for v in lo), tuple(float(v) for v in hi)),  # type: ignore[arg-type]
        reference_nodes=reference_nodes, node_sets=node_sets,
        top_z_at_load_m=top_z_at_x0(nodes_m, raw.elems[:, :corner], corner),
        bottom_z_m=float(lo[2]), symmetry=opts.symmetry, quality=q,
        target_size_used_m=float(target_size_used_m), warnings=tuple(warnings),
    )
    write_result_json(out_dir, "specimen", mesh_result_json(result))
    return result
