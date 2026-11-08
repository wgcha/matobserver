"""`jig.bending3p_rollers` — 3점굽힘 강체 압자·지지 롤러(셸 원통, 축 y) 생성기 (role = jig).

배치(recipe.md §4.3, module-boundaries.md §2):
* 지지 롤러: 중심 x = ±span/2, 윗면 접선 z = 0. 압자: 중심 x = 0, 아랫면 접선 z = `specimen.top_z_at_load_m`.
* 셸 두께 t(`shell_thickness_m`)는 LS-DYNA 접촉에서 중면 ±t/2 로 쓰이므로 `contact_offset=true`(기본)이면
  중면 반지름은 R 그대로 두고 중심을 t/2 만큼 바깥(시편 반대쪽)으로 옮겨 **접촉면(셸 바깥면) 기준 초기 간격 0**을 맞춘다.
  `contact_offset=false`면 중면이 접선(z=top, z=0)에 놓인다.
* 대칭: quarter = x≥0·y≥0 (압자 반원, 지지 롤러 x=+span/2 하나), half = y≥0, none = 전체.
* 롤러 길이: `roller_length`(params_si) 또는 auto = 시편 폭 + 2·target_size (recipe.md §4.5).
* 참조 노드(JigResult.reference_nodes): `indenter_ref` = 압자 최저 접선 위 노드(x=0, y=대칭면/중앙),
  `support_top` = +span/2 지지 롤러 최고 접선 위 노드. 둘 다 강체 파트 소속.
* *MAT_RIGID: JigSpec.rigid {E, poisson, density}(SI), 없으면 옵션 `rigid_E_pa`·`rigid_poisson`·`rigid_density_kg_m3`
  (= 설정 mesh.jig.* 기본값). SECTION_SHELL ID = part_id.
* 셸 법선은 원통 바깥(시편 쪽)을 향한다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping

import numpy as np

from ..core import errors as E
from ..core.types import GeometrySpec, Issue, JigResult, JigSpec, MeshOptions
from . import common, kfile
from .errors import MeshError

VERSION = "1.0.0"
REQUIRED_REFERENCE_NODES = ("indenter_ref", "support_top")

JIG_DEFAULTS: dict[str, Any] = {
    "rigid_E_pa": 2.1e11,
    "rigid_poisson": 0.3,
    "rigid_density_kg_m3": 7850.0,
    "shell_thickness_m": 1.0e-4,
    "contact_offset": True,
    "min_segments": 24,  # 원 둘레 최소 분할(4의 배수로 올림)
    "segment_size_m": None,  # None → 시편 target_size_used_m
}
#: params_si 키 별칭 (recipe.md §4.5 이름이 정본, types.py 주석 이름도 받음)
PARAM_ALIASES = {
    "span": ("span",),
    "indenter_radius": ("indenter_radius", "indenter_r", "r_ind"),
    "support_radius": ("support_radius", "support_r", "r_sup"),
    "roller_length": ("roller_length", "jig_len"),
}


def _param(params: Mapping[str, Any], key: str) -> Any:
    for k in PARAM_ALIASES[key]:
        if k in params:
            return params[k]
    return None


class Bending3pRollersJig:
    name: ClassVar[str] = "jig.bending3p_rollers"
    version: ClassVar[str] = VERSION
    role: ClassVar[Literal["specimen", "jig"]] = "jig"
    required_reference_nodes: ClassVar[tuple[str, ...]] = REQUIRED_REFERENCE_NODES

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = dict(JIG_DEFAULTS)
        if options:
            self.options.update(options)

    # ------------------------------------------------------------------ validate
    def validate(self, spec: GeometrySpec | JigSpec) -> list[Issue]:
        if not isinstance(spec, JigSpec):
            return [common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, "jig generator needs a JigSpec", "jig")]
        out: list[Issue] = []
        if spec.generator != self.name:
            out.append(common.issue(E.GEOMETRY_KIND_NOT_ALLOWED, f"generator {spec.generator!r} != {self.name!r}",
                                    "jig.generator"))
        p = spec.params_si or {}
        vals: dict[str, float] = {}
        for key in ("span", "indenter_radius", "support_radius"):
            v = _param(p, key)
            if v is None:
                out.append(common.issue(E.GEOMETRY_DIMENSION_MISSING, f"jig parameter {key!r} missing",
                                        f"jig.params.{key}"))
            elif not common.is_pos_finite(v):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"jig parameter {key!r} must be > 0 [m]",
                                        f"jig.params.{key}", value=v))
            else:
                vals[key] = float(v)
        rl = _param(p, "roller_length")
        if rl is not None and rl != "auto" and not common.is_pos_finite(rl):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "roller_length must be > 0 [m] or auto",
                                    "jig.params.roller_length", value=rl))
        for role in ("indenter", "support"):
            if not common.check_pid_value((spec.part_ids or {}).get(role)):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"jig.part_ids.{role} missing or invalid",
                                        f"jig.part_ids.{role}"))
            if not common.check_pid_value((spec.mids or {}).get(role)):
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"jig.mids.{role} missing or invalid",
                                        f"jig.mids.{role}"))
        r = self._rigid(spec)
        if not (common.is_pos_finite(r["E"]) and common.is_pos_finite(r["density"]) and -1 < r["poisson"] < 0.5):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "jig.rigid E>0, density>0, -1<poisson<0.5",
                                    "jig.rigid", **r))
        if spec.symmetry not in common.SYMMETRY_FACTOR:
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, f"unknown symmetry {spec.symmetry!r}", "symmetry"))
        sp = spec.specimen
        if sp is None:
            out.append(common.issue(E.GEOMETRY_DIMENSION_MISSING, "specimen MeshResult missing", "jig.specimen"))
        elif spec.symmetry in common.SYMMETRY_FACTOR and sp.symmetry != spec.symmetry:
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID,
                                    f"jig symmetry {spec.symmetry!r} != specimen symmetry {sp.symmetry!r}", "symmetry"))
        lo, hi = spec.node_id_range
        elo, ehi = spec.element_id_range
        if not (0 < lo <= hi <= 99_999_999 and 0 < elo <= ehi <= 99_999_999):
            out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "invalid jig id ranges", "jig.id_ranges"))
        if len(vals) == 3:
            half = 0.5 * vals["span"]
            if half - vals["support_radius"] - vals["indenter_radius"] <= 0:
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "rollers overlap: span/2 must exceed R_ind + R_sup",
                                        "jig.params.span"))
            if spec.symmetry == "quarter" and half - vals["support_radius"] <= 0:
                out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID, "support roller crosses x=0 symmetry plane",
                                        "jig.params.span"))
            if sp is not None:
                x_extent = sp.bbox_m[1][0]
                if half >= x_extent:
                    out.append(common.issue(E.GEOMETRY_DIMENSION_INVALID,
                                            f"span/2 = {half:.6g} m ≥ specimen half length {x_extent:.6g} m",
                                            "jig.params.span"))
        return out

    def _rigid(self, spec: JigSpec) -> dict[str, float]:
        r = dict(spec.rigid or {})
        return {"E": float(r.get("E", self.options["rigid_E_pa"])),
                "poisson": float(r.get("poisson", self.options["rigid_poisson"])),
                "density": float(r.get("density", self.options["rigid_density_kg_m3"]))}

    # ------------------------------------------------------------------ mesh
    def mesh(self, spec: GeometrySpec | JigSpec, opts: MeshOptions | None, out_dir: Path) -> JigResult:
        if not isinstance(spec, JigSpec):
            raise MeshError("jig generator needs a JigSpec")
        errs = [i for i in self.validate(spec) if i.severity == "error"]
        if errs:
            raise MeshError("; ".join(f"{i.code}: {i.message}" for i in errs), code=errs[0].code)
        sp = spec.specimen
        p = spec.params_si
        span = float(_param(p, "span"))
        r_ind = float(_param(p, "indenter_radius"))
        r_sup = float(_param(p, "support_radius"))
        sym = spec.symmetry
        h = float(self.options["segment_size_m"] or sp.target_size_used_m)
        t = float(self.options["shell_thickness_m"])
        off = 0.5 * t if self.options["contact_offset"] else 0.0
        top = float(sp.top_z_at_load_m)

        (ylo, yhi) = (sp.bbox_m[0][1], sp.bbox_m[1][1])
        if sym in ("half", "quarter"):
            w_full, yc = 2.0 * yhi, 0.0
        else:
            w_full, yc = yhi - ylo, 0.5 * (ylo + yhi)
        rl = _param(p, "roller_length")
        L_r = float(rl) if (rl is not None and rl != "auto") else w_full + 2.0 * float(sp.target_size_used_m)
        if sym in ("half", "quarter"):
            y0, y1, y_ref = 0.0, 0.5 * L_r, 0.0
            ny = max(1, math.ceil(0.5 * L_r / h - 1e-9))
            ys = np.linspace(y0, y1, ny + 1)
        else:
            nh = max(1, math.ceil(0.5 * L_r / h - 1e-9))
            ys = np.linspace(yc - 0.5 * L_r, yc + 0.5 * L_r, 2 * nh + 1)
            ys[nh] = yc
            y_ref = yc

        def nseg(r: float) -> int:
            n = max(int(self.options["min_segments"]), math.ceil(2 * math.pi * r / h - 1e-9))
            return int(4 * math.ceil(n / 4))

        cyls: list[tuple[str, float, float, float, float, float, bool]] = []
        # (role, xc, zc, r, a0, a1, closed): 점 = (xc + r sin a, zc - r cos a)
        zc_ind = top + r_ind + off
        if sym == "quarter":
            cyls.append(("indenter", 0.0, zc_ind, r_ind, 0.0, math.pi, False))
            supports = [0.5 * span]
        else:
            cyls.append(("indenter", 0.0, zc_ind, r_ind, 0.0, 2 * math.pi, True))
            supports = [0.5 * span, -0.5 * span]
        for xs_ in supports:
            cyls.append(("support", xs_, -(r_sup + off), r_sup, 0.0, 2 * math.pi, True))

        nodes_l: list[np.ndarray] = []
        quads_l: list[np.ndarray] = []
        roles_l: list[str] = []
        ref_idx: dict[str, int] = {}
        base = 0
        for role, xc, zc, r, a0, a1, closed in cyls:
            n_full = nseg(r)
            n_a = n_full if closed else n_full // 2
            na_nodes = n_a if closed else n_a + 1
            ang = a0 + (a1 - a0) * np.arange(na_nodes) / n_a
            # 정확한 접선 노드: a=0(최저), a=π(최고)
            px = xc + r * np.sin(ang)
            pz = zc - r * np.cos(ang)
            px[np.isclose(ang, 0.0)] = xc
            pz[np.isclose(ang, 0.0)] = zc - r
            m = np.isclose(ang, math.pi)
            px[m], pz[m] = xc, zc + r
            ny_n = len(ys)
            A, Y = np.meshgrid(np.arange(na_nodes), np.arange(ny_n), indexing="ij")
            pts = np.stack([px[A.ravel()], ys[Y.ravel()], pz[A.ravel()]], axis=1)

            def nid(a, j):
                return base + (a % na_nodes) * ny_n + j

            AA, JJ = (v.ravel() for v in np.meshgrid(np.arange(n_a), np.arange(ny_n - 1), indexing="ij"))
            # 바깥 법선: (a, j) → (a, j+1) → (a+1, j+1) → (a+1, j)
            q = np.stack([nid(AA, JJ), nid(AA, JJ + 1), nid(AA + 1, JJ + 1), nid(AA + 1, JJ)], axis=1)
            j_ref = int(np.argmin(np.abs(ys - y_ref)))
            if role == "indenter":
                ref_idx["indenter_ref"] = nid(0, j_ref)
            elif "support_top" not in ref_idx and xc > 0:
                ref_idx["support_top"] = nid(n_full // 2, j_ref)
            nodes_l.append(pts)
            quads_l.append(q)
            roles_l += [role] * len(q)
            base += len(pts)

        nodes = np.vstack(nodes_l)
        quads = np.vstack(quads_l)
        n_nodes, n_elems = len(nodes), len(quads)
        nlo, nhi = spec.node_id_range
        elo, ehi = spec.element_id_range
        if nlo + n_nodes - 1 > nhi or elo + n_elems - 1 > ehi:
            raise MeshError("jig mesh exceeds jig id ranges")
        coords_mm = kfile.quantize_mm(nodes)
        node_ids = np.arange(nlo, nlo + n_nodes)
        elem_ids = np.arange(elo, elo + n_elems)
        pid = {k: int(v) for k, v in spec.part_ids.items()}
        elem_pids = np.array([pid[r] for r in roles_l])
        rigid = self._rigid(spec)
        parts = [kfile.RigidPart(title=f"matobserver jig {role}", pid=pid[role], secid=pid[role],
                                 mid=int(spec.mids[role]), thickness_m=t, density_kg_m3=rigid["density"],
                                 E_pa=rigid["E"], poisson=rigid["poisson"]) for role in ("indenter", "support")]
        payload = {
            "generator": self.name, "version": self.version, "options": {k: self.options[k] for k in sorted(self.options)},
            "params_si": {k: (v if isinstance(v, str) else float(v)) for k, v in sorted(p.items())},
            "part_ids": dict(spec.part_ids), "mids": dict(spec.mids), "rigid": rigid, "symmetry": sym,
            "node_id_range": list(spec.node_id_range), "element_id_range": list(spec.element_id_range),
            "specimen": {"top_z_at_load_m": sp.top_z_at_load_m, "bbox_m": [list(sp.bbox_m[0]), list(sp.bbox_m[1])],
                         "target_size_used_m": sp.target_size_used_m, "symmetry": sp.symmetry},
        }
        header = kfile.header_lines(self.name, self.version, "shell4", h, common.input_hash(payload))
        text = kfile.jig_text(header=header, node_ids=node_ids, coords_mm=coords_mm, elem_ids=elem_ids,
                              elem_pids=elem_pids, elem_nodes=node_ids[quads], parts=parts)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / common.JIG_FILE
        path.write_bytes(text.encode("ascii"))
        refs = {k: int(node_ids[v]) for k, v in ref_idx.items()}
        result = JigResult(mesh_path=path, reference_nodes=refs, part_ids={"indenter": pid["indenter"],
                                                                         "support": pid["support"]})
        common.write_result_json(out_dir, "jig", {
            "mesh_file": path.name, "reference_nodes": refs, "part_ids": dict(result.part_ids),
            "node_count": n_nodes, "element_count": n_elems, "roller_length_m": L_r, "shell_thickness_m": t,
            "contact_offset": bool(self.options["contact_offset"]), "rigid": rigid,
        })
        return result
