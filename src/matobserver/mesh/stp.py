"""`stp` 시편 메셔 — gmsh 파이썬 API(OpenCASCADE)로 STEP 을 읽어 tet4/tet10 을 만든다.

* 단위: STEP 선언 단위를 gmsh `Geometry.OCCTargetUnit = "M"`으로 m 환산(recipe.md §4.3 STP 단위).
* 다중 바디: 3D 솔리드마다 region `body:{i}`(STEP 안 순서, 0부터). 서로 닿는 면은 `occ.fragment`로 적합(절점 공유) 메시.
  STEP 바디 이름이 있으면 `region_part_ids["body:{이름}"]`도 별칭으로 받는다. 바디끼리 겹치면 `STP_CHECK_FAILED`.
* 배치: `placement.mode=auto`(기본) — 메시 절점 바운딩 박스 기준 x·y 중심 → 0, 최저 z → 0. 회전 없음.
  `explicit` — `rotate_deg`(x→y→z 고정축) 후 `translate`; 최저 z 가 0(허용 `placement_z_tol_m`)이 아니면 `STP_CHECK_FAILED`.
* 대칭: 1차 STP 는 항상 none 으로 메시(엔진이 none 을 넘김). 다른 값이 오면 none 으로 메시하고 경고 `SYMMETRY_IGNORED_STP`.
* 결정성: `General.NumThreads = gmsh_threads(1)`, 고정 `Mesh.RandomSeed`, 알고리즘 고정.
* 얇은 부위 검사(`validate`): 바디별 특성 두께 2V/A < `stp_min_thickness_m` 또는 곡선 길이 < `stp_min_edge_m` → `STP_CHECK_FAILED`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Iterator, Literal, Mapping

import numpy as np

from ..core import errors as E
from ..core.types import GeometrySpec, Issue, JigSpec, MeshOptions, MeshResult
from . import common, elements, smooth
from .errors import MeshError

VERSION = "1.0.0"
_LOCK = threading.RLock()  # gmsh 는 프로세스 전역 상태 → 직렬화

STP_DEFAULTS: dict[str, Any] = {
    "stp_min_thickness_m": 1.0e-5,
    "stp_min_edge_m": 1.0e-6,
    "placement_z_tol_m": 1.0e-9,
    "curvature_elements": 16,  # Mesh.MeshSizeFromCurvature (2π 당 요소 수, 0 = 끔)
    "random_seed": 1,
    "algorithm_3d": 1,  # 1 = Delaunay
    "optimize_netgen": 1,
    "smooth_target_sj": 0.3,  # 내부 절점 재배치 목표 scaled Jacobian (smooth.py)
    "smooth_passes": 8,
}


def _gmsh():
    try:
        import gmsh  # noqa: PLC0415
    except Exception as e:  # pragma: no cover - 환경 문제
        raise MeshError(f"gmsh python API unavailable: {e}") from e
    return gmsh


@contextmanager
def gmsh_session(threads: int) -> Iterator[Any]:
    gmsh = _gmsh()
    with _LOCK:
        own = not gmsh.isInitialized()
        if own:
            gmsh.initialize([], readConfigFiles=False, run=False, interruptible=False)
        model_name = f"matobserver_stp_{threading.get_ident()}"
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("General.Verbosity", 1)
            gmsh.option.setNumber("General.NumThreads", int(threads))
            for k in ("Mesh.MaxNumThreads1D", "Mesh.MaxNumThreads2D", "Mesh.MaxNumThreads3D"):
                gmsh.option.setNumber(k, int(threads))
            gmsh.model.add(model_name)
            gmsh.logger.start()
            yield gmsh
        finally:
            try:
                gmsh.logger.stop()
            except Exception:
                pass
            try:
                gmsh.model.remove()
            except Exception:
                pass
            if own:
                gmsh.finalize()


@dataclass
class _Geom:
    bodies: list[int]  # body i → gmsh volume tag (fragment 후)
    names: list[str]
    volumes_m3: list[float]
    areas_m2: list[float]
    min_curve_m: float
    overlap: bool


def _file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rotation(spec: GeometrySpec) -> tuple[list[float], list[float], str]:
    pl = dict(spec.placement or {})
    mode = pl.get("mode", "auto")
    rot = [float(v) for v in pl.get("rotate_deg", [0.0, 0.0, 0.0])] if mode == "explicit" else [0.0, 0.0, 0.0]
    tr = [float(v) for v in pl.get("translate", [0.0, 0.0, 0.0])] if mode == "explicit" else [0.0, 0.0, 0.0]
    return rot, tr, mode


def _load(gmsh: Any, spec: GeometrySpec) -> _Geom:
    """STEP 읽기 + 회전 + fragment. 실패는 MeshError(STP_UNREADABLE)."""
    path = Path(spec.stp_path)  # type: ignore[arg-type]
    gmsh.option.setString("Geometry.OCCTargetUnit", "M")
    gmsh.option.setNumber("Geometry.OCCImportLabels", 1)
    try:
        dimtags = gmsh.model.occ.importShapes(str(path), highestDimOnly=False)
        gmsh.model.occ.synchronize()
    except Exception as e:
        raise MeshError(f"cannot read STEP file: {e}", code=E.STP_UNREADABLE) from None
    vols = [t for d, t in gmsh.model.getEntities(3)]
    if not dimtags or not vols:
        raise MeshError("STEP file contains no solid body", code=E.STP_UNREADABLE)
    names = [gmsh.model.getEntityName(3, v) or "" for v in vols]
    rot, _tr, _mode = _rotation(spec)
    for ax, ang in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), rot):
        if ang:
            gmsh.model.occ.rotate([(3, v) for v in vols], 0, 0, 0, *ax, math.radians(ang))
    gmsh.model.occ.synchronize()
    volumes = [gmsh.model.occ.getMass(3, v) for v in vols]
    overlap = False
    if len(vols) > 1:
        _out, omap = gmsh.model.occ.fragment([(3, v) for v in vols], [])
        gmsh.model.occ.synchronize()
        owners: dict[int, int] = {}
        new_bodies: list[int] = []
        for i, pieces in enumerate(omap):
            tags = [t for d, t in pieces if d == 3]
            for t in tags:
                if t in owners:
                    overlap = True
                owners[t] = i
            if len(tags) != 1:
                overlap = overlap or len(tags) > 1
            new_bodies.append(tags[0] if tags else -1)
        vols = new_bodies
    areas: list[float] = []
    for v in vols:
        faces = gmsh.model.getBoundary([(3, v)], oriented=False, combined=False) if v > 0 else []
        areas.append(sum(gmsh.model.occ.getMass(2, abs(t)) for _d, t in faces))
    curves = gmsh.model.getEntities(1)
    min_curve = min((gmsh.model.occ.getMass(1, t) for _d, t in curves), default=math.inf)
    return _Geom(bodies=vols, names=names, volumes_m3=volumes, areas_m2=areas, min_curve_m=min_curve,
                 overlap=overlap)


class StpMesher:
    name: ClassVar[str] = "stp"
    version: ClassVar[str] = VERSION
    role: ClassVar[Literal["specimen", "jig"]] = "specimen"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = common.merged_options(options, STP_DEFAULTS)

    # ------------------------------------------------------------------ validate
    def validate(self, spec: GeometrySpec | JigSpec) -> list[Issue]:
        if not isinstance(spec, GeometrySpec):
            return [common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, "stp needs a GeometrySpec", "geometry")]
        out: list[Issue] = []
        if spec.kind != "stp":
            out.append(common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, f"kind {spec.kind!r} != 'stp'", "geometry.kind"))
        out += common.check_reference_points(spec.reference_points)
        pl = dict(spec.placement or {})
        if pl.get("mode", "auto") not in ("auto", "explicit"):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "placement.mode must be auto|explicit",
                                    "geometry.placement.mode"))
        for key in ("translate", "rotate_deg"):
            v = pl.get(key)
            if v is not None and (not isinstance(v, (list, tuple)) or len(v) != 3
                                  or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v)):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"placement.{key} must be 3 numbers",
                                        f"geometry.placement.{key}"))
        if spec.stp_path is None:
            out.append(common.issue(E.FILE_NOT_FOUND, "stp_path missing", "geometry.file_id"))
            return out
        p = Path(spec.stp_path)
        if not p.is_file():
            out.append(common.issue(E.FILE_NOT_FOUND, f"STEP file not found: {p.name}", "geometry.file_id"))
            return out
        try:
            head = p.read_bytes()[:64].lstrip()
        except OSError as e:
            out.append(common.issue(E.STP_UNREADABLE, f"cannot read file: {e}", "geometry.file_id"))
            return out
        if not head.startswith(b"ISO-10303-21"):
            out.append(common.issue(E.STP_UNREADABLE, "not a STEP (ISO-10303-21) file", "geometry.file_id"))
            return out
        if out:
            return out
        try:
            with gmsh_session(int(self.options["gmsh_threads"])) as gmsh:
                g = _load(gmsh, spec)
                zmin = None
                if pl.get("mode") == "explicit":
                    bb = [gmsh.model.occ.getBoundingBox(3, v) for v in g.bodies if v > 0]
                    pts = [gmsh.model.getValue(0, t, []) for _d, t in gmsh.model.getEntities(0)]
                    zmin = min(p_[2] for p_ in pts) if pts else min(b[2] for b in bb)
        except MeshError as e:
            return out + [common.issue(e.code or E.STP_UNREADABLE, str(e), "geometry.file_id")]
        except Exception as e:  # gmsh 내부 오류
            return out + [common.issue(E.STP_UNREADABLE, f"STEP import failed: {e}", "geometry.file_id")]
        out += self._geom_issues(spec, g)
        if zmin is not None:
            tz = float(pl.get("translate", [0, 0, 0])[2])
            if abs(zmin + tz) > max(1e-6, float(self.options["placement_z_tol_m"])):
                out.append(common.issue(E.STP_CHECK_FAILED, f"explicit placement: lowest z = {zmin + tz:.6g} m, must be 0",
                                        "geometry.placement", zmin_m=zmin + tz))
        return out

    def _geom_issues(self, spec: GeometrySpec, g: _Geom) -> list[Issue]:
        out: list[Issue] = []
        if g.overlap:
            out.append(common.issue(E.STP_CHECK_FAILED, "solid bodies overlap", "geometry.file_id"))
        tmin = float(self.options["stp_min_thickness_m"])
        for i, (v, a) in enumerate(zip(g.volumes_m3, g.areas_m2)):
            t = 2.0 * v / a if a > 0 else 0.0
            if v <= 0 or t < tmin:
                out.append(common.issue(E.STP_CHECK_FAILED,
                                        f"body {i}: characteristic thickness 2V/A = {t:.3g} m < {tmin:.3g} m (thin region)",
                                        "geometry.file_id", body=i, thickness_m=t))
        if g.min_curve_m < float(self.options["stp_min_edge_m"]):
            out.append(common.issue(E.STP_CHECK_FAILED,
                                    f"geometry has an edge of {g.min_curve_m:.3g} m (< {self.options['stp_min_edge_m']} m)",
                                    "geometry.file_id", min_edge_m=g.min_curve_m))
        rpi = spec.region_part_ids or {}
        single = len(g.bodies) == 1
        for i, nm in enumerate(g.names):
            if common.resolve_pid(rpi, f"body:{i}", single, (f"body:{nm}",) if nm else ()) is None:
                out.append(common.issue(E.STP_BODY_SLOT_MISMATCH, f"body {i} ({nm or 'unnamed'}) has no part id",
                                        "geometry.body_slots", body=i, n_bodies=len(g.bodies)))
        known = {f"body:{i}" for i in range(len(g.bodies))} | {f"body:{n}" for n in g.names if n} | {"*", "specimen"}
        for k in rpi:
            if k.startswith("body:") and k not in known:
                out.append(common.issue(E.STP_BODY_SLOT_MISMATCH, f"{k!r} does not exist (STEP has {len(g.bodies)} bodies)",
                                        "geometry.body_slots", n_bodies=len(g.bodies)))
        return out

    # ------------------------------------------------------------------ mesh
    def mesh(self, spec: GeometrySpec | JigSpec, opts: MeshOptions | None, out_dir: Path) -> MeshResult:
        if not isinstance(spec, GeometrySpec):
            raise MeshError("stp needs a GeometrySpec")
        common.check_options(opts, ("tet4", "tet10"))
        assert opts is not None
        errs = [i for i in self.validate(spec) if i.severity == "error"]
        if errs:
            raise MeshError("; ".join(f"{i.code}: {i.message}" for i in errs), code=errs[0].code)
        warnings: list[Issue] = []
        if opts.symmetry != "none":
            warnings.append(common.issue(E.SYMMETRY_IGNORED_STP, f"symmetry {opts.symmetry!r} ignored for STP (meshed as none)",
                                         severity="warning"))
            opts = dataclasses.replace(opts, symmetry="none")
        h0 = float(opts.target_size_m) * float(opts.size_factor)
        stp_sha = _file_sha256(Path(spec.stp_path))  # type: ignore[arg-type]

        h = h0  # 요청 크기(배율 포함)
        h_eff = None
        curv = int(self.options["curvature_elements"])
        for attempt in range(16):
            with gmsh_session(int(self.options["gmsh_threads"])) as gmsh:
                g = _load(gmsh, spec)
                if h_eff is None:
                    # 두께 방향 최소 요소 수: 전체 z 높이 / min_elems 로 크기 상한(조대화 중에는 상한보다 요소 수 우선)
                    bb = [gmsh.model.occ.getBoundingBox(3, v) for v in g.bodies]
                    height = max(b[5] for b in bb) - min(b[2] for b in bb)
                    h_eff = min(h, height / max(1, int(opts.min_elems_through_thickness)))
                raw_nodes, raw_elems, raw_region = self._generate(gmsh, g, opts.topology, h_eff, curv)
            n = len(raw_elems)
            if n <= opts.max_elements:
                break
            h_eff *= max(1.05, (n / opts.max_elements) ** (1 / 3) * 1.02)
            h = max(h, h_eff)
            if attempt >= 3:
                curv = 0  # 곡률 기반 세분도 해제
        else:
            raise MeshError(f"cannot reach max_elements={opts.max_elements} for STP mesh")
        if attempt > 0:
            warnings.append(common.issue(E.MESH_COARSENED,
                                         f"element count limited to {opts.max_elements}: target size {h0:.4g} m → {h_eff:.4g} m",
                                         severity="warning", requested_size_m=h0, used_size_m=h_eff, element_count=n,
                                         max_elements=int(opts.max_elements)))

        # 배치
        rot, tr, mode = _rotation(spec)
        nodes = raw_nodes.copy()
        if mode == "auto":
            lo, hi = nodes.min(axis=0), nodes.max(axis=0)
            nodes[:, 0] -= 0.5 * (lo[0] + hi[0])
            nodes[:, 1] -= 0.5 * (lo[1] + hi[1])
            nodes[:, 2] -= lo[2]
        else:
            nodes += np.asarray(tr)
            zmin = nodes[:, 2].min()
            if abs(zmin) > max(1e-6, float(self.options["placement_z_tol_m"])):
                raise MeshError(f"explicit placement: lowest z = {zmin:.6g} m, must be 0", code=E.STP_CHECK_FAILED)
            nodes[:, 2] -= zmin
        nodes[nodes[:, 2] == nodes[:, 2].min(), 2] = 0.0

        region_names = [f"body:{i}" for i in range(len(g.bodies))]
        aliases = {f"body:{i}": ((f"body:{nm}",) if nm else ()) for i, nm in enumerate(g.names)}
        raw = common.RawMesh(nodes=nodes, elems=raw_elems, elem_region=raw_region, region_names=region_names,
                             elems_through_thickness=None)
        payload = common.spec_payload(spec, opts, stp_sha256=stp_sha)
        payload["generator"] = {"name": self.name, "version": self.version, "gmsh": _gmsh().__version__,
                                "options": {k: self.options[k] for k in sorted(self.options)}}
        result = common.finalize_specimen(
            raw=raw, spec=spec, opts=opts, out_dir=Path(out_dir), generator=self.name, version=self.version,
            payload_hash=common.input_hash(payload), reference_volume_m3=float(sum(g.volumes_m3)),
            target_size_used_m=h_eff, warnings=warnings, options=self.options, region_aliases=aliases)
        return result

    def _smooth(self, gmsh: Any, g: _Geom) -> None:
        if int(self.options["smooth_passes"]) > 0:
            _smooth_impl(gmsh, g, float(self.options["smooth_target_sj"]), int(self.options["smooth_passes"]))

    def _generate(self, gmsh: Any, g: _Geom, topology: str, h: float,
                  curvature: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        o = self.options
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", int(curvature))
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.option.setNumber("Mesh.Algorithm3D", int(o["algorithm_3d"]))
        gmsh.option.setNumber("Mesh.RandomSeed", int(o["random_seed"]))
        gmsh.option.setNumber("Mesh.Optimize", 1)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", int(o["optimize_netgen"]))
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        gmsh.option.setNumber("Mesh.SecondOrderLinear", 0)
        try:
            gmsh.model.mesh.generate(3)
            self._smooth(gmsh, g)
            if topology == "tet10":
                gmsh.option.setNumber("Mesh.HighOrderOptimize", 0)
                gmsh.model.mesh.setOrder(2)
        except Exception as e:
            raise MeshError(f"gmsh meshing failed: {e}") from None
        etype = 11 if topology == "tet10" else 4
        k = 10 if topology == "tet10" else 4
        perm = _gmsh_to_lsdyna(gmsh, etype)
        conn_list, region_list = [], []
        for i, v in enumerate(g.bodies):
            types, _tags, ntags = gmsh.model.mesh.getElements(3, v)
            for t, nt in zip(types, ntags):
                if t != etype:
                    raise MeshError(f"unexpected gmsh element type {t} in body {i}")
                c = np.asarray(nt, dtype=np.int64).reshape(-1, k)[:, perm]
                conn_list.append(c)
                region_list.append(np.full(len(c), i, dtype=int))
        if not conn_list:
            raise MeshError("gmsh produced no volume elements")
        conn = np.vstack(conn_list)
        region = np.concatenate(region_list)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        tags = np.asarray(tags, dtype=np.int64)
        coords = np.asarray(coords, dtype=float).reshape(-1, 3)
        used = np.unique(conn)
        lookup = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        lookup[tags] = np.arange(len(tags))
        sel = lookup[used]
        nodes = coords[sel]
        renum = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        renum[used] = np.arange(len(used))
        elems = renum[conn]
        # 방향 보정(음의 체적이면 N2↔N3)
        X = nodes[elems[:, :4]]
        neg = elements.tet4_volume(X) < 0
        if neg.any():
            swap = [0, 2, 1, 3] + ([6, 5, 4, 7, 9, 8] if k == 10 else [])
            elems[neg] = elems[neg][:, swap]
        return nodes, elems, region


def _smooth_impl(gmsh: Any, g: _Geom, target: float, passes: int) -> None:
    tags, coords, _ = gmsh.model.mesh.getNodes()
    tags = np.asarray(tags, dtype=np.int64)
    coords = np.asarray(coords, dtype=float).reshape(-1, 3)
    lookup = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
    lookup[tags] = np.arange(len(tags))
    conn = []
    free = np.zeros(len(tags), dtype=bool)
    for v in g.bodies:
        types, _t, nt = gmsh.model.mesh.getElements(3, v)
        for t, ntags in zip(types, nt):
            if t == 4:
                conn.append(lookup[np.asarray(ntags, dtype=np.int64)].reshape(-1, 4))
        inner, _c, _p = gmsh.model.mesh.getNodes(3, v, includeBoundary=False)
        if len(inner):
            free[lookup[np.asarray(inner, dtype=np.int64)]] = True
    if not conn:
        return
    tets = np.vstack(conn)
    neg = elements.tet4_volume(coords[tets]) < 0
    tets[neg] = tets[neg][:, [0, 2, 1, 3]]
    X2, moved = smooth.improve(coords, tets, free, target=target, passes=passes)
    for i in moved:
        gmsh.model.mesh.setNode(int(tags[i]), [float(c) for c in X2[i]], [])


def _gmsh_to_lsdyna(gmsh: Any, etype: int) -> list[int]:
    """gmsh 국소 절점 순서 → LS-DYNA 순서(N1..N4 꼭짓점, N5=1-2, N6=2-3, N7=3-1, N8=1-4, N9=2-4, N10=3-4)."""
    _name, _dim, _order, nn, local, _np = gmsh.model.mesh.getElementProperties(etype)
    local = np.asarray(local, dtype=float).reshape(nn, 3)
    want = elements.TET10_NODE_NATURAL[:nn]
    perm = []
    for w in want:
        d = np.linalg.norm(local - w, axis=1)
        j = int(np.argmin(d))
        if d[j] > 1e-9:
            raise MeshError("unexpected gmsh reference element layout")
        perm.append(j)
    return perm
