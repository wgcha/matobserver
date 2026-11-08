"""Impl-Solver 시험 공용 도우미 (tests/{contract,unit}에서 importlib로 읽는다).

* 시편 hex8/tet4 메시·지그 롤러 강체 셸 메시 생성(module-boundaries.md §2 규격) — Impl-Mesh 구현 전 대역.
* 픽스처 레시피(parametric·fixed) → SimulationCase.
* 가짜 배치 스크립트 설정(Config)·동작 지정.
* verify_recipe용 가짜 registry·services.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import yaml

from matobserver.core.config import Config
from matobserver.core.types import (
    ElasticBlock,
    ExtractorRef,
    JigResult,
    MeshQuality,
    MeshResult,
    NeutralMaterial,
    OutputRequest,
    ReferencePointRequest,
    ResponseDef,
    ResponseWindow,
    SimulationCase,
    TableRow,
)
from matobserver.verify.dryrun import output_request, slot_bindings

FIX = Path(__file__).resolve().parent
RECIPES = FIX / "recipes"
FAKE_SCRIPT = FIX / "fake_lsdyna.py"

# 기준 시편(SI): span 64 mm, b 10 mm, h 4 mm, L 80 mm
SPAN, WIDTH, THICK, LENGTH = 0.064, 0.010, 0.004, 0.080


# ======================================================================================
# 메시 (파일 단위 mm)
# ======================================================================================
def _node_line(nid: int, x: float, y: float, z: float) -> str:
    return f"{nid:>8d}{x:>16.9e}{y:>16.9e}{z:>16.9e}{0:>8d}{0:>8d}"


def specimen_text(nx: int = 8, ny: int = 2, nz: int = 2, *, length_mm: float = LENGTH * 1e3,
                  width_mm: float = WIDTH * 1e3, thick_mm: float = THICK * 1e3, pid: int = 1,
                  topology: str = "hex8", sym_ids=(901, 902), part_set_id: int = 900) -> tuple[str, dict]:
    """quarter 시편 x∈[0,L/2], y∈[0,b/2], z∈[0,h]. 반환 (텍스트, 정보)."""
    lx, ly = length_mm / 2.0, width_mm / 2.0
    nid = {}
    lines = ["*KEYWORD", "$ matobserver mesh",
             f"$ generator=solver_fixture version=1 topology={topology} target_size_mm={lx / nx:g} units=mm-N-s-tonne",
             "$ input_sha256=fixture", "*NODE"]
    k = 0
    for kz in range(nz + 1):
        for jy in range(ny + 1):
            for ix in range(nx + 1):
                k += 1
                nid[(ix, jy, kz)] = k
                lines.append(_node_line(k, lx * ix / nx, ly * jy / ny, thick_mm * kz / nz))
    lines.append("*ELEMENT_SOLID")
    e = 0
    for kz in range(nz):
        for jy in range(ny):
            for ix in range(nx):
                c = [nid[(ix, jy, kz)], nid[(ix + 1, jy, kz)], nid[(ix + 1, jy + 1, kz)], nid[(ix, jy + 1, kz)],
                     nid[(ix, jy, kz + 1)], nid[(ix + 1, jy, kz + 1)], nid[(ix + 1, jy + 1, kz + 1)], nid[(ix, jy + 1, kz + 1)]]
                if topology == "hex8":
                    e += 1
                    lines += [f"{e:>8d}{pid:>8d}", "".join(f"{n:>8d}" for n in c)]
                else:  # tet4: 육면체 하나를 5개 사면체로
                    for t in ((0, 1, 3, 4), (1, 2, 3, 6), (1, 4, 5, 6), (3, 4, 6, 7), (1, 3, 4, 6)):
                        e += 1
                        n4 = [c[i] for i in t]
                        lines += [f"{e:>8d}{pid:>8d}", "".join(f"{n:>8d}" for n in n4 + [n4[3]] * 4)]

    def nset(sid, title, members):
        out = ["*SET_NODE_LIST_TITLE", title, f"{sid:>10d}"]
        for i in range(0, len(members), 8):
            out.append("".join(f"{m:>10d}" for m in members[i:i + 8]))
        return out

    x0 = sorted(v for (i, j, kk), v in nid.items() if i == 0)
    y0 = sorted(v for (i, j, kk), v in nid.items() if j == 0)
    lines += nset(sym_ids[0], "sym_x0", x0) + nset(sym_ids[1], "sym_y0", y0)
    lines += ["*SET_PART_LIST_TITLE", "specimen", f"{part_set_id:>10d}", f"{pid:>10d}", "*END"]
    info = {"node_count": k, "element_count": e, "midspan_bottom": nid[(0, 0, 0)],
            "volume_m3": length_mm * width_mm * thick_mm * 1e-9, "target_size_m": lx / nx * 1e-3}
    return "\n".join(lines) + "\n", info


def write_specimen(out_dir: Path, **kw) -> MeshResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    text, info = specimen_text(**kw)
    p = out_dir / "specimen.k"
    p.write_text(text)
    h = kw.get("thick_mm", THICK * 1e3) * 1e-3
    return MeshResult(
        mesh_path=p, node_count=info["node_count"], element_count=info["element_count"],
        regions={"specimen": {"part_id": kw.get("pid", 1), "element_count": info["element_count"], "volume_m3": info["volume_m3"]}},
        volume_m3=info["volume_m3"], reference_volume_m3=info["volume_m3"],
        bbox_m=((0.0, 0.0, 0.0), (LENGTH / 2, WIDTH / 2, h)),
        reference_nodes={"midspan_bottom": info["midspan_bottom"]}, node_sets={"sym_x0": 901, "sym_y0": 902},
        top_z_at_load_m=h, bottom_z_m=0.0, symmetry="quarter",
        quality=MeshQuality(1.0, 1.0, kw.get("nz", 2)), target_size_used_m=info["target_size_m"], warnings=())


def jig_text(*, span_mm: float = SPAN * 1e3, r_ind: float = 5.0, r_sup: float = 5.0, h_top: float = THICK * 1e3,
             roller_half: float = 6.0, n_arc: int = 6, ny: int = 2, node0: int = 5_000_001, elem0: int = 5_000_001,
             pids=(1001, 1002), mids=(1001, 1002), rigid=(2.1e11, 0.3, 7850.0), with_materials: bool = True,
             indenter_z0: float | None = None) -> tuple[str, dict]:
    """quarter 지그: 압자(중심 x=0, z=h_top+R, 1/4 원호) + 지지 롤러(중심 x=span/2, z=−R, 반원호). 강체 셸."""
    lines = ["*KEYWORD", "$ matobserver mesh",
             "$ generator=solver_fixture.jig version=1 topology=shell4 target_size_mm=1 units=mm-N-s-tonne",
             "$ input_sha256=fixture", "*NODE"]
    nid = node0
    eid = elem0
    elems = []
    refs = {}
    z_ind = h_top if indenter_z0 is None else indenter_z0
    for which, (pid, cx, cz, r, th0, th1) in enumerate(
            [(pids[0], 0.0, z_ind + r_ind, r_ind, 0.0, math.pi / 2), (pids[1], span_mm / 2, -r_sup, r_sup, -math.pi / 2, math.pi / 2)]):
        grid = {}
        for i in range(n_arc + 1):
            th = th0 + (th1 - th0) * i / n_arc
            if which == 0:
                x, z = cx + r * math.sin(th), cz - r * math.cos(th)
            else:
                x, z = cx + r * math.sin(th), cz + r * math.cos(th)
            for j in range(ny + 1):
                grid[(i, j)] = nid
                lines.append(_node_line(nid, x, roller_half * j / ny, z))
                nid += 1
        ref_i = 0 if which == 0 else n_arc // 2
        refs["indenter_ref" if which == 0 else "support_top"] = grid[(ref_i, 0)]
        for i in range(n_arc):
            for j in range(ny):
                elems.append((eid, pid, grid[(i, j)], grid[(i + 1, j)], grid[(i + 1, j + 1)], grid[(i, j + 1)]))
                eid += 1
    lines.append("*ELEMENT_SHELL")
    lines += ["".join(f"{v:>8d}" for v in el) for el in elems]
    if with_materials:
        for pid, mid in zip(pids, mids):
            lines += ["*PART", f"jig {pid}", "".join(f"{v:>10d}" for v in (pid, pid, mid))]
            lines += ["*SECTION_SHELL", "".join(f"{v:>10d}" for v in (pid, 2)) + f"{1.0:>10.4f}" + f"{2.0:>10.1f}",
                      "".join(f"{0.5:>10.1f}" for _ in range(4))]
            lines += ["*MAT_RIGID", f"{mid:>10d}{rigid[2] * 1e-12:>10.3E}{rigid[0] * 1e-6:>10.1f}{rigid[1]:>10.3f}",
                      f"{0.0:>10.1f}{0:>10d}{0:>10d}", f"{0.0:>10.1f}"]
    lines.append("*END")
    return "\n".join(lines) + "\n", refs


def write_jig(out_dir: Path, **kw) -> JigResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    text, refs = jig_text(**kw)
    p = out_dir / "jig.k"
    p.write_text(text)
    return JigResult(mesh_path=p, reference_nodes=refs, part_ids={"indenter": 1001, "support": 1002})


# ======================================================================================
# 레시피 → SimulationCase
# ======================================================================================
def load_manifest(recipe: str | Path) -> dict:
    d = RECIPES / recipe if isinstance(recipe, str) else Path(recipe)
    return yaml.safe_load((d / "manifest.yaml").read_text())


def responses_from_manifest(manifest: Mapping[str, Any], axis: str = "displacement") -> tuple[ResponseDef, ...]:
    out = []
    for r in manifest["responses"]:
        ex = r["extractor"] if axis == "displacement" else {**r["extractor"], **r["alt_axes"][axis]}
        refs = {k: ExtractorRef(name=v["name"], args={a: b for a, b in v.items() if a != "name"}) for k, v in ex.items()}
        w = r.get("window") or {}
        out.append(ResponseDef(response_key=r["response_key"], kind=r.get("kind", "curve"), test_channel=r["test_channel"],
                               axis=axis, extractor=refs, weight=float(r.get("weight", 1.0)),
                               window=ResponseWindow(w.get("x_min"), w.get("x_max"), w.get("split", "auto"), w.get("region_weights"))))
    return tuple(out)


def engine_params(mode: str, x_u: float, h: float, cfg: Config | None = None) -> dict[str, float]:
    cfg = cfg or Config()
    dend = x_u * 1.05
    tend = 1.0 if mode == "implicit" else dend / 0.5
    ep = {"mo_dend": dend, "mo_tend": tend, "mo_htop": h, "mo_hbot": 0.0}
    ep.update(output_request(ep, x_u, cfg))
    return ep


def body_material(E: float = 2.0e9, sy: float | None = 4.0e7) -> NeutralMaterial:
    el = ElasticBlock(E, 0.38, 1200.0)
    if sy is None:
        return NeutralMaterial(elastic=el)
    return NeutralMaterial(elastic=el, table=(TableRow(0.0, sy), TableRow(0.01, sy * 1.2), TableRow(0.05, sy * 1.5)))


def make_case(recipe: str, tmp: Path, *, recipe_dir: Path | None = None, mesh: MeshResult | None = None,
              jig: JigResult | None = None, x_u: float = 0.004, cfg: Config | None = None,
              params: Mapping[str, float] | None = None) -> SimulationCase:
    rdir = recipe_dir or (RECIPES / recipe)
    m = load_manifest(rdir)
    mesh = mesh or write_specimen(tmp / "mesh")
    if m["kind"] == "parametric" and jig is None:
        jig = write_jig(tmp / "mesh" / "jig")
    if m["kind"] == "fixed":
        jig = None
    a = m["solver"]["analysis"]
    modes = {a["primary"]} | ({a["fallback"]} if a.get("fallback", "none") != "none" else set())
    eps = {mode: engine_params(mode, x_u, mesh.top_z_at_load_m, cfg) for mode in sorted(modes)}
    prim = eps[a["primary"]]
    p = {pp["name"]: float(pp["default"]) for pp in m["parameters"]}
    p.update(params or {})
    return SimulationCase(
        case_id="t1-c1", test_key="t1", recipe_ref=f"{m['recipe_id']}@{m['version']}", recipe_dir=rdir, manifest=m,
        params_si=p, engine_params_si=eps, specimen_mesh=mesh, jig_mesh=jig, slots=slot_bindings(m),
        responses=responses_from_manifest(m), analysis_primary=a["primary"], analysis_fallback=a.get("fallback", "none"),
        output=OutputRequest(prim["mo_dtout"], prim["mo_dtplt"], prim["mo_dtmax"], ()))


# ======================================================================================
# 가짜 배치 스크립트
# ======================================================================================
def fake_config(**overrides: Any) -> Config:
    base = {
        "solver.lsdyna.script": str(FAKE_SCRIPT),
        "solver.lsdyna.batch_command": [sys.executable, "{script}", "-i", "{main}", "-n", "{ncpu}"],
        "solver.poll_interval_s": 0.05,
        "solver.kill_grace_s": 1.0,
        "solver.license_retry.interval_s": 0.1,
        "solver.license_retry.max_wait_s": 0.35,
        "solver.run_timeout_s": 60,
    }
    base.update(overrides)
    return Config(base)


def set_behavior(monkeypatch, tmp: Path, behavior: Mapping[str, Any]) -> Path:
    p = tmp / "fake_behavior.json"
    p.write_text(json.dumps(behavior))
    monkeypatch.setenv("FAKE_LSDYNA_BEHAVIOR", str(p))
    return p


def fake_adapter(**cfg_overrides: Any):
    from matobserver.core.registry import Registry

    reg = Registry.from_paths([FIX.parents[2] / "config" / "plugins.d"])
    return reg.create("solver", "lsdyna", {"config": fake_config(**cfg_overrides)})


# ======================================================================================
# verify_recipe 용 가짜 registry·services
# ======================================================================================
class FakeBoxMesher:
    name = "box"
    version = "0.0.0"
    role = "specimen"

    def __init__(self, options=None) -> None:
        self.calls = []

    def validate(self, spec):
        return []

    def mesh(self, spec, opts, out_dir):
        self.calls.append(opts.size_factor)
        nx = max(2, int(round(8 / opts.size_factor)))
        return write_specimen(Path(out_dir), nx=nx)


class FakeJigMesher:
    name = "jig.bending3p_rollers"
    version = "0.0.0"
    role = "jig"

    def __init__(self, options=None) -> None:
        pass

    def validate(self, spec):
        return []

    def mesh(self, spec, opts, out_dir):
        return write_jig(Path(out_dir), span_mm=spec.params_si["span"] * 1e3, r_ind=spec.params_si["indenter_radius"] * 1e3,
                         r_sup=spec.params_si["support_radius"] * 1e3)


class FakeBending:
    name = "bending"
    version = "0.0.0"

    def __init__(self, options=None) -> None:
        pass

    def reference_points(self, manifest):
        return (ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"}),)

    def responses(self, manifest, x_quantity):
        return responses_from_manifest(manifest, x_quantity)

    def engine_params(self, window, mode, mesh):
        return {k: v for k, v in engine_params(mode, window["X_u"], mesh.top_z_at_load_m).items()
                if k in ("mo_dend", "mo_tend", "mo_htop", "mo_hbot")}


class FakeRegistry:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.box = FakeBoxMesher()

    def create(self, plugin_type, name, options=None, **kw):
        if plugin_type == "solver":
            from matobserver.solvers.lsdyna.adapter import LsDynaAdapter

            return LsDynaAdapter({**(options or {}), "config": self.cfg})
        if plugin_type == "test_spec":
            return FakeBending()
        if plugin_type == "geometry_mesher":
            return self.box if name == "box" else FakeJigMesher()
        raise KeyError((plugin_type, name))


class _Never:
    def is_set(self) -> bool:
        return False


@dataclass
class FakeServices:
    root: Path
    config: Config
    registry: Any = None
    owner_kind: str = "recipe_verification"
    owner_id: str = "ver_fixture"
    cancel: Any = field(default_factory=_Never)
    artifacts: list = field(default_factory=list)
    progress_calls: list = field(default_factory=list)

    def workdir(self, *parts: str) -> Path:
        p = self.root / "_recipes" / self.owner_id
        for x in parts:
            p = p / x
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_artifact(self, path, kind, *, slot=None, test_key=None):
        self.artifacts.append((Path(path), kind))
        return f"art_{len(self.artifacts)}"

    def record_eval(self, item):
        pass

    def progress(self, stage, **fields):
        self.progress_calls.append((stage, fields))
