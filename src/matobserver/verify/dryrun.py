"""레시피 dry-run 검증 `verify_recipe` (module-boundaries.md §4.2, recipe.md §7.2–7.3).

기준 카드(`verification.reference_card`)·기준 치수·기본 파라미터로 실제 덱을 조립해 매니페스트 솔버로 실행한다.
메시는 registry의 GeometryMesher로 직접 생성(호출만), TestSpec도 registry로 얻는다. 정적 검사(§7.1)는 Backend가 먼저 한다.

흐름: 메시 크기 배율(`verification.mesh_convergence.size_factors`)마다 메시 → (parametric) 지그 → SimulationCase →
prepare/run/extract. 기준 실행 = 배율 1.0(없으면 첫 배율). 판정은 `recipe_checks`.
"""

from __future__ import annotations

import datetime as _dt
import traceback
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from ..core import errors as E
from ..core.config import Config
from ..core.types import (
    ElasticBlock,
    EngineServices,
    Extracted,
    GeometrySpec,
    JigSpec,
    MeshOptions,
    MeshResult,
    NeutralMaterial,
    OutputRequest,
    PreparedRun,
    ResolvedRecipe,
    RunStatus,
    SimulationCase,
    SlotBinding,
    TableRow,
)
from ..deck.assemble import find_damping, find_hardcoded_materials, find_mass_scaling, read_recipe_texts
from . import recipe_checks as rc


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def neutral_from_card_blocks(blocks: Mapping[str, Any]) -> NeutralMaterial:
    e = blocks["elastic"]
    table = blocks.get("table")
    return NeutralMaterial(
        elastic=ElasticBlock(float(e["youngs_modulus"]), float(e["poisson_ratio"]), float(e["density"])),
        table=None if table is None else tuple(TableRow(float(r["plastic_strain"]), float(r["true_stress"])) for r in table),
    )


def load_reference_card(recipe_dir: Path, manifest: Mapping[str, Any]) -> dict[str, NeutralMaterial]:
    rel = manifest["verification"]["reference_card"]
    data = yaml.safe_load((Path(recipe_dir) / rel).read_bytes().decode("utf-8")) or {}
    return {str(slot): neutral_from_card_blocks(blocks) for slot, blocks in data.items()}


def slot_bindings(manifest: Mapping[str, Any]) -> tuple[SlotBinding, ...]:
    out = []
    for s in manifest["material_slots"]:
        pids = s.get("part_ids") or [s["part_id"]]
        out.append(SlotBinding(slot=s["slot"], part_ids=tuple(int(p) for p in pids), mid=int(s["mid"]),
                               secid=int(s["secid"]), section=dict(s.get("section") or {}),
                               lcid=None if s.get("lcid") is None else int(s["lcid"])))
    return tuple(out)


def default_params_si(manifest: Mapping[str, Any]) -> dict[str, float]:
    params = {p["name"]: float(p["default"]) for p in manifest.get("parameters", []) or [] if p.get("default") is not None}
    params.update({k: float(v) for k, v in (manifest["verification"].get("reference_params") or {}).items()})
    return params


def reference_geometry(manifest: Mapping[str, Any], params_si: Mapping[str, float], testspec: Any) -> GeometrySpec:
    """기준 치수 → GeometrySpec. 슬롯 region에 `layer:<i>`가 있으면 layered_box(두께 균등 분할), 아니면 box.
    total_length가 없으면 span + 2·overhang (testspec.md §10.1)."""
    dims = {k: float(v) for k, v in (manifest["verification"].get("reference_dimensions") or {}).items()}
    if "total_length" not in dims and "span" in params_si:
        dims["total_length"] = params_si["span"] + 2.0 * params_si.get("overhang", 0.0)
    region_part_ids: dict[str, int] = {}
    layer_regions: list[str] = []
    for s in manifest["material_slots"]:
        for reg in s.get("regions", []) or []:
            region_part_ids[str(reg)] = int(s["part_id"])
            if str(reg).startswith("layer:"):
                layer_regions.append(str(reg))
    kind = "box"
    layers: tuple[tuple[str, float], ...] = ()
    if layer_regions:
        kind = "layered_box"
        n = len(layer_regions)
        layers = tuple((r, dims["thickness"] / n) for r in sorted(layer_regions, key=lambda r: int(r.split(":")[1])))
    return GeometrySpec(kind=kind, dimensions=dims, layers=layers,
                        placement=dict((manifest.get("specimen") or {}).get("placement") or {}),
                        region_part_ids=region_part_ids,
                        reference_points=tuple(testspec.reference_points(manifest)))


def mesh_options(manifest: Mapping[str, Any], cfg: Config, size_factor: float, geometry_kind: str) -> MeshOptions:
    sp = manifest["specimen"]
    el = sp["element"]
    ids = sp.get("id_ranges") or {}
    sym = (manifest.get("symmetry") or {}).get("type", "none")
    topo = el.get("stp_topology", "tet10") if geometry_kind == "stp" else el["topology"]
    return MeshOptions(
        topology=topo, target_size_m=float(el["target_size_m"]),
        min_elems_through_thickness=int(el.get("min_elems_through_thickness", 1)),
        max_elements=int(cfg.get("mesh.max_elements")), size_factor=float(size_factor),
        symmetry="none" if geometry_kind == "stp" else sym,
        node_id_range=tuple(ids.get("node", (1, 4_999_999))), element_id_range=tuple(ids.get("element", (1, 4_999_999))),
        part_set_id=int(sp.get("part_set_id", 0)), node_set_ids=dict(sp.get("node_sets") or {}),
    )


def jig_spec(manifest: Mapping[str, Any], params_si: Mapping[str, float], mesh: MeshResult, cfg: Config,
             geometry: GeometrySpec) -> JigSpec:
    jig = manifest["jig"]
    p: dict[str, float] = {}
    for gen_key, pname in (jig.get("params") or {}).items():
        if pname == "auto":
            if gen_key == "roller_length":
                el = manifest["specimen"]["element"]
                p[gen_key] = float(geometry.dimensions.get("width", 0.0)) + 2.0 * float(el["target_size_m"])
            continue
        p[gen_key] = float(params_si[pname])
    rigid = dict(jig.get("rigid") or {
        "E": cfg.get("mesh.jig.rigid_E_pa"), "poisson": cfg.get("mesh.jig.rigid_poisson"),
        "density": cfg.get("mesh.jig.rigid_density_kg_m3")})
    ids = jig.get("id_ranges") or {}
    return JigSpec(generator=jig["generator"], params_si=p, part_ids=dict(jig["part_ids"]), mids=dict(jig["mids"]),
                   rigid={k: float(v) for k, v in rigid.items()}, specimen=mesh,
                   symmetry=(manifest.get("symmetry") or {}).get("type", "none"),
                   node_id_range=tuple(ids.get("node", (5_000_000, 5_999_999))),
                   element_id_range=tuple(ids.get("element", (5_000_000, 5_999_999))))


def output_request(engine_params_si: Mapping[str, float], x_u: float, cfg: Config,
                   history_nodes: Sequence[int] = ()) -> dict[str, float]:
    """정렬 7단계(testspec.md §5): dt_out = Δx/(v·k), Δx = X_u / grid_points(격자 간격 근사), dt_plot = tend/frames,
    dt_max = dt_out. 반환은 mo_dtout·mo_dtplt·mo_dtmax (SI)."""
    tend = float(engine_params_si["mo_tend"])
    dend = float(engine_params_si["mo_dend"])
    v = dend / tend
    dx = x_u / max(int(cfg.get("alignment.grid_points")) - 1, 1)
    dt_out = dx / (v * int(cfg.get("alignment.output_oversample")))
    return {"mo_dtout": dt_out, "mo_dtplt": tend / int(cfg.get("solver.d3plot_frames")), "mo_dtmax": dt_out}


class _DryRun:
    def __init__(self, recipe: ResolvedRecipe, services: EngineServices) -> None:
        self.recipe = recipe
        self.services = services
        self.cfg: Config = services.config
        self.manifest = recipe.manifest
        reg = services.registry
        sname = self.manifest["solver"]["name"]
        self.solver = reg.create("solver", sname, {"config": self.cfg})
        self.testspec = reg.create("test_spec", self.manifest["test_type"], {})
        self.reg = reg

    def build_case(self, size_factor: float) -> SimulationCase:
        m = self.manifest
        params = default_params_si(m)
        geom = reference_geometry(m, params, self.testspec)
        mesher = self.reg.create("geometry_mesher", geom.kind, {})
        tag = f"s{size_factor:g}"
        mesh = mesher.mesh(geom, mesh_options(m, self.cfg, size_factor, geom.kind), self.services.workdir("mesh", tag))
        jig = None
        if m.get("kind") == "parametric":
            jgen = self.reg.create("geometry_mesher", m["jig"]["generator"], {})
            jig = jgen.mesh(jig_spec(m, params, mesh, self.cfg, geom), None, self.services.workdir("mesh", tag, "jig"))
        responses = tuple(self.testspec.responses(m, "displacement"))
        x_u = float(m["verification"]["reference_window_m"])
        window = {"x_l": 0.0, "x_u": x_u, "X_l": 0.0, "X_u": x_u}
        a = m["solver"]["analysis"]
        modes = [a["primary"]] + ([a["fallback"]] if a.get("fallback", "none") != "none" else [])
        engine_params: dict[str, dict[str, float]] = {}
        for mode in modes:
            ep = dict(self.testspec.engine_params(window, mode, mesh))
            ep.update(output_request(ep, x_u, self.cfg))
            engine_params[mode] = ep
        prim = engine_params[a["primary"]]
        return SimulationCase(
            case_id="verify-c1", test_key="verify", recipe_ref=self.recipe.recipe_ref, recipe_dir=Path(self.recipe.store_dir),
            manifest=m, params_si=params, engine_params_si=engine_params, specimen_mesh=mesh, jig_mesh=jig,
            slots=slot_bindings(m), responses=responses, analysis_primary=a["primary"],
            analysis_fallback=a.get("fallback", "none"),
            output=OutputRequest(dt_out_s=prim["mo_dtout"], dt_plot_s=prim["mo_dtplt"], dt_max_s=prim["mo_dtmax"],
                                 history_nodes=()),
        )

    def run_one(self, size_factor: float, materials: Mapping[str, NeutralMaterial]) -> tuple[SimulationCase, PreparedRun, RunStatus, Extracted]:
        case = self.build_case(size_factor)
        prepared = self.solver.prepare(case, materials, self.services.workdir("dryrun", f"s{size_factor:g}"))
        ncpu = int(self.cfg.get("solver.lsdyna.ncpu"))
        status = self.solver.run(prepared, ncpu, float(self.cfg.get("solver.run_timeout_s")), self.services.cancel)
        extracted = self.solver.extract(prepared, status)
        return case, prepared, status, extracted


def _beam_k_th(case: SimulationCase, materials: Mapping[str, NeutralMaterial]) -> float | None:
    dims = case.manifest["verification"].get("reference_dimensions") or {}
    span = case.params_si.get("span")
    if span is None or "width" not in dims or "thickness" not in dims:
        return None
    es = [materials[b.slot].elastic.youngs_modulus for b in case.slots if b.slot in materials]
    if not es:
        return None
    return rc.beam_stiffness(sum(es) / len(es), float(dims["width"]), float(dims["thickness"]), float(span))


def _bundle(services: EngineServices) -> list[dict[str, str]]:
    src = services.workdir("dryrun")
    out_dir = services.workdir("artifacts")
    path = out_dir / "verification_bundle.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file() and not p.is_symlink():
                z.write(p, p.relative_to(src).as_posix())
    return [{"artifact_id": services.add_artifact(path, "verification_bundle"), "kind": "verification_bundle"}]


def verify_recipe(recipe: ResolvedRecipe, services: EngineServices, strict: bool) -> Mapping[str, Any]:
    """recipe.md §7.3 VerificationReport. 검증 실행 자체가 실패하면 status=error (레시피는 rejected)."""
    started = _now()
    report: dict[str, Any] = {"verification_id": services.owner_id, "status": "error", "started_at": started,
                              "finished_at": None, "solver": {"name": recipe.manifest.get("solver", {}).get("name")},
                              "checks": [], "artifacts": []}
    checks: list[dict[str, Any]] = report["checks"]
    try:
        dr = _DryRun(recipe, services)
        report["solver"]["adapter_version"] = getattr(dr.solver, "version", None)
        m = recipe.manifest
        materials = load_reference_card(Path(recipe.store_dir), m)
        missing = [b.slot for b in slot_bindings(m) if b.slot not in materials]
        if missing:
            raise ValueError(f"reference card has no entry for slots {missing}")
        factors = list((m["verification"].get("mesh_convergence") or {}).get("size_factors") or [1.0])
        base_factor = 1.0 if 1.0 in factors else factors[0]
        runs: dict[float, tuple[SimulationCase, PreparedRun, RunStatus, Extracted]] = {}
        for f in [base_factor] + [f for f in factors if f != base_factor]:
            runs[float(f)] = dr.run_one(float(f), materials)
            services.progress("verify", size_factor=float(f), state=runs[float(f)][2].state.value)
        case, prepared, status, extracted = runs[float(base_factor)]
        report["solver"]["version"] = status.solver_version or dr.solver.solver_version()

        checks += rc.check_termination(status)
        checks.append(rc.check_response(extracted, case.responses, dr.cfg))
        main_resp = next((r for r in case.responses if r.kind == "curve"), None)
        curve = extracted.curves.get(main_resp.response_key) if main_resp else None
        checks.append(rc.check_units(curve, _beam_k_th(case, materials), str(m.get("kind")), dr.cfg))
        pen_fn = getattr(dr.solver, "initial_penetration_m", None)
        pen = pen_fn(prepared, status) if callable(pen_fn) else None
        checks.append(rc.check_penetration(curve, pen, dr.cfg))
        mc = m["verification"].get("mesh_convergence") or {}
        conv_curves = []
        for f in factors:
            c = runs[float(f)][3].curves.get(main_resp.response_key) if main_resp else None
            if c is not None:
                conv_curves.append((float(f), c))
        checks.append(rc.check_mesh_convergence(conv_curves, mc.get("quantities") or ["initial_slope", "force_at_end"], dr.cfg))
        ref_csv = m["verification"].get("reference_curve")
        if ref_csv and curve is not None and main_resp is not None:
            checks.append(dict(rc.check_reference_curve(curve, Path(recipe.store_dir) / ref_csv, main_resp, dr.cfg)))
        else:
            checks.append({"code": E.VERIFY_REFERENCE_CURVE, "result": "skipped", "detail": "no reference_curve"})
        texts = read_recipe_texts(case)
        pvals = {k: v for k, v in case.params_si.items()}
        checks.append(rc.warn_check(E.WARN_MASS_SCALING, find_mass_scaling(texts, pvals)))
        checks.append(rc.warn_check(E.WARN_DAMPING, find_damping(texts)))
        checks.append(rc.warn_check(E.WARN_HARDCODED_MATERIAL, find_hardcoded_materials(texts)))
        checks.append(rc.check_ke_ratio(extracted, status.mode_used, dr.cfg))
        checks.append(rc.check_no_fallback(m))
        checks.append(rc.check_compliance(m))
        report["status"] = rc.report_status(checks, strict)
        try:
            report["artifacts"] = _bundle(services)
        except Exception as e:  # 묶음 실패는 판정에 영향 없음
            report["artifacts_error"] = repr(e)
    except Exception as e:
        report["status"] = "error"
        report["error"] = {"message": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=5)}
    report["finished_at"] = _now()
    return report


__all__ = ["verify_recipe", "load_reference_card", "slot_bindings", "reference_geometry", "output_request"]
