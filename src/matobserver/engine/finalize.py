"""결과 확정 단계: 슬롯 카드(result.md §2·§3), 왕복 검사(§4.9), explicit 재현 검사(§4.12), 메타데이터(§4), artifact(§6)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

import matobserver
from matobserver.core import errors as E
from matobserver.core.hashing import sha256_digest, sha256_json
from matobserver.core.types import NeutralMaterial, RunOverrides, RunState, TableRow, TestData

from . import alignment as AL
from . import explicit_check as XC
from . import roundtrip as RT
from .context import Prepared, SlotCtx, TestCtx, engine_params_for, geometry_spec, warn
from .evaluate import EvalRecord, Evaluator, materials_for, split_theta

CONFIG_HASH_PREFIXES = ("alignment.", "objective.", "engine.", "identifiability.", "material.", "result.", "cases.",
                        "testspec.", "solver.implicit.", "solver.explicit.")
CONFIG_HASH_KEYS = ("solver.d3plot_frames",)

ASSUMPTION_TEXT = {
    "J2_ISOTROPIC_HARDENING": "J2 등방경화",
    "RATE_INDEPENDENT": "변형률속도 무관(근거: metadata.rate.reference_strain_rate_1_s)",
    "TEMPERATURE_INDEPENDENT": "온도 무관",
    "POISSON_RATIO_INPUT": "푸아송비는 입력값(식별하지 않음)",
    "NO_DAMAGE": "파단·손상 미포함",
    "ISOTROPIC_EQUIVALENT": "등가 등방성(모드 A)",
    "RIGID_JIG": "지그 강체",
    "COMPLIANCE_FROM_RECIPE": "컴플라이언스는 레시피 교정값",
    "ELASTIC_ONLY": "탄성 계수만 식별(취성 재료)",
    "FRICTION_FIXED": "지그–시편 마찰 고정",
}

MEDIA = {".json": "application/json", ".csv": "text/csv", ".k": "text/plain", ".zip": "application/zip"}


def clean(obj: Any) -> Any:
    """JSON 직렬화 가능한 순수 파이썬 값(NaN·inf → None)."""
    if isinstance(obj, Mapping):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [clean(v) for v in obj.tolist()]
    if isinstance(obj, (np.floating,)):
        obj = float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


# ======================================================================================
# artifact
# ======================================================================================
class Artifacts:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.root = Path(services.workdir("artifacts"))
        self.refs: list[dict[str, Any]] = []

    def add(self, path: Path, kind: str, *, slot: str | None = None, test_key: str | None = None) -> str | None:
        try:
            aid = self.services.add_artifact(path, kind, slot=slot, test_key=test_key)
        except Exception:
            return None
        data = Path(path).read_bytes()
        self.refs.append({"artifact_id": aid, "kind": kind, "media_type": MEDIA.get(Path(path).suffix, "application/octet-stream"),
                          "filename": Path(path).name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "slot": slot, "test_key": test_key,
                          "created_at": datetime.now(timezone.utc).isoformat()})
        return aid

    def write_text(self, name: str, text: str) -> Path:
        p = self.root / name
        p.write_text(text, encoding="utf-8")
        return p

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write_text(name, json.dumps(clean(obj), sort_keys=True, indent=1, ensure_ascii=False))

    def write_csv(self, name: str, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> Path:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None or (isinstance(v, float) and not math.isfinite(v)) else
                        (repr(float(v)) if isinstance(v, (float, np.floating)) else v) for v in r])
        return self.write_text(name, buf.getvalue())

    def zip_dir(self, name: str, src: Path, exclude_suffixes: Sequence[str] = ()) -> Path | None:
        src = Path(src)
        if not src.is_dir():
            return None
        p = self.root / name
        with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(x for x in src.rglob("*") if x.is_file()):
                if any(f.name.endswith(s) for s in exclude_suffixes):
                    continue
                zi = zipfile.ZipInfo(f.relative_to(src).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, f.read_bytes())
        return p


# ======================================================================================
# 카드·유효 범위
# ======================================================================================
def valid_ranges(prep: Prepared, rec: EvalRecord) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for co in rec.cases.values():
        if co.extracted is None:
            continue
        for slot, fm in co.extracted.field_max.items():
            v = fm.get("eff_plastic_strain")
            if v is None:
                continue
            cur = out.setdefault(slot, {"max_plastic_strain": 0.0, "source": "field_max", "test_keys": []})
            cur["max_plastic_strain"] = max(cur["max_plastic_strain"], float(v))
            if co.test_key not in cur["test_keys"]:
                cur["test_keys"].append(co.test_key)
    return out


def _valid_range_record(prep: Prepared, ev: Evaluator, theta: Mapping[str, float], rec: EvalRecord, cfg: Any) -> EvalRecord:
    """유효 범위(result.md §2 valid_range)는 비교 구간 끝 X_u 시점의 field_max 여야 한다. 해석은 overtravel 포함
    mo_dend까지 가므로, 최적 θ를 mo_dend = X_u로 1회 더 풀어(예산 밖, history stage=diagnostics) 그 field_max 를 쓴다."""
    if not any(m.table is not None for m in rec.materials.values()):
        return rec
    from dataclasses import replace as _r

    saved = {}
    try:
        for tc in prep.tests:
            for cc in tc.cases:
                sc = cc.sim_case
                if sc is None or cc.X_u <= 0:
                    continue
                saved[cc.case_id] = (cc, sc)
                eng = {}
                for mode, ep in sc.engine_params_si.items():
                    ep = dict(ep)
                    f = cc.X_u / float(ep["mo_dend"])
                    ep["mo_dend"] = float(cc.X_u)
                    if mode == "explicit":
                        ep["mo_tend"] = float(ep["mo_tend"]) * f
                    ep.update(AL.output_intervals(cc.dx_min, ep["mo_dend"], ep["mo_tend"], cfg))
                    eng[mode] = ep
                cc.sim_case = _r(sc, engine_params_si=eng)
        if not saved:
            return rec
        vr_rec = ev.evaluate(theta, "final", elastic_only=False, history_stage="diagnostics")
        ev.finish(vr_rec, is_best=False)
        ok = all(c.extracted is not None and c.extracted.field_max for c in vr_rec.cases.values())
        return vr_rec if ok else rec
    finally:
        for cc, sc in saved.values():
            cc.sim_case = sc


def _is_coeff(sc: SlotCtx, local: Mapping[str, float]) -> bool:
    try:
        return (sc.model.describe(local).get("hardening") or {}).get("law") is not None
    except Exception:
        return False


def result_card(sc: SlotCtx, local: Mapping[str, float], eps_valid: float | None, services: Any,
                warnings: list[dict[str, Any]]) -> tuple[NeutralMaterial, dict[str, Any] | None]:
    """result.md §3.1·§3.2 테이블 규칙 적용 카드와 extrapolation 객체."""
    cfg = services.config
    base = sc.model.to_neutral(local)
    if base.table is None:
        return base, None
    method = str(cfg.get("result.extrapolation_method"))
    ext_to = cfg.get("result.extrapolate_to_strain")
    first = float(cfg.get("result.table_first_step"))
    ev = float(eps_valid) if eps_valid is not None else float(base.table[-1].plastic_strain)
    coeff = _is_coeff(sc, local)
    if coeff:
        s_max = max(ev, 2.0 * first)
        opts = dict(sc.options)
        opts.update({"max_strain": s_max, "table_points": int(cfg.get("result.table_points")),
                     "table_spacing": cfg.get("result.table_spacing"), "table_first_step": first})
        m = type(sc.model)(opts)
        m.bind(sc.reference)
        m.initial_guess(dict(getattr(sc.model, "_hints", {}) or {}))
        card = m.to_neutral(local)
        rows = list(card.table or ())
        first_ex = None
        if ext_to is not None and float(ext_to) > s_max:
            first_ex = len(rows)
            ext_pts = np.linspace(s_max, float(ext_to), 6)[1:]
            if method == "hardening_law":
                opts2 = dict(opts, max_strain=float(ext_to))
                m2 = type(sc.model)(opts2)
                m2.bind(sc.reference)
                full = m2.stress(ext_pts, local) if hasattr(m2, "stress") else None
            else:
                full = None
            if full is None:
                e1, e2 = rows[-2].plastic_strain, rows[-1].plastic_strain
                slope = (rows[-1].true_stress - rows[-2].true_stress) / (e2 - e1)
                full = rows[-1].true_stress + slope * (ext_pts - e2)
            last = rows[-1].true_stress
            for e, s in zip(ext_pts, full):
                last = max(last, float(s))
                rows.append(TableRow(float(e), last))
        card = NeutralMaterial(elastic=card.elastic, table=tuple(rows))
        extrap = {"method": method, "beyond_plastic_strain": ev, "rows_extrapolated_from_index": first_ex}
        return card, extrap
    # 테이블 모드: 노드 그대로, εp_valid 밖 노드는 외삽 표시 + 경고
    rows = list(base.table)
    beyond = [i for i, r in enumerate(rows) if r.plastic_strain > ev * (1 + 1e-9)]
    first_ex = beyond[0] if beyond else None
    if beyond:
        warnings.append(warn(E.TABLE_NODES_BEYOND_VALID_RANGE,
                             f"테이블 노드 {len(beyond)}개가 유효 범위 εp ≤ {ev:.4g} 밖(식별 근거 약함)", slot=sc.name))
    if ext_to is not None and float(ext_to) > rows[-1].plastic_strain:
        e1, e2 = rows[-2].plastic_strain, rows[-1].plastic_strain
        slope = (rows[-1].true_stress - rows[-2].true_stress) / (e2 - e1)
        first_ex = first_ex if first_ex is not None else len(rows)
        for e in np.linspace(e2, float(ext_to), 6)[1:]:
            rows.append(TableRow(float(e), float(rows[-1].true_stress + slope * (e - rows[-1].plastic_strain))))
    card = NeutralMaterial(elastic=base.elastic, table=tuple(rows))
    return card, {"method": "last_slope", "beyond_plastic_strain": ev, "rows_extrapolated_from_index": first_ex}


def _at_bound(p, v: float, tol: float) -> bool:
    if p.scale == "log" and p.lower > 0 and v > 0:
        lo, hi, x = math.log(p.lower), math.log(p.upper), math.log(v)
    else:
        lo, hi, x = p.lower, p.upper, v
    if hi <= lo:
        return False
    u = (x - lo) / (hi - lo)
    return u <= tol or u >= 1.0 - tol


def _param_result(p, v: float, tol: float) -> dict[str, Any]:
    return {"value": float(v), "unit": p.unit, "stage": p.stage, "lower": float(p.lower), "upper": float(p.upper),
            "initial": (float(p.initial) if p.initial is not None else None), "at_bound": _at_bound(p, float(v), tol)}


# ======================================================================================
# 메인
# ======================================================================================
def finalize(*, prep: Prepared, ev: Evaluator, services: Any, results: list, theta: dict[str, float],
             final_rec: EvalRecord, post_fit: list, flags: list, diag_warnings: list, diag_raw: dict, n_budget: int,
             t_start: float, wall: float, created_at: str) -> dict[str, Any]:
    cfg, reg = services.config, services.registry
    spec = prep.spec
    warnings: list[dict[str, Any]] = list(prep.warnings) + list(diag_warnings)
    arts = Artifacts(services)
    tol_b = float(cfg.get("result.at_bound_tol"))
    locals_, test_theta = split_theta(prep, theta)

    # ---- best 실행 폴더 사본 ----
    best_dirs: dict[str, Path] = {}
    for cid, co in final_rec.cases.items():
        dst = Path(services.workdir("best", cid))
        if co.workdir.is_dir():
            shutil.copytree(co.workdir, dst, dirs_exist_ok=True)
        best_dirs[cid] = dst

    # ---- 슬롯 결과 ----
    vr = valid_ranges(prep, _valid_range_record(prep, ev, theta, final_rec, cfg))
    slots_out: list[dict[str, Any]] = []
    final_cards: dict[str, NeutralMaterial] = {}
    for sc in prep.slots:
        caps = sc.model.capabilities()
        if sc.identify:
            local = locals_[sc.name]
            rng = vr.get(sc.name)
            base = sc.model.to_neutral(local)
            card, extrap = result_card(sc, local, rng["max_plastic_strain"] if (rng and base.table is not None) else None,
                                       services, warnings)
            identified = {}
            for p in sc.space_local.params:
                pr = _param_result(p, local[p.name], tol_b)
                identified[p.name] = pr
                if pr["at_bound"]:
                    warnings.append(warn(E.PARAM_AT_BOUND, f"{sc.name}.{p.name} 가 경계 근처", slot=sc.name, param=p.name))
            fixed = {k: {"value": float(v), "source": "request.params"} for k, v in sc.fixed_values.items()}
            desc = dict(sc.model.describe(local))
            hard = desc.get("hardening") or {"law": None, "coefficients": None}
        else:
            card = sc.reference
            assert card is not None
            rng = vr.get(sc.name) if card.table is not None else None
            extrap = None
            identified = {}
            fixed = {}
            hard = {"law": None, "coefficients": None}
        src_ref = "request.reference" if sc.reference is not None else "config.default"
        fixed["poisson_ratio"] = {"value": float(card.elastic.poisson_ratio), "source": src_ref}
        fixed["density"] = {"value": float(card.elastic.density),
                            "source": "config.default" if sc.density_defaulted else src_ref}
        final_cards[sc.name] = card
        kw = caps.solver_keyword.get("lsdyna")
        if card.table is None and kw == "MAT_024":
            kw = "MAT_001"
        slots_out.append({
            "slot": sc.name, "role": sc.role, "member_position": sc.member_position,
            "material_model": {"name": sc.model_name, "version": sc.model_version,
                               "options": clean(sc.model.resolved_options() if hasattr(sc.model, "resolved_options")
                                                else sc.options)},
            "regularization": _regularization(sc, locals_.get(sc.name)),
            "card_blocks": card.to_card_blocks(),
            "identified_params": identified, "fixed_params": fixed,
            "valid_range": (clean(rng) if (rng is not None and card.table is not None) else None),
            "extrapolation": extrap, "hardening": clean(hard), "derived": {"lsdyna_keyword": kw},
        })

    # ---- 왕복 검사 (§4.9) ----
    rt_enabled = bool(cfg.get("verify.roundtrip_enabled"))
    rt = {"enabled": rt_enabled, "per_slot": []}
    if rt_enabled:
        tol = float(cfg.get("verify.roundtrip_tol"))
        for so in slots_out:
            card = final_cards[so["slot"]]
            if card.table is None:
                continue
            smax = cfg.get("verify.roundtrip_strain")
            if smax is None:
                smax = (so["valid_range"] or {}).get("max_plastic_strain") or card.table[-1].plastic_strain
            smax = min(float(smax), float(card.table[-1].plastic_strain)) if smax else float(card.table[-1].plastic_strain)
            if smax <= 0:
                smax = float(card.table[-1].plastic_strain)
            wd = Path(services.workdir("roundtrip", so["slot"]))
            services.progress("finalize", roundtrip_slot=so["slot"])
            res = dict(RT.check(so["slot"], card, ev.adapter, wd, smax, tol, timeout_s=ev.timeout))
            zp = arts.zip_dir(f"roundtrip_{so['slot']}.zip", wd)
            if zp is not None:
                res["artifact_id"] = arts.add(zp, "roundtrip", slot=so["slot"])
            rt["per_slot"].append(res)
            if not res.get("passed"):
                warnings.append(warn(E.ROUNDTRIP_FAILED, f"왕복 검사 실패(max_rel_err={res.get('max_rel_err')})",
                                     slot=so["slot"]))

    # ---- explicit 재현 검사 (§4.12) ----
    xc = _explicit_check(prep, ev, services, final_rec, final_cards, arts, warnings)

    # ---- 경고: 해석 상태 ----
    ke_max = float(cfg.get("solver.explicit.max_ke_ratio"))
    for co in final_rec.cases.values():
        if co.status is not None and co.status.fallback_used:
            warnings.append(warn(E.FALLBACK_EXPLICIT_USED, f"{co.case_id}: implicit 실패 → explicit 사용", test_key=co.test_key))
        if co.extracted is not None and co.extracted.energy.get("ke_ie_ratio_max") is not None:
            if float(co.extracted.energy["ke_ie_ratio_max"]) > ke_max:
                warnings.append(warn(E.KE_RATIO_HIGH, f"{co.case_id}: KE/IE {co.extracted.energy['ke_ie_ratio_max']:.3g}",
                                     test_key=co.test_key))
    elapsed = time.monotonic() - t_start
    if any(r.reason == "wall_time" for r in results):
        warnings.append(warn(E.WALL_TIME_REACHED, "budget.max_wall_time_s 도달: 그때까지의 최적점 사용"))
    for f in flags:
        warnings.append(warn(f["code"], f"{f['stage']}: {', '.join(f['params'])} ({f['value']:.3g})",
                             param=",".join(f["params"])))

    # ---- metadata ----
    rate = _rate(prep, warnings)
    meta = {
        "test_types": sorted({tc.spec_name for tc in prep.tests}),
        "matnexus_test_type_keys": sorted({k for tc in prep.tests for k in tc.spec.matnexus_test_type_keys}),
        "tests": [_test_summary(tc, test_theta.get(tc.key, {}), tol_b) for tc in prep.tests],
        "assumptions": _assumptions(prep, final_cards),
        "residuals": _residuals(results, final_rec),
        "uncertainty": _uncertainty(prep, theta, final_rec),
        "identifiability": {"precheck": prep.precheck, "post_fit": post_fit, "flags": flags,
                            "recommendations": diag_raw.get("recommendations", [])},
        "compliance": [tc.compliance_info for tc in prep.tests],
        "equivalence": _equivalence(prep),
        "termination": _termination(results, elapsed, prep),
        "roundtrip": rt,
        "warnings": [],
        "traceability": _traceability(prep, ev, results, services),
        "explicit_check": xc,
        "rate": rate,
    }
    if not post_fit and not any(w["code"] == E.DIAGNOSTICS_SKIPPED for w in warnings) and results:
        if not bool(cfg.get("identifiability.post_fit")):
            warnings.append(warn(E.DIAGNOSTICS_SKIPPED, "identifiability.post_fit=false"))

    # ---- artifact ----
    for so in slots_out:
        p = arts.write_json(f"{so['slot']}_card_blocks.json", so["card_blocks"])
        arts.add(p, "card_blocks_json", slot=so["slot"])
        _render_card(arts, ev, prep, so, final_cards[so["slot"]], warnings)
    for tc in prep.tests:
        for cc in tc.cases:
            co = final_rec.cases.get(cc.case_id)
            if co is not None and co.extracted is not None:
                for rk, cv in co.extracted.curves.items():
                    p = arts.write_csv(f"curve_raw_{cc.case_id}_{rk}.csv", ["x_m", "f_N"],
                                       list(zip(np.asarray(cv.x).tolist(), np.asarray(cv.y).tolist())))
                    arts.add(p, "curve_raw", test_key=tc.key)
            zp = arts.zip_dir(f"deck_best_{cc.case_id}.zip", best_dirs.get(cc.case_id, Path("/nonexistent")))
            if zp is not None:
                arts.add(zp, "deck_best", test_key=tc.key)
            if cc.mesh is not None and Path(cc.mesh.mesh_path).is_file():
                dst = arts.root / f"mesh_{cc.case_id}.k"
                shutil.copyfile(cc.mesh.mesh_path, dst)
                arts.add(dst, "mesh", test_key=tc.key)
        for i, rc in enumerate(tc.runs):
            for resp in tc.responses:
                pair = final_rec.pairs.get((rc.run.run_id, resp.response_key))
                if pair is None:
                    continue
                rows = [(float(x), float(fe), (float(fs) if math.isfinite(fs) else None),
                         (float(fs - fe) if math.isfinite(fs) else None), str(rg))
                        for x, fe, fs, rg in zip(pair.x, pair.f_exp, pair.f_sim, pair.region)]
                p = arts.write_csv(f"curve_compare_{tc.key}_r{i}_{resp.response_key}.csv",
                                   ["x_m", "f_exp_N", "f_sim_N", "residual_N", "region"], rows)
                arts.add(p, "curve_compare", test_key=tc.key)
    names = list(prep.space.names())
    hist_rows = [[h["eval_index"], h["stage"], h["J"], h["status"]] + [h["theta"].get(n) for n in names] for h in ev.history]
    arts.add(arts.write_csv("history.csv", ["eval_index", "stage", "J", "status"] + names, hist_rows), "history")
    conv = {r.stage: {"best_J": [float(v) for v in r.best_J_history], "evaluations": r.evaluations} for r in results}
    arts.add(arts.write_json("convergence.json", conv), "convergence_plot_data")
    arts.add(arts.write_json("identifiability.json", {"post_fit": post_fit, "flags": flags, "raw": diag_raw,
                                                      "precheck": prep.precheck}), "identifiability_json")

    meta["warnings"] = _dedupe(warnings)
    result = {
        "schema_version": "1.0",
        "job_id": spec.job_id,
        "status": "succeeded",
        "created_at": created_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "units": {"length": "m", "force": "N", "stress": "Pa", "strain": "1", "density": "kg/m^3", "time": "s",
                  "compliance": "m/N"},
        "slots": slots_out,
        "metadata": meta,
        "artifacts": arts.refs,
    }
    return clean(result)


def _render_card(arts: Artifacts, ev: Evaluator, prep: Prepared, so: Mapping[str, Any], card: NeutralMaterial,
                 warnings: list[dict[str, Any]]) -> None:
    """(1.1, result.md §5) card_lsdyna = 결과 NeutralMaterial(절단·외삽 반영)을 SolverAdapter.render_card로 재렌더링.
    어댑터 card_format이 None이면 생략. 최적점 실행 폴더 카드 복사 금지."""
    fmt = getattr(ev.adapter.capabilities(), "card_format", None)
    render = getattr(ev.adapter, "render_card", None)
    if fmt is None or render is None:
        return
    from .context import slot_bindings

    binding = None
    for tc in prep.tests:
        bs = slot_bindings(tc.manifest, [so["slot"]])
        if bs:
            binding = bs[0]
            break
    if binding is None:
        return
    opts = {"title": f"matobserver {prep.spec.job_id} slot {so['slot']}",
            "header_comments": [f"job_id={prep.spec.job_id}", f"slot={so['slot']}",
                                f"material_model={so['material_model']['name']}@{so['material_model']['version']}",
                                "source=result card_blocks (valid-range truncated)", "units=mm-N-s-tonne"]}
    try:
        text = render(card, binding, opts)
    except NotImplementedError:
        return
    except Exception:  # 2차 산출물 실패는 결과 확정을 막지 않는다(카드 없이 진행)
        return
    ext = ".k" if fmt == "lsdyna_k" else ".txt"
    p = arts.write_text(f"{so['slot']}_card{ext}", text)
    arts.add(p, "card_lsdyna" if fmt == "lsdyna_k" else f"card_{fmt}", slot=so["slot"])


def _regularization(sc: SlotCtx, local: Mapping[str, float] | None) -> dict[str, Any] | None:
    """result.md §2 (1.2) 슬롯 regularization: {kind, weight, J_reg} — 정칙화 없는 모델·식별 안 하는 슬롯은 null."""
    fn = getattr(sc.model, "regularization_residuals", None)
    if not sc.identify or fn is None or local is None:
        return None
    w = float(getattr(sc.model, "smoothing_weight", 0.0))
    if w <= 0:
        return None
    rr = np.asarray(fn(local), dtype=float)
    return {"kind": "adjacent_slope", "weight": w, "J_reg": float(np.dot(rr, rr))}


def _dedupe(ws: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for w in ws:
        key = json.dumps(clean(w), sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def _test_summary(tc: TestCtx, tparams: Mapping[str, float], tol_b: float) -> dict[str, Any]:
    cc0 = tc.cases[0] if tc.cases else None
    ats = [at for rc in tc.runs for at in rc.aligned.values() if at.ok]
    idp = {}
    for p in tc.space.params:
        local = p.name[len(f"_test.{tc.key}."):]
        if local in tparams:
            idp[local] = _param_result(p, tparams[local], tol_b)
    g = cc0.geometry if cc0 is not None else None
    return {
        "test_key": tc.key, "test_type": tc.spec_name, "recipe_ref": tc.test.recipe.recipe_ref,
        "recipe_params": dict(cc0.params_si) if cc0 else {},
        "geometry": {"kind": tc.geometry_raw.get("kind"), "dimensions": dict(g.dimensions) if g else {},
                     **({"layers": [{"region": r, "thickness": t} for r, t in g.layers]} if g and g.layers else {}),
                     **({"stack_axis": g.stack_axis} if g and g.kind == "layered_box" else {})},
        "displacement_channel": tc.x_quantity,
        "runs": [{"run_id": rc.run.run_id, "specimen_id": rc.run.specimen_id, "case_id": rc.case_id,
                  "source_sha256": rc.run.source_sha256, "conditions": dict(rc.run.conditions)} for rc in tc.runs],
        "cases": [{"case_id": c.case_id, "run_ids": c.run_ids,
                   "element_count": (c.mesh.element_count if c.mesh else None),
                   "symmetry": (c.mesh.symmetry if c.mesh else None)} for c in tc.cases],
        "window": {"axis": tc.x_quantity, "x_min": min((a.X_l for a in ats), default=None),
                   "x_max": max((a.X_u for a in ats), default=None),
                   "x_elastic_end": (float(np.mean([a.x_e for a in ats])) if ats else None)},
        "identified_params": idp,
        "weight": tc.weight,
    }


def _assumptions(prep: Prepared, cards: Mapping[str, NeutralMaterial]) -> list[dict[str, str]]:
    codes = ["J2_ISOTROPIC_HARDENING", "RATE_INDEPENDENT", "TEMPERATURE_INDEPENDENT", "POISSON_RATIO_INPUT", "NO_DAMAGE"]
    if str(prep.spec.materials.mode).upper() == "A":
        codes.append("ISOTROPIC_EQUIVALENT")
    if any(tc.manifest.get("kind", "parametric") == "parametric" for tc in prep.tests):
        codes.append("RIGID_JIG")
    if any(tc.compliance_info.get("applied") for tc in prep.tests):
        codes.append("COMPLIANCE_FROM_RECIPE")
    if all(c.table is None for c in cards.values()):
        codes.append("ELASTIC_ONLY")
    if any("fric" in (tc.cases[0].params_si if tc.cases else {}) for tc in prep.tests):
        codes.append("FRICTION_FIXED")
    return [{"code": c, "text": ASSUMPTION_TEXT[c]} for c in codes]


def _residuals(results: list, rec: EvalRecord) -> dict[str, Any]:
    per_run: dict[str, dict[str, Any]] = {}
    for m in rec.per_run:
        cur = per_run.setdefault(m["run_id"], {"run_id": m["run_id"], "test_key": m["test_key"], "_J": [], "rmse_N": None,
                                               "max_abs_err_N": None, "nrmse": None, "shortfall": 0.0})
        cur["_J"].append(m["J"])
        if cur["rmse_N"] is None:
            cur.update({"rmse_N": m["rmse_N"], "max_abs_err_N": m["max_abs_err_N"], "nrmse": m["nrmse"]})
        cur["shortfall"] = max(cur["shortfall"], m["shortfall"])
    pr = []
    for v in per_run.values():
        v["J"] = float(np.mean(v.pop("_J")))
        pr.append(v)
    return {"J": float(rec.J),
            "J_stage": {r.stage: (float(r.best.J) if r.best is not None else None) for r in results},
            "per_test": rec.per_test, "per_run": pr}


def _uncertainty(prep: Prepared, theta: Mapping[str, float], rec: EvalRecord) -> dict[str, Any]:
    n_runs = sum(len(tc.runs) for tc in prep.tests)
    ident = [sc for sc in prep.slots if sc.identify]
    mode_b = str(prep.spec.materials.mode).upper() == "B" or len(ident) != 1
    out: dict[str, Any] = {"method": "per_specimen_scaling" if n_runs > 1 else "none", "per_run": [], "statistics": {},
                           "notes": []}
    if mode_b:
        out["notes"].append("MODE_B_SCALING_NA")
    sc = ident[0] if ident else None
    Ekey = f"{sc.name}.E" if sc else None
    Estar = None
    if sc is not None:
        Estar = theta.get(Ekey, sc.fixed_values.get("E"))
    vals = []
    for tc in prep.tests:
        for rc in tc.runs:
            E_s = None
            Jr = [m["J"] for m in rec.per_run if m["run_id"] == rc.run.run_id]
            if not mode_b and Estar is not None:
                for rk, at in rc.aligned.items():
                    pair = rec.pairs.get((rc.run.run_id, rk))
                    if pair is not None and at.k0 and pair.k0_sim:
                        E_s = float(Estar) * float(at.k0) / float(pair.k0_sim)
                        break
            if E_s is not None:
                vals.append(E_s)
            out["per_run"].append({"run_id": rc.run.run_id, "specimen_id": rc.run.specimen_id,
                                   "J": (float(np.mean(Jr)) if Jr else None), "E_scaled": E_s, "sigma_y_scaled": None})
    if len(vals) >= 2 and Ekey is not None:
        m = float(np.mean(vals))
        sd = float(np.std(vals, ddof=1))
        out["statistics"][Ekey] = {"mean": m, "stdev": sd, "cv": (sd / m if m else None), "n": len(vals)}
    return out


def _equivalence(prep: Prepared) -> dict[str, Any]:
    confs = []
    for tc in prep.tests:
        cc = tc.cases[0] if tc.cases else None
        regions = []
        if cc is not None and cc.mesh is not None:
            pid2slot = {int(m["part_id"]): m["slot"] for m in tc.manifest.get("material_slots") or []}
            for rname, info in sorted(cc.mesh.regions.items()):
                pid = int(info.get("part_id", 0))
                regions.append({"region": rname, "slot": pid2slot.get(pid), "part_id": pid,
                                "volume_m3": float(info.get("volume_m3", 0.0))})
        g = tc.geometry_raw
        confs.append({"test_key": tc.key, "geometry_kind": g.get("kind"),
                      "stack_axis": (g.get("stack_axis", "z") if g.get("kind") == "layered_box" else None),
                      "regions": regions, "span_m": (cc.params_si.get("span") if cc else None),
                      "orientation": "load_along_thickness"})
    lm = [getattr(tc.spec, "load_mode", None) for tc in prep.tests]
    return {"mode": str(prep.spec.materials.mode), "load_mode": lm[0] if len(set(lm)) == 1 else lm,
            "configurations": confs, "note": "등가 물성은 하중 모드·형상에 의존한다"}


def _termination(results: list, elapsed: float, prep: Prepared | None = None) -> dict[str, Any]:
    per = {}
    for r in results:
        per[r.stage] = {"reason": r.reason, "evaluations": r.evaluations,
                        "best_eval_index": (r.best.eval_index if r.best is not None else None)}
        if r.stage == "joint":
            per[r.stage]["accepted"] = bool(r.accepted)
    by = {r.stage: r for r in results}
    final_stage = "elastic"
    if "plastic" in by and by["plastic"].best is not None:
        final_stage = "plastic"
    if "joint" in by and by["joint"].accepted:
        final_stage = "joint"
    main = [r for r in results if r.stage != "joint" or r.accepted]
    reason = (by[final_stage].reason if final_stage in by else (results[-1].reason if results else "converged"))
    e_by_stage = []
    if prep is not None:
        for sc in prep.slots:
            key = f"{sc.name}.E"
            def _e(st: str):
                r = by.get(st)
                return float(r.best.theta[key]) if (r is not None and r.best is not None and key in r.best.theta) else None
            e1 = _e("elastic")
            if e1 is None:
                continue
            e3 = _e("joint") if ("joint" in by) else None
            e_by_stage.append({"slot": sc.name, "elastic": e1, "plastic": (_e("plastic") or e1) if "plastic" in by else None,
                               "joint": e3, "rel_change_joint": ((e3 - e1) / e1 if e3 is not None else None)})
    return {"reason": reason, "evaluations": int(sum(r.evaluations for r in results)),
            "evaluations_failed": int(sum(r.failed for r in results)), "wall_time_s": float(elapsed), "per_stage": per,
            "final_stage": final_stage, "e_by_stage": e_by_stage}


def _rate(prep: Prepared, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    per_run, per_test, allv = [], [], []
    for tc in prep.tests:
        vals = []
        for rc in tc.runs:
            cc = next((c for c in tc.cases if c.case_id == rc.case_id), None)
            dims = dict(rc.dims)
            gs = cc.geometry if cc is not None else None
            if gs is not None and "thickness" not in gs.dimensions and cc.mesh is not None:
                from dataclasses import replace as _r
                gs = _r(gs, dimensions={**dict(gs.dimensions), "load_height": cc.mesh.top_z_at_load_m - cc.mesh.bottom_z_m})
            if gs is None:
                from matobserver.core.types import GeometrySpec
                gs = GeometrySpec(kind=str(tc.geometry_raw.get("kind", "box")), dimensions=dims)
            info = tc.spec.reference_strain_rate(rc.data, gs, rc.params_si)
            per_run.append({"run_id": rc.run.run_id, "value": info.value_1_s, "velocity_source": info.velocity_source,
                            "formula": info.formula, "inputs": dict(info.inputs)})
            if info.value_1_s is None:
                warnings.append(warn(E.REFERENCE_RATE_UNKNOWN, "기준 변형률속도 계산 불가(test_speed·time 채널 없음)",
                                     test_key=tc.key, run_id=rc.run.run_id))
            else:
                vals.append(float(info.value_1_s))
                allv.append(float(info.value_1_s))
        if vals:
            per_test.append({"test_key": tc.key, "median": float(np.median(vals)), "min": float(np.min(vals)),
                             "max": float(np.max(vals))})
        else:
            per_test.append({"test_key": tc.key, "median": None, "min": None, "max": None})
    msg = ("속도 의존을 식별하지 않았다. 기준 변형률속도보다 크게 빠른 동적(낙하·충격) 해석에 쓰면 응력을 과소평가할 수 있다")
    warnings.append(warn(E.RATE_DEPENDENCE_NOT_IDENTIFIED, msg))
    return {"reference_strain_rate_1_s": {"per_run": per_run, "per_test": per_test,
                                          "overall_range": ([min(allv), max(allv)] if allv else None)},
            "rate_dependence_identified": False, "rate_model": None, "rate_coefficients": None,
            "identified_rate_range_1_s": None,
            "advisory": {"code": E.RATE_DEPENDENCE_NOT_IDENTIFIED, "message": msg}}


def config_hash(cfg: Any) -> str:
    sub = {k: cfg.get(k) for k in sorted(cfg.keys())
           if k.startswith(CONFIG_HASH_PREFIXES) or k in CONFIG_HASH_KEYS}
    return sha256_json(sub)


def _traceability(prep: Prepared, ev: Evaluator, results: list, services: Any) -> dict[str, Any]:
    cfg, reg = services.config, services.registry
    spec = prep.spec
    run_hashes = {rc.run.run_id: rc.run.fetched_hash for tc in prep.tests for rc in tc.runs}
    recipes = []
    for tc in prep.tests:
        r = {"recipe_ref": tc.test.recipe.recipe_ref, "content_hash": tc.test.recipe.content_hash}
        if r not in recipes:
            recipes.append(r)
    stp = []
    for tc in prep.tests:
        for cc in tc.cases:
            p = cc.geometry.stp_path
            if p is not None and Path(p).is_file():
                stp.append(sha256_digest(Path(p).read_bytes()))
    ch = config_hash(cfg)
    resolved = sha256_json({sc.name: clean(sc.model.resolved_options() if hasattr(sc.model, "resolved_options")
                                           else sc.options) for sc in prep.slots})
    parts = [spec.request_hash, resolved] + [run_hashes[k] for k in sorted(run_hashes)] + sorted(r["content_hash"] for r in recipes) \
        + sorted(stp) + [ch]
    input_hash = sha256_digest("\n".join(str(p) for p in parts).encode("utf-8"))
    sname = ev.solver_name
    plugins: dict[str, dict[str, str]] = {"optimizer": {}, "material_model": {}, "geometry_mesher": {}, "test_spec": {},
                                          "solver": {sname: ev.solver_entry.version}}
    for r in results:
        if r.optimizer[0]:
            plugins["optimizer"][r.optimizer[0]] = r.optimizer[1]
    for sc in prep.slots:
        plugins["material_model"][sc.model_name] = sc.model_version
    for tc in prep.tests:
        plugins["test_spec"][tc.spec_name] = tc.spec_version
        kinds = {str(tc.geometry_raw.get("kind", "box"))}
        jig = tc.manifest.get("jig")
        if tc.manifest.get("kind", "parametric") == "parametric" and jig:
            kinds.add(str(jig["generator"]))
        for k in kinds:
            try:
                plugins["geometry_mesher"][k] = reg.entry("geometry_mesher", k).version
            except Exception:
                pass
    sv = ev.solver_versions[-1] if ev.solver_versions else ev.adapter.solver_version()

    def opt(key: str) -> Any:
        k = f"solver.{sname}.{key}"
        return cfg.get(k) if k in cfg else None

    return {
        "input_hash": input_hash,
        "request_hash": spec.request_hash,
        "run_data_hashes": run_hashes,
        "recipes": recipes,
        "solver": {"name": sname, "adapter_version": ev.solver_entry.version, "solver_version": sv,
                   "precision": opt("precision"), "parallel": opt("mode"), "ncpu": opt("ncpu")},
        "plugins": plugins,
        "matobserver_version": getattr(matobserver, "__version__", "0.1.0"),
        "config_hash": ch,
        "seed": int(spec.seed),
        "matnexus": {"source_sha256": {rc.run.run_id: rc.run.source_sha256 for tc in prep.tests for rc in tc.runs},
                     "curve_max_points": cfg.get("matnexus.curve_max_points")},
    }


def _explicit_check(prep: Prepared, ev: Evaluator, services: Any, rec: EvalRecord, cards: Mapping[str, NeutralMaterial],
                    arts: Artifacts, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = services.config
    # (1.1) 우선순위: CalibrationSpec.explicit_check → services.job_options["explicit_check"] → 설정 verify.explicit_check.*
    from .context import job_option

    req = dict(job_option(prep.spec, services, "explicit_check") or {})
    enabled = bool(req.get("enabled", cfg.get("verify.explicit_check.enabled")))
    if not enabled:
        return {"enabled": False}
    sec_default = dict(req.get("section") or {})
    elform = int(sec_default.get("elform", cfg.get("verify.explicit_check.elform")))
    hg = dict(sec_default.get("hourglass") or {})
    ihq = int(hg.get("ihq", cfg.get("verify.explicit_check.hourglass.ihq")))
    qm = float(hg.get("qm", cfg.get("verify.explicit_check.hourglass.qm")))
    v = float(req.get("loading_velocity") or cfg.get("verify.explicit_check.loading_velocity_m_s"))
    max_ke = float(req.get("max_ke_ratio") or cfg.get("verify.explicit_check.max_ke_ratio"))
    settings = {"elform": elform, "hourglass": {"ihq": ihq, "qm": qm}, "loading_velocity_m_s": v, "max_ke_ratio": max_ke,
                "max_region_error": float(cfg.get("verify.explicit_check.max_region_error")),
                "max_peak_rel_diff": float(cfg.get("verify.explicit_check.max_peak_rel_diff")), "mass_scaling": False}
    targets = req.get("tests")
    per_case = []
    xwarn: list[dict[str, Any]] = []
    tmo = cfg.get("verify.explicit_check.run_timeout_s") or ev.timeout
    for tc in prep.tests:
        if targets and tc.key not in targets:
            continue
        for cc in tc.cases:
            services.progress("explicit_check", case_id=cc.case_id)
            slot_sec = {}
            for sc in prep.slots:
                s = dict((req.get("slots") or {}).get(sc.name) or {})
                slot_sec[sc.name] = {"elform": int(s.get("elform", elform)),
                                     "hourglass": {"ihq": int((s.get("hourglass") or {}).get("ihq", ihq)),
                                                   "qm": float((s.get("hourglass") or {}).get("qm", qm))}}
            ep = engine_params_for(tc, cc, "explicit", cfg, velocity=v)
            ov = RunOverrides(section_by_slot=slot_sec, forbid_mass_scaling=True, engine_params_si=ep)
            imp = rec.cases[cc.case_id].extracted
            exp = [rc.data for rc in tc.runs if rc.case_id == cc.case_id]
            wd = Path(services.workdir("explicit_check", cc.case_id))
            sink: dict[str, Any] = {}
            try:
                res = dict(XC.run(cc.sim_case, cards, imp, exp, ev.adapter, ov, wd, cfg, ncpu=ev.ncpu, timeout_s=float(tmo),
                                  cancel=services.cancel, user_window=tc.test.user_window, has_plastic=prep.has_plastic,
                                  sink=sink))
            except Exception as e:
                res = {"test_key": tc.key, "case_id": cc.case_id, "status": "failed", "run_state": "error",
                       "ke_ie_ratio_max": None, "vs_implicit": None, "vs_experiment": None, "within_tolerance": False,
                       "artifact_ids": {}, "skip_reason": None, "error": f"{type(e).__name__}: {e}"}
            ids = {}
            if res["status"] != "skipped":
                zp = arts.zip_dir(f"explicit_check_{cc.case_id}.zip", wd, exclude_suffixes=("d3plot",))
                if zp is not None:
                    ids["explicit_check"] = arts.add(zp, "explicit_check", test_key=tc.key)
                ids_c = _xc_curves(arts, tc, cc, rec, ev, sink.get("extracted"))
                if ids_c:
                    ids["explicit_check_curves"] = ids_c
            res["artifact_ids"] = ids
            per_case.append(res)
            if res["status"] == "deviation":
                xwarn.append(warn(E.EXPLICIT_CHECK_DEVIATION, f"{cc.case_id}: implicit 대비 차이 임계값 초과", test_key=tc.key))
            elif res["status"] == "failed":
                xwarn.append(warn(E.EXPLICIT_CHECK_FAILED, f"{cc.case_id}: explicit 검사 실행 실패", test_key=tc.key))
            elif res["status"] == "skipped":
                xwarn.append(warn(E.EXPLICIT_CHECK_SKIPPED, f"{cc.case_id}: {res.get('skip_reason')}", test_key=tc.key))
            if res.get("ke_ie_ratio_max") is not None and res["ke_ie_ratio_max"] > max_ke:
                xwarn.append(warn(E.EXPLICIT_CHECK_KE_HIGH, f"{cc.case_id}: KE/IE {res['ke_ie_ratio_max']:.3g} > {max_ke}",
                                  test_key=tc.key))
    warnings.extend(xwarn)
    return {"enabled": True, "settings": settings, "per_case": per_case, "warnings": [w["code"] for w in xwarn]}


def _xc_curves(arts: Artifacts, tc: TestCtx, cc, rec: EvalRecord, ev: Evaluator, ext) -> str | None:
    """explicit_check_curves CSV: x_m, f_exp_N, f_implicit_N, f_explicit_N, region (첫 run·첫 곡선 응답 격자)."""
    if ext is None:
        return None
    rc = next((r for r in tc.runs if r.case_id == cc.case_id), None)
    resp = next((r for r in tc.responses if r.kind == "curve"), None)
    if rc is None or resp is None or resp.response_key not in rc.aligned:
        return None
    at = rc.aligned[resp.response_key]
    imp_ext = rec.cases[cc.case_id].extracted
    imp = imp_ext.curves.get(resp.response_key) if imp_ext is not None else None
    pi = AL.align_sim(at, imp, ev.cfg)
    px = AL.align_sim(at, ext.curves.get(resp.response_key), ev.cfg)
    rows = [(float(x), float(fe), _f(fi), _f(fx), str(rg)) for x, fe, fi, fx, rg in
            zip(at.grid, at.f_exp, pi.f_sim, px.f_sim, at.region)]
    p = arts.write_csv(f"explicit_check_curves_{cc.case_id}.csv",
                       ["x_m", "f_exp_N", "f_implicit_N", "f_explicit_N", "region"], rows)
    return arts.add(p, "explicit_check_curves", test_key=tc.key)


def _f(v: float) -> float | None:
    return float(v) if math.isfinite(v) else None
