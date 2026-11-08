"""엔진 전처리: 슬롯·θ 조립(plugins.md §3 R1–R9), 시험 데이터 매핑·정렬(testspec.md §5), 케이스(§9.1), 메시(§4.3),
SimulationCase 구성(§4.4). 플러그인 구현은 import하지 않고 `services.registry`로만 얻는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from matobserver.core import errors as E
from matobserver.core.errors import EngineFailure
from matobserver.core.types import (
    CalibrationSpec,
    ElasticBlock,
    GeometrySpec,
    Issue,
    JigResult,
    JigSpec,
    MeshOptions,
    MeshResult,
    NeutralMaterial,
    OutputRequest,
    ParamSpace,
    ParamSpec,
    ResolvedTest,
    ResponseDef,
    RunRecord,
    SimulationCase,
    SlotBinding,
    TableRow,
    TestData,
)

from . import alignment as AL
from .identifiability import config_signature

MATERIAL_CONFIG_OPTIONS = {  # 설정 → 재료 모델 옵션 (요청·레지스트리 옵션이 없을 때만)
    "default_E_range_pa": "material.default_E_range_pa",
    "default_sigma_y_range_pa": "material.default_sigma_y_range_pa",
    "default_density_kg_m3": "material.default_density_kg_m3",
    "max_strain": "material.mat024.max_strain",
    "default_law": "material.mat024_coeff.default_law",
    "table_points": "result.table_points",
    "table_nodes": "material.mat024_table.nodes",
    "table_spacing": "result.table_spacing",
    "table_first_step": "result.table_first_step",
    "first_node_strain": "material.mat024_table.first_node_strain",  # 1.1
}

CONTRACT_1_1_DEFAULTS = {  # core 설정 표에 아직 없을 때의 1.1 계약 기본값
    "material.mat024_table.first_node_strain": 0.002,
    "engine.stages.elastic.optimizer": "lsq",
    "engine.stages.plastic.optimizer": "lsq",
}


def cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """설정 키가 core 표에 없을 수도 있는 1.1 키를 안전하게 읽는다."""
    try:
        if key in cfg:
            return cfg.get(key)
    except Exception:
        pass
    return CONTRACT_1_1_DEFAULTS.get(key, default)


def job_option(spec: Any, services: Any, key: str) -> Any:
    """(1.1) 잡 옵션 우선순위: CalibrationSpec 필드 → services.job_options[key] → None(호출자가 설정으로)."""
    v = getattr(spec, key, None)
    if v is not None:
        return v
    jo = getattr(services, "job_options", None)
    if jo:
        try:
            v = jo.get(key) if hasattr(jo, "get") else None
        except Exception:
            v = None
    return v


def warn(code: str, message: str, **kw: Any) -> dict[str, Any]:
    out = {"code": code, "message": message}
    out.update({k: v for k, v in kw.items() if v is not None})
    return out


def neutral_from_any(ref: Any, default_density: float) -> tuple[NeutralMaterial | None, bool]:
    """NeutralMaterial 또는 card_blocks dict → NeutralMaterial. (값, density 기본값 사용 여부)."""
    if ref is None:
        return None, False
    if isinstance(ref, NeutralMaterial):
        return ref, False
    el = dict(ref.get("elastic") or {})
    defaulted = el.get("density") is None
    E_ = el.get("youngs_modulus")
    elastic = ElasticBlock(
        youngs_modulus=float(E_) if E_ is not None else float("nan"),
        poisson_ratio=float(el.get("poisson_ratio", 0.3) if el.get("poisson_ratio") is not None else 0.3),
        density=float(default_density if defaulted else el["density"]),
    )
    table = ref.get("table")
    rows = None if not table else tuple(TableRow(float(r["plastic_strain"]), float(r["true_stress"])) for r in table)
    return NeutralMaterial(elastic=elastic, table=rows), defaulted


# ======================================================================================
# 슬롯·θ
# ======================================================================================
@dataclass
class SlotCtx:
    name: str
    role: str
    model_name: str
    model_version: str
    model: Any
    options: dict[str, Any]
    reference: NeutralMaterial | None
    overrides: dict[str, dict[str, Any]]
    member_position: int | None
    density_defaulted: bool
    fixed_values: dict[str, float] = field(default_factory=dict)
    space_local: ParamSpace = field(default_factory=lambda: ParamSpace(()))  # fixed_value 제거 후
    full_local: ParamSpace = field(default_factory=lambda: ParamSpace(()))  # 모델 param_space (덮어쓰기 적용)

    @property
    def identify(self) -> bool:
        return self.role == "identify"


def apply_overrides(space: ParamSpace, overrides: Mapping[str, Mapping[str, Any]]) -> tuple[ParamSpace, ParamSpace, dict[str, float]]:
    """요청 params(ParamOverride) 적용 → (전체, fixed 제거, fixed 값)."""
    names = set(space.names())
    unknown = set(overrides) - names
    if unknown:
        raise EngineFailure(E.INTERNAL_ERROR, f"{E.PARAM_UNKNOWN}: {sorted(unknown)}")
    params, fixed = [], {}
    for p in space.params:
        o = dict(overrides.get(p.name) or {})
        lo = float(o["lower"]) if o.get("lower") is not None else p.lower
        hi = float(o["upper"]) if o.get("upper") is not None else p.upper
        init = float(o["initial"]) if o.get("initial") is not None else p.initial
        if init is not None:
            init = min(max(init, lo), hi)
        q = replace(p, lower=lo, upper=hi, initial=init)
        params.append(q)
        if o.get("fixed_value") is not None:
            fixed[p.name] = float(o["fixed_value"])
    full = ParamSpace(tuple(params), space.constraints)
    kept = tuple(p for p in params if p.name not in fixed)
    kn = {p.name for p in kept}
    cons = tuple(c for c in space.constraints if all(n in kn for n in c.params))
    return full, ParamSpace(kept, cons), fixed


def center_value(p: ParamSpec) -> float:
    if p.initial is not None:
        return float(p.initial)
    if p.scale == "log" and p.lower > 0:
        return math.sqrt(p.lower * p.upper)
    return 0.5 * (p.lower + p.upper)


# ======================================================================================
# 시험·케이스
# ======================================================================================
@dataclass
class RunCtx:
    run: RunRecord
    data: TestData
    aligned: dict[str, AL.AlignedTest]
    params_si: dict[str, float]
    dims: dict[str, float]
    case_id: str = ""


@dataclass
class CaseCtx:
    case_id: str
    test_key: str
    run_ids: list[str]
    dims: dict[str, float]
    params_si: dict[str, float]
    geometry: GeometrySpec
    mesh: MeshResult | None = None
    jig: JigResult | None = None
    sim_case: SimulationCase | None = None
    X_u: float = 0.0
    dx_min: float = math.inf


@dataclass
class TestCtx:
    key: str
    test: ResolvedTest
    spec: Any
    spec_name: str
    spec_version: str
    manifest: Mapping[str, Any]
    compliance: float | None
    responses: tuple[ResponseDef, ...]
    x_quantity: str
    quasi_static: bool
    runs: list[RunCtx]
    cases: list[CaseCtx] = field(default_factory=list)
    space: ParamSpace = field(default_factory=lambda: ParamSpace(()))  # "_test.{key}." 접두
    targets: dict[str, str] = field(default_factory=dict)
    weight: float = 1.0
    hints: dict[str, float] = field(default_factory=dict)
    compliance_info: dict[str, Any] = field(default_factory=dict)
    geometry_raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Prepared:
    spec: CalibrationSpec
    slots: list[SlotCtx]
    tests: list[TestCtx]
    space: ParamSpace  # 전역 θ 공간 (fixed_value 제외)
    initial: dict[str, float]
    has_plastic: bool
    warnings: list[dict[str, Any]]
    issues: list[Issue]
    hints: dict[str, float]
    precheck: dict[str, Any] | None = None


def _plugin_options(services: Any, plugin_type: str, name: str, inject: Mapping[str, Any]) -> dict[str, Any]:
    return dict(inject)


def testspec_options(cfg: Any, name: str) -> dict[str, Any]:
    """설정 → TestSpec 옵션 주입: `testspec.{name}.*`, 정렬 맞춤 키, solver 하중 경로 키 (이름은 하드코딩하지 않음)."""
    out: dict[str, Any] = {}
    prefix = f"testspec.{name}."
    for k in cfg.keys():
        if k.startswith(prefix) and "." not in k[len(prefix):]:
            out[k[len(prefix):]] = cfg.get(k)
    for k in ("toe_fit_lo", "toe_fit_hi", "elastic_dev_tol"):
        out[k] = cfg.get(f"alignment.{k}")
    out["t_end_s"] = cfg.get("solver.implicit.t_end_s")
    out["loading_velocity_m_s"] = cfg.get("solver.explicit.loading_velocity_m_s")
    return out


def _manifest_param_defaults(manifest: Mapping[str, Any]) -> dict[str, float]:
    out = {}
    for p in manifest.get("parameters") or []:
        if p.get("default") is not None:
            out[str(p["name"])] = float(p["default"])
    return out


def _resolve_params(manifest: Mapping[str, Any], run: RunRecord, recipe_params_si: Mapping[str, float],
                    friction: Mapping[str, Any]) -> dict[str, float]:
    """우선순위: 요청 recipe_params > MatNexus 시편 치수(matnexus_key) > 매니페스트 default (recipe.md §3.2)."""
    vals = _manifest_param_defaults(manifest)
    for p in manifest.get("parameters") or []:
        key = p.get("matnexus_key")
        if key and run.specimen_dimensions.get(key) is not None:
            vals[str(p["name"])] = float(run.specimen_dimensions[key])
    vals.update({k: float(v) for k, v in (recipe_params_si or {}).items()})
    if friction and friction.get("mode", "fixed") == "fixed" and friction.get("value") is not None and "fric" in vals:
        vals["fric"] = float(friction["value"])
    return vals


def _run_dims(test: ResolvedTest, run: RunRecord, params: Mapping[str, float]) -> dict[str, float]:
    g = dict(test.geometry or {})
    dims = {k: float(v) for k, v in run.specimen_dimensions.items() if isinstance(v, (int, float))}
    dims.update({k: float(v) for k, v in (g.get("dimensions") or {}).items() if v is not None})
    if "span" in params:
        dims["span"] = float(params["span"])
    if dims.get("total_length") is None and "span" in dims:
        dims["total_length"] = dims["span"] + 2.0 * float(params.get("overhang", 0.0))
    return dims


def _rel_close(a: Mapping[str, float], b: Mapping[str, float], tol: float) -> bool:
    for k in set(a) | set(b):
        va, vb = a.get(k), b.get(k)
        if va is None or vb is None:
            if va != vb:
                return False
            continue
        den = max(abs(va), abs(vb))
        if den > 0 and abs(va - vb) / den > tol:
            return False
    return True


def prepare_slots(spec: CalibrationSpec, services: Any, warnings: list[dict[str, Any]]) -> list[SlotCtx]:
    reg, cfg = services.registry, services.config
    out: list[SlotCtx] = []
    default_mode = job_option(spec, services, "plasticity_mode") or cfg.get("engine.default_plasticity_mode")
    dens = float(cfg.get("material.default_density_kg_m3"))
    for s in spec.materials.slots:
        s = dict(s)
        name = str(s["slot"])
        role = str(s.get("role", "identify"))
        ctx = {"plasticity_mode": s.get("plasticity_mode") or default_mode}
        mname = reg.resolve_name("material_model", str(s["material_model"]), ctx)
        entry = reg.enabled_entry("material_model", mname)
        opts = {k: cfg_get(cfg, v) for k, v in MATERIAL_CONFIG_OPTIONS.items()}
        opts = {k: v for k, v in opts.items() if v is not None}
        opts.update({k: v for k, v in entry.defaults.items()})
        opts.update(dict(s.get("options") or {}))
        ref, defaulted = neutral_from_any(s.get("reference"), dens)
        if defaulted:
            warnings.append(warn(E.DENSITY_DEFAULTED, f"밀도 미입력: 기본값 {dens} kg/m^3 사용", slot=name))
        model = reg.plugin_class("material_model", mname)(opts)
        model.bind(ref)
        out.append(SlotCtx(name=name, role=role, model_name=mname, model_version=entry.version, model=model,
                           options=opts, reference=ref, overrides={k: dict(v) for k, v in (s.get("params") or {}).items()},
                           member_position=s.get("member_position"), density_defaulted=defaulted))
    return out


def finalize_slot_spaces(slots: list[SlotCtx], hints: Mapping[str, float]) -> None:
    for sc in slots:
        if not sc.identify:
            continue
        sc.model.initial_guess(dict(hints))
        full, kept, fixed = apply_overrides(sc.model.param_space(), sc.overrides)
        sc.full_local, sc.space_local, sc.fixed_values = full, kept, fixed


def has_plastic_vars(slots: Sequence[SlotCtx]) -> bool:
    for sc in slots:
        if not sc.identify:
            continue
        _, kept, _ = apply_overrides(sc.model.param_space(), sc.overrides)
        if any(p.stage == "plastic" for p in kept.params):
            return True
    return False


def prepare_tests(spec: CalibrationSpec, services: Any, has_plastic: bool, warnings: list[dict[str, Any]],
                  issues: list[Issue]) -> list[TestCtx]:
    reg, cfg = services.registry, services.config
    tests: list[TestCtx] = []
    for t in spec.tests:
        sname = reg.resolve_name("test_spec", t.test_type)
        entry = reg.enabled_entry("test_spec", sname)
        opts = testspec_options(cfg, sname)
        opts.update(entry.defaults)
        ts = reg.plugin_class("test_spec", sname)(opts)
        manifest = dict(t.recipe.manifest or {})
        comp = manifest.get("compliance") or {}
        C = comp.get("value_m_per_n") if isinstance(comp, Mapping) else None
        C = float(C) if C is not None else None
        runs: list[RunCtx] = []
        datas: list[TestData] = []
        test_input = {"test_key": t.test_key, "test_type": t.test_type, "recipe": t.recipe.recipe_ref,
                      "geometry": dict(t.geometry or {}), "recipe_params": dict(t.recipe_params_si or {}),
                      "displacement_channel": t.displacement_channel}
        for iss in ts.validate(test_input, t.runs, manifest):
            ctx_ = {**dict(iss.context), "test_key": t.test_key}
            if iss.severity == "error":
                issues.append(replace(iss, context=ctx_))
            else:
                warnings.append(warn(iss.code, iss.message, test_key=t.test_key, run_id=ctx_.get("run_id")))
        for r in t.runs:
            try:
                d = ts.map_run(r, t.displacement_channel, C)
            except ValueError as e:
                code = str(e).split(":")[0] if ":" in str(e) else E.CHANNEL_MISSING
                issues.append(Issue(code=code if code in E.VALIDATION_CODES else E.CHANNEL_MISSING, message=str(e),
                                    severity="error", context={"test_key": t.test_key, "run_id": r.run_id}))
                continue
            datas.append(d)
            params = _resolve_params(manifest, r, t.recipe_params_si, spec.friction)
            runs.append(RunCtx(run=r, data=d, aligned={}, params_si=params, dims=_run_dims(t, r, params)))
        xq = datas[0].x_quantity if datas else "displacement"
        try:
            responses = ts.responses(manifest, xq)
        except ValueError as e:
            issues.append(Issue(code=E.DISPLACEMENT_CHANNEL_UNAVAILABLE, message=str(e), severity="error",
                                context={"test_key": t.test_key}))
            responses = ()
        qs = ts.analysis_mode({"test_key": t.test_key, "geometry": dict(t.geometry)}, datas) == "quasi_static"
        for rc in runs:
            for resp in responses:
                if resp.kind != "curve":
                    continue
                at = AL.prepare_test(rc.data.x, rc.data.y, resp, cfg, compliance_m_per_n=rc.data.compliance_m_per_n,
                                     user_window=t.user_window, has_plastic=has_plastic, quasi_static=qs)
                rc.aligned[resp.response_key] = at
                for i in at.issues:
                    issues.append(replace(i, context={**dict(i.context), "test_key": t.test_key, "run_id": rc.run.run_id}))
                for w in at.warnings:
                    warnings.append(warn(w.code, w.message, test_key=t.test_key, run_id=rc.run.run_id))
        # 컴플라이언스 정보 (result.md §4.6)
        src = datas[0].displacement_source if datas else "crosshead"
        cinfo: dict[str, Any] = {"test_key": t.test_key, "displacement_channel": xq, "applied": False,
                                 "value_m_per_n": C, "source": "recipe" if C is not None else None,
                                 "recipe_ref": t.recipe.recipe_ref, "method": comp.get("method") if comp else None,
                                 "calibrated_at": comp.get("calibrated_at") if comp else None, "reason": None, "warnings": []}
        if src == "deflectometer":
            cinfo["reason"] = "deflectometer"
        elif C is None:
            cinfo["reason"] = "missing"
            warnings.append(warn(E.COMPLIANCE_MISSING, "컴플라이언스 C 미교정: E 신뢰도 낮음", test_key=t.test_key))
        else:
            cinfo["applied"] = True
            instr = comp.get("instruments") if comp else None
            if instr:
                bad = [rc.run.run_id for rc in runs if rc.run.instrument not in instr]
                if bad:
                    cinfo["warnings"].append(E.COMPLIANCE_INSTRUMENT_MISMATCH)
                    warnings.append(warn(E.COMPLIANCE_INSTRUMENT_MISMATCH, f"장비 불일치 run {bad}", test_key=t.test_key))
        if any(w["code"] == E.COMPLIANCE_NONMONOTONIC and w.get("test_key") == t.test_key for w in warnings):
            cinfo["warnings"].append(E.COMPLIANCE_NONMONOTONIC)
        tc = TestCtx(key=t.test_key, test=t, spec=ts, spec_name=sname, spec_version=entry.version, manifest=manifest,
                     compliance=C, responses=tuple(responses), x_quantity=xq, quasi_static=qs, runs=runs,
                     weight=float(t.weight or 1.0), compliance_info=cinfo, geometry_raw=dict(t.geometry or {}))
        # R9 비재료 변수
        tspace = ts.param_space({"test_key": t.test_key, "geometry": dict(t.geometry)}, manifest)
        tc.space = tspace.qualify(f"_test.{t.test_key}")
        tc.targets = dict(ts.param_targets())
        tests.append(tc)
    return tests


def aggregate_hints(tests: Sequence[TestCtx], cfg: Any) -> dict[str, float]:
    e0, sy, ep = [], [], []
    for tc in tests:
        h = tc.hints
        if h.get("E0"):
            e0.append(h["E0"])
        if h.get("sigma_y0"):
            sy.append(h["sigma_y0"])
        if h.get("eps_p_max0"):
            ep.append(h["eps_p_max0"])
    out: dict[str, float] = {}
    if e0:
        out["E0"] = float(math.exp(np.mean(np.log(e0))))
    if sy:
        out["sigma_y0"] = float(np.mean(sy))
    if ep:
        out["eps_p_max0"] = float(np.max(ep))
    return out


def geometry_spec(tc: TestCtx, dims: Mapping[str, float], slots: Sequence[SlotCtx], services: Any,
                  for_rate: bool = False) -> GeometrySpec:
    g = tc.geometry_raw
    kind = str(g.get("kind", "box"))
    manifest = tc.manifest
    mslots = {str(m["slot"]): m for m in manifest.get("material_slots") or []}
    region_pid: dict[str, int] = {}
    layers: tuple[tuple[str, float], ...] = ()
    if kind == "layered_box":
        lay = list(g.get("layers") or [])
        layers = tuple((f"layer:{i}", float(l["thickness"])) for i, l in enumerate(lay))
        for i, l in enumerate(lay):
            sl = l.get("slot")
            if sl is None:
                for m in mslots.values():
                    if f"layer:{i}" in (m.get("regions") or []) or "*" in (m.get("regions") or []):
                        sl = m["slot"]
                        break
            if sl in mslots:
                region_pid[f"layer:{i}"] = int(mslots[sl]["part_id"])
    elif kind == "stp":
        for bs in g.get("body_slots") or []:
            sl = bs.get("slot")
            if sl in mslots:
                region_pid[f"body:{bs['body']}"] = int(mslots[sl]["part_id"])
        if not region_pid:
            for m in mslots.values():
                for reg_ in m.get("regions") or []:
                    region_pid[str(reg_)] = int(m["part_id"])
    else:
        for m in mslots.values():
            for reg_ in m.get("regions") or ["specimen"]:
                region_pid["specimen" if reg_ == "*" else str(reg_)] = int(m["part_id"])
    stp_path = None
    if kind == "stp":
        if g.get("stp_path"):
            stp_path = Path(g["stp_path"])
        elif g.get("file_id"):
            stp_path = Path(services.workdir("inputs", "stp")) / f"{g['file_id']}.stp"
    placement = dict((manifest.get("specimen") or {}).get("placement") or {})
    placement.update(dict(g.get("placement") or {}))
    gdims = {k: float(dims[k]) for k in ("width", "thickness", "total_length") if dims.get(k) is not None}
    return GeometrySpec(kind=kind, dimensions=gdims, layers=layers, stack_axis=g.get("stack_axis", "z") or "z",
                        stp_path=stp_path, placement=placement, region_part_ids=region_pid,
                        reference_points=tuple(tc.spec.reference_points(manifest)))


def build_cases(tc: TestCtx, cfg: Any) -> None:
    tol = float(cfg.get("cases.dimension_merge_rel_tol"))
    groups: list[list[RunCtx]] = []
    for rc in tc.runs:
        key = {**{k: v for k, v in rc.dims.items() if k in ("width", "thickness", "total_length", "span")}, **rc.params_si}
        for gr in groups:
            if all(_rel_close(key, {**{k: v for k, v in o.dims.items() if k in ("width", "thickness", "total_length", "span")},
                                    **o.params_si}, tol) for o in gr):
                gr.append(rc)
                break
        else:
            groups.append([rc])
    tc.cases = []
    for n, gr in enumerate(groups, start=1):
        cid = f"{tc.key}-c{n}"
        dims = {k: float(np.mean([r.dims[k] for r in gr if k in r.dims])) for k in
                set().union(*[set(r.dims) for r in gr])}
        params = {k: float(np.mean([r.params_si[k] for r in gr if k in r.params_si])) for k in
                  set().union(*[set(r.params_si) for r in gr])}
        for r in gr:
            r.case_id = cid
        xu = max((at.X_u for r in gr for at in r.aligned.values() if at.ok), default=0.0)
        dx = min((at.grid_dx_min for r in gr for at in r.aligned.values() if at.ok), default=math.inf)
        tc.cases.append(CaseCtx(case_id=cid, test_key=tc.key, run_ids=[r.run.run_id for r in gr], dims=dims,
                                params_si=dict(sorted(params.items())), geometry=GeometrySpec("box", {}), X_u=xu, dx_min=dx))


def mesh_options(manifest: Mapping[str, Any], kind: str, cfg: Any) -> MeshOptions:
    sp = manifest.get("specimen") or {}
    el = sp.get("element") or {}
    topo = el.get("stp_topology", "tet10") if kind == "stp" else el.get("topology", "hex8")
    sym = "none" if kind == "stp" else str((manifest.get("symmetry") or {}).get("type", "none"))
    idr = sp.get("id_ranges") or {}
    return MeshOptions(
        topology=topo, target_size_m=float(el.get("target_size_m", 0.0005)),
        min_elems_through_thickness=int(el.get("min_elems_through_thickness", 4)),
        max_elements=int(cfg.get("mesh.max_elements")), symmetry=sym,  # type: ignore[arg-type]
        node_id_range=tuple(idr.get("node", (1, 4_999_999))), element_id_range=tuple(idr.get("element", (1, 4_999_999))),
        part_set_id=int(sp.get("part_set_id", 0) or 0), node_set_ids=dict(sp.get("node_sets") or {}))


def mesh_case(tc: TestCtx, cc: CaseCtx, slots: Sequence[SlotCtx], services: Any, warnings: list[dict[str, Any]]) -> None:
    reg, cfg = services.registry, services.config
    cc.geometry = geometry_spec(tc, cc.dims, slots, services)
    kind = cc.geometry.kind
    if kind == "stp" and (tc.manifest.get("symmetry") or {}).get("type", "none") != "none":
        warnings.append(warn(E.SYMMETRY_IGNORED_STP, "STP 형상은 대칭을 적용하지 않음", test_key=tc.key))
    try:
        mesher = reg.create("geometry_mesher", kind)
        vi = [i for i in mesher.validate(cc.geometry) if i.severity == "error"]
        if vi:
            raise EngineFailure(E.MESH_FAILED, "; ".join(f"{i.code}: {i.message}" for i in vi))
        out = Path(services.workdir("cases", cc.case_id, "mesh"))
        cc.mesh = mesher.mesh(cc.geometry, mesh_options(tc.manifest, kind, cfg), out)
    except EngineFailure:
        raise
    except Exception as e:
        raise EngineFailure(E.MESH_FAILED, f"specimen mesh failed for {cc.case_id}: {e!r}") from e
    for w in cc.mesh.warnings:
        warnings.append(warn(w.code, w.message, test_key=tc.key))
    jig = tc.manifest.get("jig")
    if tc.manifest.get("kind", "parametric") == "parametric" and jig:
        try:
            gen = str(jig["generator"])
            jm = reg.create("geometry_mesher", gen)
            pmap = dict(jig.get("params") or {})
            params_si: dict[str, float] = {}
            for k, v in pmap.items():
                if v == "auto":
                    if k == "roller_length":
                        params_si[k] = float(cc.dims.get("width", 0.0)) + 2.0 * float(
                            ((tc.manifest.get("specimen") or {}).get("element") or {}).get("target_size_m", 0.0005))
                    continue
                if isinstance(v, str) and v in cc.params_si:
                    params_si[k] = float(cc.params_si[v])
                elif isinstance(v, (int, float)):
                    params_si[k] = float(v)
            rigid = dict(jig.get("rigid") or {})
            rigid.setdefault("E", float(cfg.get("mesh.jig.rigid_E_pa")))
            rigid.setdefault("poisson", float(cfg.get("mesh.jig.rigid_poisson")))
            rigid.setdefault("density", float(cfg.get("mesh.jig.rigid_density_kg_m3")))
            idr = jig.get("id_ranges") or {}
            js = JigSpec(generator=gen, params_si=params_si, part_ids=dict(jig.get("part_ids") or {}),
                         mids=dict(jig.get("mids") or {}), rigid=rigid, specimen=cc.mesh, symmetry=cc.mesh.symmetry,
                         node_id_range=tuple(idr.get("node", (5_000_000, 5_999_999))),
                         element_id_range=tuple(idr.get("element", (5_000_000, 5_999_999))))
            cc.jig = jm.mesh(js, None, Path(services.workdir("cases", cc.case_id, "mesh")))
        except EngineFailure:
            raise
        except Exception as e:
            raise EngineFailure(E.MESH_FAILED, f"jig mesh failed for {cc.case_id}: {e!r}") from e


def slot_bindings(manifest: Mapping[str, Any], slot_names: Sequence[str]) -> tuple[SlotBinding, ...]:
    out = []
    for m in manifest.get("material_slots") or []:
        if m["slot"] not in slot_names:
            continue
        pids = m.get("part_ids") or [m["part_id"]]
        out.append(SlotBinding(slot=str(m["slot"]), part_ids=tuple(int(p) for p in pids), mid=int(m.get("mid", 0)),
                               secid=int(m.get("secid", 0)), section=dict(m.get("section") or {}),
                               lcid=(int(m["lcid"]) if m.get("lcid") is not None else None)))
    return tuple(out)


def _history_nodes(responses: Sequence[ResponseDef], mesh: MeshResult, jig: JigResult | None) -> tuple[int, ...]:
    refs = dict(mesh.reference_nodes)
    if jig is not None:
        refs.update(jig.reference_nodes)
    ids: set[int] = set()
    for r in responses:
        for ex in r.extractor.values():
            for key in ("node",):
                nd = ex.args.get(key)
                if isinstance(nd, Mapping):
                    if "id" in nd:
                        ids.add(int(nd["id"]))
                    elif nd.get("ref") in refs:
                        ids.add(int(refs[nd["ref"]]))
            for nd in ex.args.get("ref_nodes") or []:
                if isinstance(nd, Mapping):
                    if "id" in nd:
                        ids.add(int(nd["id"]))
                    elif nd.get("ref") in refs:
                        ids.add(int(refs[nd["ref"]]))
    return tuple(sorted(ids))


def engine_params_for(tc: TestCtx, cc: CaseCtx, mode: str, cfg: Any, *, velocity: float | None = None) -> dict[str, float]:
    assert cc.mesh is not None
    ep = dict(tc.spec.engine_params({"X_u": cc.X_u, "x_max": cc.X_u}, mode, cc.mesh))
    if velocity is not None:
        ep["mo_tend"] = ep["mo_dend"] / velocity
    axis = tc.responses[0].axis if tc.responses else "displacement"
    ep.update(AL.output_intervals(cc.dx_min, ep["mo_dend"], ep["mo_tend"], cfg, axis=axis or "displacement"))
    return {k: float(v) for k, v in ep.items()}


def build_sim_case(tc: TestCtx, cc: CaseCtx, slot_names: Sequence[str], cfg: Any) -> SimulationCase:
    assert cc.mesh is not None
    an = (tc.manifest.get("solver") or {}).get("analysis") or {}
    primary = an.get("primary", "implicit")
    fallback = an.get("fallback", "none")
    modes = [primary] + ([fallback] if fallback not in (None, "none", primary) else [])
    eng = {m: engine_params_for(tc, cc, m, cfg) for m in modes}
    p = eng[primary]
    out = OutputRequest(dt_out_s=p["mo_dtout"], dt_plot_s=p["mo_dtplt"], dt_max_s=p["mo_dtmax"],
                        history_nodes=_history_nodes(tc.responses, cc.mesh, cc.jig))
    return SimulationCase(case_id=cc.case_id, test_key=tc.key, recipe_ref=tc.test.recipe.recipe_ref,
                          recipe_dir=Path(tc.test.recipe.store_dir), manifest=tc.manifest, params_si=dict(cc.params_si),
                          engine_params_si=eng, specimen_mesh=cc.mesh, jig_mesh=cc.jig,
                          slots=slot_bindings(tc.manifest, slot_names), responses=tc.responses,
                          analysis_primary=primary, analysis_fallback=fallback, output=out)


def hint_data(tc: TestCtx) -> list[TestData]:
    """정렬 결과(1–4단계)를 담은 TestData — TestSpec.initial_hints 입력."""
    out = []
    for rc in tc.runs:
        at = next((a for a in rc.aligned.values() if a.ok), None)
        if at is None or at.k0 is None:
            continue
        d = rc.data
        out.append(TestData(run_id=d.run_id, x_quantity=d.x_quantity, x=at.x, y=at.y,
                            displacement_source=d.displacement_source, compliance_m_per_n=None, scalars=dict(d.scalars),
                            dimensions={**dict(d.dimensions), **{k: v for k, v in rc.dims.items()}},
                            meta={**dict(d.meta), "k0": at.k0, "x_elastic_end": at.x_e, "X_u": at.X_u, "aligned": True}))
    return out


def preprocess(spec: CalibrationSpec, services: Any, *, with_meshes: bool = True) -> Prepared:
    cfg = services.config
    warnings: list[dict[str, Any]] = []
    issues: list[Issue] = []
    slots = prepare_slots(spec, services, warnings)
    has_plastic = has_plastic_vars(slots)
    tests = prepare_tests(spec, services, has_plastic, warnings, issues)
    for tc in tests:
        build_cases(tc, cfg)
        dims0 = tc.cases[0].dims if tc.cases else {}
        params0 = tc.cases[0].params_si if tc.cases else {}
        gs = geometry_spec(tc, dims0, slots, services)
        try:
            tc.hints = {k: float(v) for k, v in tc.spec.initial_hints(hint_data(tc), gs, params0).items()}
        except Exception:
            tc.hints = {}
    hints = aggregate_hints(tests, cfg)
    finalize_slot_spaces(slots, hints)
    # 전역 θ 공간 (R2·R9)
    params: list[ParamSpec] = []
    cons = []
    fixed_all: dict[str, float] = {}
    for sc in slots:
        if not sc.identify:
            continue
        q = sc.space_local.qualify(sc.name)
        params.extend(q.params)
        cons.extend(q.constraints)
        fixed_all.update({f"{sc.name}.{k}": v for k, v in sc.fixed_values.items()})
    for tc in tests:
        params.extend(tc.space.params)
        cons.extend(tc.space.constraints)
    space = ParamSpace(tuple(params), tuple(cons))
    initial = {p.name: center_value(p) for p in space.params}
    prep = Prepared(spec=spec, slots=slots, tests=tests, space=space, initial=initial, has_plastic=has_plastic,
                    warnings=warnings, issues=issues, hints=hints)
    if with_meshes and not [i for i in issues if i.severity == "error"]:
        names = [s.name for s in slots]
        for tc in tests:
            for cc in tc.cases:
                mesh_case(tc, cc, slots, services, warnings)
                cc.sim_case = build_sim_case(tc, cc, names, cfg)
    return prep
