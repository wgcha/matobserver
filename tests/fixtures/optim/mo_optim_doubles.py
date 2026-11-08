"""Impl-Optim 테스트 더블·픽스처 빌더 (LS-DYNA·실제 메셔 없이 엔진을 검증).

* `BoxMesherDouble` / `LayeredBoxMesherDouble` (GeometryMesher, role=specimen): 계약 §2 형식의 성긴 hex8 .k 와 MeshResult.
* `JigDouble` (role=jig): 참조 노드만 있는 JigResult.
* `FakeServices` (EngineServices): 작업 폴더·artifact·이력·진행·취소.
* `make_registry`, `make_manifest`, `make_spec`, `synthetic_curve` 등 G2용 빌더.

pytest `--import-mode=importlib` 이라 각 테스트 conftest 가 이 폴더를 sys.path 에 넣는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

import numpy as np

from matobserver.core.config import Config
from matobserver.core.hashing import sha256_json
from matobserver.core.registry import PluginEntry, Registry, parse_plugin_file
from matobserver.core.types import (
    CalibrationSpec,
    GeometrySpec,
    Issue,
    JigResult,
    JigSpec,
    MeshOptions,
    MeshQuality,
    MeshResult,
    ResolvedMaterials,
    ResolvedRecipe,
    ResolvedTest,
    RunRecord,
)
from matobserver.core.units import m_to_mm

REPO = Path(__file__).resolve().parents[3]
OPTIM_YAML = REPO / "config" / "plugins.d" / "optim.yaml"


# ======================================================================================
# 메셔 더블
# ======================================================================================
def _write_k(path: Path, nodes: list[tuple[int, float, float, float]], elems: list[tuple[int, int, list[int]]]) -> None:
    lines = ["*KEYWORD", "$ matobserver mesh", "$ generator=double version=0 topology=hex8 units=mm-N-s-tonne", "*NODE"]
    for nid, x, y, z in nodes:
        lines.append(f"{nid:8d}{m_to_mm(x):16.9e}{m_to_mm(y):16.9e}{m_to_mm(z):16.9e}{0:8d}{0:8d}")
    lines.append("*ELEMENT_SOLID")
    for eid, pid, ns in elems:
        lines.append(f"{eid:8d}{pid:8d}")
        lines.append("".join(f"{n:8d}" for n in ns))
    lines.append("*END")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


class BoxMesherDouble:
    name: ClassVar[str] = "box"
    version: ClassVar[str] = "0.0.1"
    role: ClassVar[str] = "specimen"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def validate(self, spec) -> list[Issue]:
        d = spec.dimensions
        bad = [k for k in ("width", "thickness", "total_length") if not d.get(k) or d[k] <= 0]
        return [Issue("GEOMETRY_DIMENSION_MISSING", f"missing {k}", "error") for k in bad]

    def mesh(self, spec: GeometrySpec, opts: MeshOptions | None, out_dir: Path) -> MeshResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        W, H, Lt = spec.dimensions["width"], spec.dimensions["thickness"], spec.dimensions["total_length"]
        sym = opts.symmetry if opts is not None else "none"
        x0 = 0.0 if sym == "quarter" else -Lt / 2
        y0 = 0.0 if sym in ("quarter", "half") else -W / 2
        xs = np.linspace(x0, Lt / 2, 5)
        # 영역: (name, pid, ylo, yhi, zlo, zhi)
        regions = []
        if spec.layers:
            acc = 0.0
            for name, t in spec.layers:
                pid = spec.region_part_ids[name]
                if spec.stack_axis == "z":
                    regions.append((name, pid, y0, W / 2, acc, acc + t))
                else:
                    ylo = -W / 2 + acc
                    regions.append((name, pid, max(ylo, y0), max(ylo + t, y0), 0.0, H))
                acc += t
            regions = [r for r in regions if r[3] > r[2]]
        else:
            regions.append(("specimen", spec.region_part_ids.get("specimen", 1), y0, W / 2, 0.0, H))
        nodes: list[tuple[int, float, float, float]] = []
        elems: list[tuple[int, int, list[int]]] = []
        nid = eid = 0
        info = {}
        for name, pid, ylo, yhi, zlo, zhi in regions:
            ys = np.linspace(ylo, yhi, 3)
            zs = np.linspace(zlo, zhi, 3)
            idx = {}
            for i, x in enumerate(xs):
                for j, y in enumerate(ys):
                    for k, z in enumerate(zs):
                        nid += 1
                        idx[(i, j, k)] = nid
                        nodes.append((nid, float(x), float(y), float(z)))
            cnt = 0
            for i in range(len(xs) - 1):
                for j in range(len(ys) - 1):
                    for k in range(len(zs) - 1):
                        eid += 1
                        cnt += 1
                        c = [idx[(i, j, k)], idx[(i + 1, j, k)], idx[(i + 1, j + 1, k)], idx[(i, j + 1, k)],
                             idx[(i, j, k + 1)], idx[(i + 1, j, k + 1)], idx[(i + 1, j + 1, k + 1)], idx[(i, j + 1, k + 1)]]
                        elems.append((eid, pid, c))
            fac = {"quarter": 4.0, "half": 2.0}.get(sym, 1.0)
            vol = (Lt / 2 - x0) * (yhi - ylo) * (zhi - zlo) * (fac if sym != "none" else 1.0)
            info[name] = {"part_id": float(pid), "element_count": float(cnt), "volume_m3": float(vol)}
        path = out_dir / "specimen.k"
        _write_k(path, nodes, elems)
        return MeshResult(mesh_path=path, node_count=len(nodes), element_count=len(elems), regions=info,
                          volume_m3=W * H * Lt, reference_volume_m3=W * H * Lt,
                          bbox_m=((x0, y0, 0.0), (Lt / 2, W / 2, H)), reference_nodes={"midspan_bottom": 1},
                          node_sets={}, top_z_at_load_m=H, bottom_z_m=0.0, symmetry=sym,
                          quality=MeshQuality(1.0, 1.0, 2), target_size_used_m=H / 2, warnings=())


class LayeredBoxMesherDouble(BoxMesherDouble):
    name: ClassVar[str] = "layered_box"


class JigDouble:
    name: ClassVar[str] = "jig.bending3p_rollers"
    version: ClassVar[str] = "0.0.1"
    role: ClassVar[str] = "jig"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def validate(self, spec) -> list[Issue]:
        return []

    def mesh(self, spec: JigSpec, opts, out_dir: Path) -> JigResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "jig.k"
        p.write_text("*KEYWORD\n*END\n", encoding="ascii")
        return JigResult(mesh_path=p, reference_nodes={"indenter_ref": 5000001, "support_top": 5000002},
                         part_ids=dict(spec.part_ids))


# ======================================================================================
# 레지스트리·서비스
# ======================================================================================
def make_registry(extra: Sequence[PluginEntry] = ()) -> Registry:
    entries = parse_plugin_file(OPTIM_YAML)
    mod = "mo_optim_doubles"
    entries += [
        PluginEntry("geometry_mesher", "box", f"{mod}:BoxMesherDouble", "0.0.1"),
        PluginEntry("geometry_mesher", "layered_box", f"{mod}:LayeredBoxMesherDouble", "0.0.1"),
        PluginEntry("geometry_mesher", "jig.bending3p_rollers", f"{mod}:JigDouble", "0.0.1"),
    ]
    entries += list(extra)
    return Registry(entries)


class NeverCancel:
    def is_set(self) -> bool:
        return False


class FlagCancel:
    def __init__(self) -> None:
        self.flag = False

    def is_set(self) -> bool:
        return self.flag


@dataclass
class FakeServices:
    root: Path
    registry: Registry
    config: Config
    owner_kind: str = "job"
    owner_id: str = "cal_TEST"
    cancel: Any = field(default_factory=NeverCancel)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    evals: list[dict[str, Any]] = field(default_factory=list)
    progress_log: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def workdir(self, *parts: str) -> Path:
        p = Path(self.root, self.owner_id, *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_artifact(self, path: Path, kind: str, *, slot: str | None = None, test_key: str | None = None) -> str:
        aid = f"art_{len(self.artifacts):04d}"
        self.artifacts.append({"artifact_id": aid, "path": Path(path), "kind": kind, "slot": slot, "test_key": test_key})
        return aid

    def record_eval(self, item: Mapping[str, Any]) -> None:
        self.evals.append(dict(item))

    def progress(self, stage: str, **fields: Any) -> None:
        self.progress_log.append((stage, dict(fields)))


# ======================================================================================
# 매니페스트·요청 빌더
# ======================================================================================
def make_manifest(*, slots: Sequence[str] = ("body",), symmetry: str = "quarter", compliance: float | None = 1.8e-7,
                  layered_regions: bool = False, kind: str = "parametric", explicit_controls: bool = True,
                  extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    scale = {"quarter": 4.0, "half": 2.0, "none": 1.0}[symmetry]
    mslots = []
    for i, s in enumerate(slots):
        mslots.append({"slot": s, "regions": ([f"layer:{i}"] if layered_regions else ["specimen"]), "part_id": i + 1,
                       "mid": 101 + i, "secid": 101 + i, "lcid": 1101 + i,
                       "section": {"keyword": "SECTION_SOLID", "elform": -2}})
    m = {
        "schema_version": "1.0", "recipe_id": "flex3p-test", "version": 1, "kind": kind, "test_type": "bending",
        "title": "test", "unit_system": "mm-N-s-tonne",
        "solver": {"name": "surrogate_beam", "analysis": {"primary": "implicit", "fallback": "explicit"}},
        "deck": {"main": "deck/main.k", "controls": {"implicit": "deck/controls_implicit.k",
                                                      **({"explicit": "deck/controls_explicit.k"} if explicit_controls else {})},
                 "files": []},
        "symmetry": {"type": symmetry, "planes": [], "force_scale": scale},
        "specimen": {"geometry_kinds": ["box", "layered_box", "stp"],
                     "element": {"topology": "hex8", "target_size_m": 0.0005, "min_elems_through_thickness": 4},
                     "part_set_id": 900, "node_sets": {"sym_x0": 901, "sym_y0": 902},
                     "placement": {"length_axis": "x", "width_axis": "y", "load_axis": "-z", "origin": "midspan_bottom"}},
        "material_slots": mslots,
        "parameters": [
            {"name": "span", "type": "R", "dimension": "length", "unit_si": "m", "default": 0.064, "range": [0.01, 0.3],
             "matnexus_key": "span"},
            {"name": "r_ind", "type": "R", "dimension": "length", "unit_si": "m", "default": 0.005, "range": [0.001, 0.02],
             "matnexus_key": None},
            {"name": "overhang", "type": "R", "dimension": "length", "unit_si": "m", "default": 0.008,
             "range": [0.0, 0.05], "matnexus_key": None},
            {"name": "fric", "type": "R", "dimension": "dimensionless", "unit_si": "1", "default": 0.1,
             "range": [0.0, 0.6], "matnexus_key": None},
        ],
        "responses": [{"response_key": "force_disp", "test_channel": "force", "axis": "displacement",
                       "extractor": {"y": {"name": "rcforc_resultant", "contact_id": 1, "component": "z", "sign": -1,
                                           "scale": "symmetry"},
                                     "x": {"name": "nodout_disp", "node": {"ref": "indenter_ref"}, "component": "z",
                                           "sign": -1}},
                       "alt_axes": {"deflection": {"x": {"name": "nodout_rel_disp", "node": {"ref": "midspan_bottom"},
                                                         "ref_nodes": [{"ref": "support_top"}], "component": "z",
                                                         "sign": -1}}},
                       "weight": 1.0, "window": {"split": "auto"}}],
        "compliance": ({"value_m_per_n": compliance, "method": "rigid_block", "calibrated_at": "2026-10-01"}
                       if compliance is not None else None),
        "verification": {"reference_card": "verification/reference_card.yaml"},
    }
    if kind == "parametric":
        m["jig"] = {"generator": "jig.bending3p_rollers", "part_ids": {"indenter": 1001, "support": 1002},
                    "mids": {"indenter": 1001, "support": 1002},
                    "params": {"span": "span", "indenter_radius": "r_ind", "roller_length": "auto"},
                    "reference_nodes": ["indenter_ref", "support_top"]}
    if extra:
        m.update(extra)
    return m


def section_boxes(width: float, thickness: float, layers: Sequence[tuple[str, float]] = (), stack_axis: str = "z",
                  slot: str = "body"):
    if not layers:
        return [(slot, -width / 2, width / 2, 0.0, thickness)]
    out, acc = [], 0.0
    for s, t in layers:
        if stack_axis == "z":
            out.append((s, -width / 2, width / 2, acc, acc + t))
        else:
            out.append((s, -width / 2 + acc, -width / 2 + acc + t, 0.0, thickness))
        acc += t
    return out


def synthetic_curve(materials: Mapping[str, tuple[float, list[tuple[float, float]] | None]], *, width: float,
                    thickness: float, span: float, d_max: float, n: int = 400, layers=(), stack_axis: str = "z",
                    noise: float = 0.0, seed: int = 0, compliance: float | None = None):
    """대리모델과 같은 물리로 가상 시험 곡선(SI). noise = 점별 상대 표준편차(곱셈 가우스)."""
    from matobserver.solvers.surrogate.beam import Section, Uniaxial, three_point_bending

    slot0 = next(iter(materials))
    sec = Section.from_boxes(section_boxes(width, thickness, layers, stack_axis, slot0), 240)
    mats = [Uniaxial.from_card(*materials[s]) for s in sec.slots]
    d = np.linspace(0.0, d_max, n)
    F = three_point_bending(sec, mats, span, d).force
    if noise > 0:
        rng = np.random.default_rng(seed)
        F = F * (1.0 + noise * rng.standard_normal(len(F)))
    x = d + (compliance * F if compliance else 0.0)
    return x, F


def make_run(run_id: str, x: np.ndarray, F: np.ndarray, *, width: float, thickness: float, span: float,
             test_speed: float | None = 3.3e-5, extra_channels: Mapping[str, np.ndarray] | None = None) -> RunRecord:
    ch = {"force": np.asarray(F, dtype=float), "displacement": np.asarray(x, dtype=float)}
    ch.update(extra_channels or {})
    dims = {"width": width, "thickness": thickness, "span": span}
    return RunRecord(run_id=run_id, test_type_key="flexural", channels=ch, specimen_dimensions=dims,
                     conditions=({"temperature": 296.15, "test_speed": test_speed} if test_speed else {"temperature": 296.15}),
                     instrument="UTM-01", specimen_id=f"S-{run_id}", material_id=None, source_sha256=None,
                     fetched_hash=sha256_json({"x": list(map(float, x)), "F": list(map(float, F))}))


def make_test(test_key: str, runs: Sequence[RunRecord], manifest: Mapping[str, Any], geometry: Mapping[str, Any],
              *, store_dir: Path, recipe_params: Mapping[str, float] | None = None, weight: float = 1.0,
              displacement_channel: str = "auto") -> ResolvedTest:
    rr = ResolvedRecipe(recipe_ref=f"{manifest['recipe_id']}@{manifest['version']}",
                        content_hash=sha256_json(manifest), store_dir=store_dir, manifest=manifest)
    return ResolvedTest(test_key=test_key, test_type="bending", recipe=rr, geometry=dict(geometry), runs=tuple(runs),
                        displacement_channel=displacement_channel, recipe_params_si=dict(recipe_params or {}),
                        user_window={}, weight=weight)


def make_spec(tests: Sequence[ResolvedTest], slots: Sequence[Mapping[str, Any]], *, mode: str = "A",
              optimizer=("bo", {}), elastic_optimizer=("lsq", {}), solver=("surrogate_beam", {}),
              max_evaluations: int = 120, seed: int = 20261108, job_id: str = "cal_TEST") -> CalibrationSpec:
    req = {"tests": [t.test_key for t in tests], "slots": [dict(s) for s in slots], "opt": [optimizer[0], elastic_optimizer[0]],
           "seed": seed, "budget": max_evaluations}
    return CalibrationSpec(job_id=job_id, schema_version="1.0", tests=tuple(tests),
                           materials=ResolvedMaterials(mode=mode, slots=tuple(dict(s) for s in slots)),
                           optimizer=optimizer, elastic_optimizer=elastic_optimizer, solver=solver,
                           friction={"mode": "fixed", "value": 0.1},
                           budget={"max_evaluations": max_evaluations, "max_wall_time_s": 3600, "run_timeout_s": 60},
                           seed=seed, request_hash=sha256_json(req))


def swift_table(K: float, eps0: float, n: float, max_strain: float = 0.2, points: int = 60):
    ep = np.concatenate([[0.0], np.geomspace(1e-5, max_strain, points - 1)])
    return [(float(e), float(K * (eps0 + e) ** n)) for e in ep]


def reference(nu: float = 0.38, rho: float = 1200.0, E: float | None = None, table=None) -> dict[str, Any]:
    el = {"poisson_ratio": nu, "density": rho}
    if E is not None:
        el["youngs_modulus"] = E
    return {"elastic": el, "table": ([{"plastic_strain": a, "true_stress": b} for a, b in table] if table else None)}


# ======================================================================================
# 엔진 경로 시험용 플러그인 더블
# ======================================================================================
from matobserver.core.types import (  # noqa: E402
    Extracted as _Extracted,
    ParamSpace as _ParamSpace,
    ParamSpec as _ParamSpec,
    RunState as _RunState,
    RunStatus as _RunStatus,
)
from matobserver.solvers.surrogate.adapter import SurrogateBeamAdapter as _SB  # noqa: E402
from matobserver.testspecs.bending import BendingTestSpec as _BT  # noqa: E402

SPY_LOG: list[dict[str, Any]] = []


class SpyAdapter(_SB):
    """prepare 에 넘어온 카드·케이스를 기록 (O-11, O-12)."""

    name: ClassVar[str] = "spy_beam"
    version: ClassVar[str] = "0.0.1"

    def prepare(self, case, materials, workdir, mode=None, overrides=None):
        SPY_LOG.append({"case": case, "materials": dict(materials), "mode": mode, "overrides": overrides})
        return super().prepare(case, materials, workdir, mode, overrides)


class FlakyAdapter(_SB):
    """옵션 fail_rate 비율로 결정적 무작위 실패(ERROR, 곡선 없음) 주입 (O-9)."""

    name: ClassVar[str] = "flaky_beam"
    version: ClassVar[str] = "0.0.1"

    def __init__(self, options=None):
        super().__init__(options)
        self._rng = np.random.default_rng(int(self.options.get("seed", 0)))
        self.rate = float(self.options.get("fail_rate", 0.3))
        self.failures = 0

    def run(self, prepared, ncpu, timeout_s, cancel):
        if self._rng.random() < self.rate:
            self.failures += 1
            (Path(prepared.workdir) / "surrogate_result.json").write_text('{"state": "error"}', encoding="utf-8")
            return _RunStatus(_RunState.ERROR, prepared.mode, False, 0.0, "E r r o r", None, 0, ())
        return super().run(prepared, ncpu, timeout_s, cancel)


class ExplicitBiasAdapter(_SB):
    """explicit 재현 검사용: explicit(overrides) 실행 곡선에 배율·KE/IE·실패를 옵션으로 준다 (O-14)."""

    name: ClassVar[str] = "xc_beam"
    version: ClassVar[str] = "0.0.1"
    ORDER: list[str] = []

    def __init__(self, options=None):
        super().__init__(options)
        self.scale = float(self.options.get("explicit_scale", 1.0))
        self.fail = bool(self.options.get("explicit_fail", False))
        self.ke = float(self.options.get("explicit_ke", 0.002))
        self.unsupported = bool(self.options.get("no_overrides", False))

    def capabilities(self):
        c = super().capabilities()
        if self.unsupported:
            from dataclasses import replace as _r
            return _r(c, supports_overrides=False)
        return c

    def single_element_uniaxial(self, material, strain_max, workdir, timeout_s):
        ExplicitBiasAdapter.ORDER.append("roundtrip")
        return super().single_element_uniaxial(material, strain_max, workdir, timeout_s)

    def prepare(self, case, materials, workdir, mode=None, overrides=None):
        if overrides is not None:
            ExplicitBiasAdapter.ORDER.append("explicit")
        return super().prepare(case, materials, workdir, mode, overrides)

    def run(self, prepared, ncpu, timeout_s, cancel):
        st = super().run(prepared, ncpu, timeout_s, cancel)
        if prepared.mode == "explicit" and self.fail:
            from dataclasses import replace as _r
            return _r(st, state=_RunState.ERROR)
        return st

    def extract(self, prepared, status):
        ex = super().extract(prepared, status)
        if prepared.mode != "explicit":
            return ex
        from matobserver.core.types import CurveData as _CD
        curves = {k: _CD(v.x, v.y * self.scale, v.x_quantity, v.y_quantity) for k, v in ex.curves.items()}
        return _Extracted(curves, ex.scalars, ex.field_max, {"ke_ie_ratio_max": self.ke})


class FrictionTestSpec(_BT):
    """비재료 변수 `mu` → 레시피 `fric` (R9, O-12)."""

    name: ClassVar[str] = "bending_mu"
    version: ClassVar[str] = "0.0.1"

    def param_space(self, test_input, manifest):
        return _ParamSpace((_ParamSpec("mu", "1", 0.0, 0.6, 0.25, "linear", "elastic", "friction"),))

    def param_targets(self):
        return {"mu": "recipe:fric"}


def double_entries() -> list[PluginEntry]:
    m = "mo_optim_doubles"
    return [PluginEntry("solver", "spy_beam", f"{m}:SpyAdapter", "0.0.1"),
            PluginEntry("solver", "flaky_beam", f"{m}:FlakyAdapter", "0.0.1"),
            PluginEntry("solver", "xc_beam", f"{m}:ExplicitBiasAdapter", "0.0.1"),
            PluginEntry("test_spec", "bending_mu", f"{m}:FrictionTestSpec", "0.0.1")]
