"""`box`(육면체 시편)·`layered_box`(모드 B 층상) 정렬 격자 메셔.

배치(recipe.md §4.3): x = 길이(스팬), y = 폭, z = 하중축, 원점 = 스팬 중앙·폭 중앙·아랫면 z=0.
대칭: quarter = x≥0, y≥0 / half = y≥0 / none = 전체(x=0, y=0 위에 절점 줄을 둔다).

layered_box 의 `stack_axis`:
* `z`(기본): 층을 아래→위(z)로 쌓는다. z 범위 = Σ층 두께(=`thickness`), y 범위 = `width`.
* `y`(측면 하중): 같은 적층판을 옆으로 세운 배치. y 범위 = Σ층 두께(층 0이 y 최소 쪽), z 범위 = `width`.
  y=0 대칭면이 적층을 가르므로 half/quarter 는 거절(`MeshError`).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping

import numpy as np

from ..core import errors as E
from ..core.types import GeometrySpec, Issue, JigSpec, MeshOptions, MeshResult
from . import common, structured
from .errors import MeshError

VERSION = "1.0.0"


def _layer_plan(breaks: list[float], h: float, minimum_total: int) -> structured.AxisPlan:
    counts = [max(1, int(math.ceil((breaks[i + 1] - breaks[i]) / h - 1e-9))) for i in range(len(breaks) - 1)]
    while sum(counts) < minimum_total:  # 가장 굵은 셀을 가진 층부터 1개씩 추가(동률은 낮은 인덱스)
        sizes = [(breaks[i + 1] - breaks[i]) / counts[i] for i in range(len(counts))]
        counts[int(np.argmax(sizes))] += 1
    return structured.AxisPlan(tuple(breaks), tuple(counts))


def _span_plan(length: float, h: float, symmetric_half: bool, minimum: int = 1) -> structured.AxisPlan:
    """symmetric_half=True → [0, L/2] 한 구간, False → [-L/2, 0], [0, L/2] 두 구간(0에 절점)."""
    half = 0.5 * length
    n = structured.n_cells(half, h, minimum=minimum)
    if symmetric_half:
        return structured.AxisPlan((0.0, half), (n,))
    return structured.AxisPlan((-half, 0.0, half), (n, n))


class _BoxBase:
    name: ClassVar[str]
    version: ClassVar[str] = VERSION
    role: ClassVar[Literal["specimen", "jig"]] = "specimen"
    _layered: ClassVar[bool] = False

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = common.merged_options(options, {"layer_sum_rel_tol": 1e-3})

    # ------------------------------------------------------------------ validate
    def validate(self, spec: GeometrySpec | JigSpec) -> list[Issue]:
        if not isinstance(spec, GeometrySpec):
            return [common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, f"{self.name} needs a GeometrySpec", "geometry")]
        out: list[Issue] = []
        if spec.kind != self.name:
            out.append(common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, f"kind {spec.kind!r} != {self.name!r}",
                                    "geometry.kind"))
        out += self._validate_shape(spec)
        out += common.check_reference_points(spec.reference_points)
        rpi = spec.region_part_ids or {}
        for k, v in rpi.items():
            if not common.check_pid_value(v):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"part id for {k!r} must be a positive int",
                                        f"geometry.region_part_ids.{k}", value=v))
        regions = self._region_names(spec)
        for r in regions:
            if common.resolve_pid(rpi, r, len(regions) == 1) is None:
                out.append(common.issue(E.SLOT_UNASSIGNED, f"region {r!r} has no part id", "geometry.region_part_ids",
                                        region=r))
        return out

    def _region_names(self, spec: GeometrySpec) -> list[str]:
        return ["specimen"]

    def _validate_shape(self, spec: GeometrySpec) -> list[Issue]:
        out = common.check_dimensions(spec, ("width", "thickness", "total_length"))
        if spec.layers:
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "box takes no layers (use layered_box)",
                                    "geometry.layers"))
        return out

    # ------------------------------------------------------------------ geometry
    def _extents(self, spec: GeometrySpec) -> tuple[float, float, float, list[float], str]:
        """(L, W_y, H_z, layer breaks along stack axis, stack axis)."""
        d = spec.dimensions
        return float(d["total_length"]), float(d["width"]), float(d["thickness"]), [0.0, float(d["thickness"])], "z"

    def _plans(self, spec: GeometrySpec, opts: MeshOptions, h: float, cap: float):
        L, W, H, breaks, axis = self._extents(spec)
        sym = opts.symmetry
        mins = int(opts.min_elems_through_thickness)
        if axis == "z":
            zp = _layer_plan(breaks, h, mins)
            hp = min(h, cap * zp.min_cell())
            xp = _span_plan(L, hp, sym == "quarter")
            yp = _span_plan(W, hp, sym in ("half", "quarter"))
        else:
            yb = [b - 0.5 * breaks[-1] for b in breaks]
            yp = _layer_plan(yb, h, 1)
            hz = min(h, cap * yp.min_cell())
            zp = structured.AxisPlan((0.0, H), (structured.n_cells(H, hz, minimum=mins),))
            hp = min(h, cap * min(yp.min_cell(), zp.min_cell()))
            xp = _span_plan(L, hp, sym == "quarter")
        return xp, yp, zp

    def mesh(self, spec: GeometrySpec | JigSpec, opts: MeshOptions | None, out_dir: Path) -> MeshResult:
        if not isinstance(spec, GeometrySpec):
            raise MeshError(f"{self.name} needs a GeometrySpec")
        common.check_options(opts, common.SOLID_TOPOLOGIES)
        assert opts is not None
        errs = [i for i in self.validate(spec) if i.severity == "error"]
        if errs:
            raise MeshError("; ".join(f"{i.code}: {i.message}" for i in errs), code=errs[0].code)
        L, W, H, breaks, axis = self._extents(spec)
        if axis == "y" and opts.symmetry != "none":
            raise MeshError("stack_axis='y' puts the layup across the y=0 symmetry plane; use symmetry 'none'",
                            code=E.GEOMETRY_DIMENSION_INVALID)

        h0 = float(opts.target_size_m) * float(opts.size_factor)
        epc = structured.elements_per_cell(opts.topology)
        cap_on = structured.aspect_cap(opts.topology, float(self.options["max_aspect_ratio"]))
        step = float(self.options["coarsen_step"])
        h, cap, relaxed = h0, cap_on, False
        prev, stable = None, 0
        for _ in range(int(self.options["coarsen_max_iter"])):
            xp, yp, zp = self._plans(spec, opts, h, cap)
            count = xp.n * yp.n * zp.n * epc
            if count <= opts.max_elements:
                break
            stable = stable + 1 if count == prev else 0
            prev = count
            if stable >= 3 and not relaxed:  # 면내 aspect 상한 때문에 더 줄지 않음 → 상한 해제
                cap, relaxed, stable = math.inf, True, 0
                continue
            h *= step
        else:
            raise MeshError(f"cannot reach max_elements={opts.max_elements} (minimum layout has {count} elements)")

        warnings: list[Issue] = []
        if h > h0 or relaxed:
            warnings.append(common.issue(
                E.MESH_COARSENED, f"element count limited to {opts.max_elements}: target size {h0:.4g} m → {h:.4g} m"
                + (" (in-plane aspect cap relaxed)" if relaxed else ""), severity="warning",
                requested_size_m=h0, used_size_m=h, element_count=count, max_elements=int(opts.max_elements),
                aspect_cap_relaxed=relaxed))

        xs, ys, zs = xp.points(), yp.points(), zp.points()
        nodes, elems, cells = structured.build(opts.topology, xs, ys, zs)
        if axis == "z":
            seg = zp.cell_segment()[cells[:, 2]]
        else:
            seg = yp.cell_segment()[cells[:, 1]]
        region_names = self._region_names(spec)
        raw = common.RawMesh(nodes=nodes, elems=elems, elem_region=seg if len(region_names) > 1 else np.zeros(
            len(elems), dtype=int), region_names=region_names, elems_through_thickness=zp.n)
        payload = common.spec_payload(spec, opts)
        payload["generator"] = {"name": self.name, "version": self.version,
                                "options": {k: self.options[k] for k in sorted(self.options)}}
        ref_vol = L * W * H if axis == "z" else L * breaks[-1] * H
        return common.finalize_specimen(
            raw=raw, spec=spec, opts=opts, out_dir=Path(out_dir), generator=self.name, version=self.version,
            payload_hash=common.input_hash(payload), reference_volume_m3=ref_vol, target_size_used_m=h,
            warnings=warnings, options=self.options)


class BoxMesher(_BoxBase):
    """육면체 시편 (hex8 정렬 격자, tet4/tet10 은 Kuhn 6분할). region = "specimen"."""

    name: ClassVar[str] = "box"


class LayeredBoxMesher(_BoxBase):
    """층상 육면체(모드 B). region = spec.layers 의 이름("layer:{i}"), 계면 절점 공유(연속 메시)."""

    name: ClassVar[str] = "layered_box"
    _layered: ClassVar[bool] = True

    def _region_names(self, spec: GeometrySpec) -> list[str]:
        return [str(n) for n, _t in spec.layers] or ["specimen"]

    def _validate_shape(self, spec: GeometrySpec) -> list[Issue]:
        out = common.check_dimensions(spec, ("width", "total_length"))
        if spec.stack_axis not in ("z", "y"):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "stack_axis must be 'z' or 'y'",
                                    "geometry.stack_axis", value=spec.stack_axis))
        if not spec.layers:
            out.append(common.issue(E.GEOMETRY_DIMENSION_MISSING, "layered_box needs ≥1 layer", "geometry.layers"))
            return out
        names = [n for n, _ in spec.layers]
        if len(set(names)) != len(names):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "layer names must be unique", "geometry.layers"))
        total = 0.0
        for i, (n, t) in enumerate(spec.layers):
            if not isinstance(n, str) or not n:
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "layer name must be a string",
                                        f"geometry.layers[{i}]"))
            if not common.is_pos_finite(t):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "layer thickness must be positive [m]",
                                        f"geometry.layers[{i}].thickness", value=t))
            else:
                total += float(t)
        th = (spec.dimensions or {}).get("thickness")
        if th is not None and common.is_pos_finite(th) and total > 0:
            if abs(total - float(th)) > float(self.options["layer_sum_rel_tol"]) * float(th):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID,
                                        f"sum of layer thicknesses {total:.6g} m != thickness {float(th):.6g} m",
                                        "geometry.layers", total=total, thickness=float(th)))
        return out

    def _extents(self, spec: GeometrySpec):
        d = spec.dimensions
        breaks = [0.0]
        for _n, t in spec.layers:
            breaks.append(breaks[-1] + float(t))
        L, W = float(d["total_length"]), float(d["width"])
        if spec.stack_axis == "y":
            return L, breaks[-1], W, breaks, "y"
        return L, W, breaks[-1], breaks, "z"
