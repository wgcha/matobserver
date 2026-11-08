"""`run_calibration()` 진입점 (module-boundaries.md §4.1)과 잡 생성 검증용 `preflight()`.

단계: preprocess → elastic(R8: 탄성 전용 카드) → plastic(E 고정) → (그 밖 단계 태그) → diagnostics → finalize(왕복 검사)
→ explicit_check(옵션) → 결과 조립(result.md). 예산·정체·시간·취소 판정의 주체는 엔진(plugins.md §4.1).

계약 해석(반환 보고에 기록):
* 예산 배분: plastic 단계가 있으면 elastic 단계 상한 = ceil(0.3·N)(최소 1), 나머지는 이후 단계. 진단·왕복·explicit 검사는 예산 밖.
* 정체: 단계 최적 J의 상대 개선이 최근 k·(d+1) 평가(d = 단계 변수 수) 동안 `rel_tol` 미만이면 종료 — 차분 기반 최적화기의
  한 반복 ≈ d+1 평가이므로 「연속 k회」를 반복 단위로 본다.
* 초기 설계점 전부 실패 판정: 첫 단계의 처음 min(3, 예산) 평가가 모두 곡선 없이 실패.
* 잔차 벡터를 `EvalOutcome.components["residual:NNNNNNN"]`로 최적화기에 넘긴다(lsq Gauss–Newton용, 다른 최적화기는 무시).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np

from matobserver.core import errors as E
from matobserver.core.errors import EngineFailure
from matobserver.core.types import CalibrationSpec, Issue, ParamSpace

from . import identifiability as IDF
from .context import Prepared, preprocess
from .evaluate import CalibrationCancelled, EvalRecord, Evaluator
from .finalize import finalize

STAGE_ORDER = ("elastic", "plastic")


@dataclass
class StageResult:
    stage: str
    names: tuple[str, ...]
    space: ParamSpace
    best: EvalRecord | None
    evaluations: int
    failed: int
    reason: str
    best_J_history: list[float] = field(default_factory=list)
    optimizer: tuple[str, str] = ("", "")  # (name, version)


def _budget(spec: CalibrationSpec, cfg: Any) -> tuple[int, float, float]:
    b = dict(spec.budget or {})
    n = int(b.get("max_evaluations") or cfg.get("budget.default_max_evaluations"))
    wall = float(b.get("max_wall_time_s") or cfg.get("budget.default_max_wall_time_s"))
    tmo = float(b.get("run_timeout_s") or cfg.get("solver.run_timeout_s"))
    return max(1, n), wall, tmo


def stages_of(space: ParamSpace) -> list[str]:
    seen = [s for s in STAGE_ORDER if any(p.stage == s for p in space.params)]
    for p in space.params:
        if p.stage not in seen:
            seen.append(p.stage)
    return seen


def _subset(space: ParamSpace, stage: str) -> ParamSpace:
    kept = tuple(p for p in space.params if p.stage == stage)
    names = {p.name for p in kept}
    return ParamSpace(kept, tuple(c for c in space.constraints if all(n in names for n in c.params)))


def _better(a: EvalRecord, b: EvalRecord | None) -> bool:
    if b is None:
        return True
    return (not a.valid, a.J) < (not b.valid, b.J)


def run_stage(stage: str, space: ParamSpace, opt_spec: tuple[str, Mapping[str, Any]], base_theta: dict[str, float],
              cap: int, ev: Evaluator, services: Any, spec: CalibrationSpec, t_start: float, wall: float,
              first_stage: bool, total_offset: int, total_budget: int) -> StageResult:
    cfg, reg = services.config, services.registry
    oname = reg.resolve_name("optimizer", opt_spec[0])
    oentry = reg.enabled_entry("optimizer", oname)
    opt = reg.create("optimizer", oname, dict(opt_spec[1] or {}))
    names = space.names()
    opt.setup(space, int(spec.seed), [{n: float(base_theta[n]) for n in names}])
    # R8: plastic 변수가 있을 때만 탄성 전용 카드. 소성 변수가 전부 고정(또는 없음)이면 고정 카드 전체(탄소성 포함)로 E 피팅
    elastic_only = stage == "elastic" and ev.prep.has_plastic
    k = int(cfg.get("engine.stagnation.k"))
    rel_tol = float(cfg.get("engine.stagnation.rel_tol"))
    window = max(1, k * (len(names) + 1))
    n_check = max(1, min(3, cap))
    abort_cfg = bool(cfg.get("engine.abort_if_initial_all_failed"))
    best: EvalRecord | None = None
    hist: list[float] = []
    n = failed = 0
    reason = "budget"
    first_failed = 0
    while True:
        if n >= cap:
            reason = "budget"
            break
        if time.monotonic() - t_start >= wall:
            reason = "wall_time"
            break
        sig = opt.suggest_stop()
        if sig.stop:
            reason = "converged"
            break
        th = opt.ask(1)[0]
        rec = ev.evaluate({**base_theta, **th}, stage, elastic_only=elastic_only)
        n += 1
        is_best = _better(rec, best)
        if is_best:
            best = rec
        ev.finish(rec, is_best=is_best)
        opt.tell([th], [rec.outcome()])
        if rec.failed:
            failed += 1
            if n <= n_check:
                first_failed += 1
        if first_stage and abort_cfg and n == n_check and first_failed == n_check:
            raise EngineFailure(E.ALL_INITIAL_RUNS_FAILED, f"first {n_check} evaluations all failed")
        hist.append(best.J if best is not None else math.inf)
        services.progress(stage, stage_iter=n, iter=total_offset + n, max_evaluations=total_budget,
                          evaluations_failed=failed, best_J_stage=float(best.J) if best else None,
                          best_J=float(best.J) if best else None, elapsed_s=time.monotonic() - t_start)
        if n > window:
            old = hist[-window - 1]
            if math.isfinite(old) and old > 0 and (old - hist[-1]) / old < rel_tol:
                reason = "stagnation"
                break
    if first_stage and abort_cfg and 0 < n <= n_check and first_failed == n:
        raise EngineFailure(E.ALL_INITIAL_RUNS_FAILED, f"all {n} initial evaluations failed")
    return StageResult(stage=stage, names=names, space=space, best=best, evaluations=n, failed=failed, reason=reason,
                       best_J_history=hist, optimizer=(oname, oentry.version))


def _diagnostics(prep: Prepared, ev: Evaluator, stage_results: list[StageResult], final_theta: dict[str, float],
                 services: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int]:
    cfg = services.config
    if not bool(cfg.get("identifiability.post_fit")):
        return [], [], [], {}, 0
    need = sum(len(sr.names) for sr in stage_results if sr.best is not None)
    if need > int(cfg.get("identifiability.max_extra_evals")):
        return [], [], [{"code": E.DIAGNOSTICS_SKIPPED, "message": f"차분 평가 {need}회 > identifiability.max_extra_evals"}], {}, 0
    h = float(cfg.get("identifiability.fd_step_rel"))
    post, flags, warns, raw = [], [], [], {}
    recs: list[dict[str, Any]] = []
    used = 0
    for sr in stage_results:
        if sr.best is None or not sr.names:
            continue
        services.progress("diagnostics", stage_iter=used)
        base = {**final_theta}
        base.update({n: sr.best.theta[n] for n in sr.names})
        r0 = sr.best.residual
        S = np.zeros((len(r0), len(sr.names)))
        for j, (name, p) in enumerate(zip(sr.names, sr.space.params)):
            v = float(base[name])
            if v > 0:
                v2 = v * (1.0 + h)
                if v2 > p.upper:
                    v2 = v * (1.0 - h)
                dl = math.log(v2 / v)
            else:
                step = h * (p.upper - p.lower)
                v2 = min(v + step, p.upper)
                dl = (v2 - v) / max(step, 1e-300) * h  # 0 근처: 범위 기준 상대 스텝(h·range)을 ln 스텝 h로 간주
            th = {**base, name: v2}
            rec = ev.evaluate(th, sr.stage, elastic_only=(sr.stage == "elastic" and prep.has_plastic),
                              history_stage="diagnostics")
            ev.finish(rec, is_best=False)
            used += 1
            if len(rec.residual) == len(r0) and dl != 0:
                S[:, j] = (rec.residual - r0) / dl
        entry, fl, rc = IDF.posterior(list(sr.names), S, r0, cfg, stage=sr.stage, evaluations=len(sr.names))
        post.append(entry)
        flags.extend(fl)
        recs.extend(rc)
        raw[sr.stage] = {"params": list(sr.names), "S": S.tolist(), "residual_norm": float(np.linalg.norm(r0))}
    raw["recommendations"] = recs
    return post, flags, warns, raw, used


def run_calibration(spec: CalibrationSpec, services: Any) -> Mapping[str, Any]:
    t_start = time.monotonic()
    created_at = datetime.now(timezone.utc).isoformat()
    cfg = services.config
    services.progress("preprocess")
    prep = preprocess(spec, services)
    errs = [i for i in prep.issues if i.severity == "error"]
    if errs:
        raise EngineFailure(E.INTERNAL_ERROR, "preprocess failed: " + "; ".join(f"{i.code}: {i.message}" for i in errs))
    n_budget, wall, tmo = _budget(spec, cfg)
    ev = Evaluator(prep, services, spec.solver[0], spec.solver[1], tmo)
    prep.precheck = _precheck(prep, cfg)
    if E.IDENTIFIABILITY_LOW_DATA in prep.precheck["codes"]:
        prep.warnings.append({"code": E.IDENTIFIABILITY_LOW_DATA, "message": "소성 변수 대비 데이터 점 부족"})
    stages = stages_of(prep.space)
    theta = dict(prep.initial)
    results: list[StageResult] = []
    used = 0
    for si, st in enumerate(stages):
        sub = _subset(prep.space, st)
        remaining = n_budget - used
        if remaining <= 0:
            results.append(StageResult(st, sub.names(), sub, None, 0, 0, "budget"))
            continue
        later = len(stages) - si - 1
        cap = remaining if later == 0 else max(1, min(remaining - later, int(math.ceil(0.3 * n_budget))))
        opt_spec = _stage_optimizer(spec, cfg, st)
        services.progress(st)
        sr = run_stage(st, sub, opt_spec, theta, cap, ev, services, spec, t_start, wall, si == 0, used, n_budget)
        used += sr.evaluations
        results.append(sr)
        if sr.best is None or not sr.best.valid:
            code = E.WALL_TIME_EXCEEDED_NO_VALID if sr.reason == "wall_time" else E.BUDGET_EXHAUSTED_NO_VALID
            raise EngineFailure(code, f"stage {st}: no valid evaluation")
        theta.update({n: sr.best.theta[n] for n in sr.names})
    # 최종 평가 기록: 마지막 단계 최적점. 탄성 전용 카드로만 평가됐는데 테이블 카드가 있으면 전체 카드로 1회 더(예산 밖).
    final_rec = results[-1].best if results else None
    needs_full = any(m.table is not None for m in _final_materials(prep, theta).values())
    if final_rec is None or (results and results[-1].stage == "elastic" and needs_full and prep.has_plastic) \
            or not results:
        final_rec = ev.evaluate(theta, "final", elastic_only=False, history_stage="diagnostics")
        ev.finish(final_rec, is_best=True)
        if not final_rec.valid:
            raise EngineFailure(E.BUDGET_EXHAUSTED_NO_VALID, "final evaluation failed")
    services.progress("diagnostics")
    post, flags, dwarn, diag_raw, n_diag = _diagnostics(prep, ev, results, theta, services)
    services.progress("finalize")
    return finalize(prep=prep, ev=ev, services=services, results=results, theta=theta, final_rec=final_rec,
                    post_fit=post, flags=flags, diag_warnings=dwarn, diag_raw=diag_raw, n_budget=n_budget,
                    t_start=t_start, wall=wall, created_at=created_at)


def _stage_optimizer(spec: CalibrationSpec, cfg: Any, stage: str) -> tuple[str, Mapping[str, Any]]:
    """요청 최적화기(이름이 있으면) → (1.1) engine.stages.{stage}.optimizer → (1.0 별칭) engine.{stage}_stage.optimizer → lsq."""
    from .context import cfg_get

    req = spec.elastic_optimizer if stage == "elastic" else spec.optimizer
    if req and req[0]:
        return req[0], dict(req[1] or {})
    key = "elastic" if stage == "elastic" else "plastic"
    try:
        new_in_table = f"engine.stages.{key}.optimizer" in cfg
    except Exception:
        new_in_table = False
    name = cfg_get(cfg, f"engine.stages.{key}.optimizer") if new_in_table else None
    if not name:
        name = cfg_get(cfg, f"engine.{key}_stage.optimizer") or cfg_get(cfg, f"engine.stages.{key}.optimizer") or "lsq"
    return str(name), {}


def _final_materials(prep: Prepared, theta: Mapping[str, float]):
    from .evaluate import materials_for

    return materials_for(prep, theta, elastic_only=False)


def _precheck(prep: Prepared, cfg: Any) -> dict[str, Any]:
    n_el = sum(1 for p in prep.space.params if p.stage == "elastic" and not p.name.startswith("_test."))
    n_pl = sum(1 for p in prep.space.params if p.stage == "plastic" and not p.name.startswith("_test."))
    sigs = []
    n_pts = 0
    for tc in prep.tests:
        params = tc.cases[0].params_si if tc.cases else {}
        g = dict(tc.geometry_raw)
        dims = tc.cases[0].dims if tc.cases else {}
        g["dimensions"] = {k: dims.get(k) for k in ("width", "thickness", "total_length") if dims.get(k) is not None}
        sigs.append(IDF.config_signature({"recipe_ref": tc.test.recipe.recipe_ref, "params_si": params, "geometry": g}))
        for rc in tc.runs:
            for at in rc.aligned.values():
                if at.ok:
                    n_pts += at.n_plastic
    return IDF.precheck(n_el, n_pl, sigs, n_pts, cfg)


def preflight(spec_draft: CalibrationSpec, services: Any) -> list[Issue]:
    """잡 생성 검증(api.md §4.7 ⑤⑥⑩)용: 시험측 곡선 정렬 1–5단계 + TestSpec.validate + 식별성 사전 점검. 메시·해석 없음."""
    cfg = services.config
    try:
        prep = preprocess(spec_draft, services, with_meshes=False)
    except EngineFailure as e:
        return [Issue(code=E.SCHEMA_INVALID, message=str(e), severity="error")]
    issues = list(prep.issues)
    for w in prep.warnings:
        ctx = {k: w[k] for k in ("test_key", "run_id", "slot") if k in w}
        issues.append(Issue(code=w["code"], message=w["message"], severity="warning", context=ctx))
    if any(i.severity == "error" for i in issues):
        return issues
    pc = _precheck(prep, cfg)
    mode = cfg.get("identifiability.precheck_mode")
    if pc["verdict"] == "underdetermined":
        sev = "error" if mode == "reject" else "warning"
        issues.append(Issue(code=E.IDENTIFIABILITY_UNDERDETERMINED, severity=sev,
                            message=f"탄성 식별 변수 {pc['n_params']['elastic']}개 > 서로 다른 시험 구성 {pc['n_configs']}개",
                            context={"precheck": pc}))
    if E.IDENTIFIABILITY_LOW_DATA in pc["codes"]:
        issues.append(Issue(code=E.IDENTIFIABILITY_LOW_DATA, severity="warning",
                            message="소성 변수 대비 데이터 점 부족", context={"precheck": pc}))
    return issues


__all__ = ["run_calibration", "preflight", "CalibrationCancelled"]
