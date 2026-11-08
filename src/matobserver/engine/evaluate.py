"""평가 1회 = 모든 케이스 해석 1회씩(순차) → 정렬 → J (testspec.md §8, §9.1). θ 분할 R4·R5·R8·R9."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from matobserver.core import errors as E
from matobserver.core.errors import EngineFailure, MatObserverError
from matobserver.core.types import EvalOutcome, Extracted, NeutralMaterial, PreparedRun, RunState, RunStatus

from . import alignment as AL
from . import objective as OBJ
from .context import Prepared, SlotCtx

RESIDUAL_PREFIX = "residual:"


class CalibrationCancelled(MatObserverError):
    """CancelToken 이 설정되어 식별을 중단함 (워커가 잡을 cancelled 로 바꾼다)."""


@dataclass
class CaseOutcome:
    case_id: str
    test_key: str
    prepared: PreparedRun | None
    status: RunStatus | None
    extracted: Extracted | None
    workdir: Path
    error: str | None = None

    @property
    def state(self) -> str:
        if self.status is not None:
            return self.status.state.value
        return "error"


@dataclass
class EvalRecord:
    eval_index: int
    stage: str
    theta: dict[str, float]
    J: float
    penalized: bool
    failed: bool  # 어떤 케이스에서도 곡선을 얻지 못함
    valid: bool  # 모든 케이스 곡선 확보
    components: dict[str, Any]
    residual: np.ndarray
    per_test: list[dict[str, Any]]
    per_run: list[dict[str, Any]]
    pairs: dict[tuple[str, str], AL.AlignedPair]  # (run_id, response_key) → 정렬 쌍
    cases: dict[str, CaseOutcome]
    materials: dict[str, NeutralMaterial]
    wall_time_s: float
    started_at: str
    is_best: bool = False
    reg_residual: np.ndarray = field(default_factory=lambda: np.array([]))

    def outcome(self) -> EvalOutcome:
        comp: dict[str, float] = {}
        for i, v in enumerate(np.concatenate([self.residual, self.reg_residual])):
            comp[f"{RESIDUAL_PREFIX}{i:07d}"] = float(v)
        return EvalOutcome(J=float(self.J), penalized=bool(self.penalized), components=comp)


def split_theta(prep: Prepared, theta: Mapping[str, float]) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """θ → (슬롯별 θ_local(고정값 포함), 시험별 비재료 θ)."""
    slots: dict[str, dict[str, float]] = {}
    for sc in prep.slots:
        if not sc.identify:
            continue
        local = {p.name: float(p.initial if p.initial is not None else 0.0) for p in sc.full_local.params}
        local.update(sc.fixed_values)
        pre = sc.name + "."
        for k, v in theta.items():
            if k.startswith(pre):
                local[k[len(pre):]] = float(v)
        slots[sc.name] = local
    tests: dict[str, dict[str, float]] = {}
    for tc in prep.tests:
        pre = f"_test.{tc.key}."
        tests[tc.key] = {k[len(pre):]: float(v) for k, v in theta.items() if k.startswith(pre)}
    return slots, tests


def materials_for(prep: Prepared, theta: Mapping[str, float], *, elastic_only: bool) -> dict[str, NeutralMaterial]:
    locals_, _ = split_theta(prep, theta)
    out: dict[str, NeutralMaterial] = {}
    for sc in prep.slots:
        if sc.identify:
            m = sc.model.to_neutral(locals_[sc.name])
            if elastic_only:
                m = replace(m, table=None)  # R8: 탄성 단계는 탄성 전용 카드(MAT_001 등가)
            out[sc.name] = m
        else:
            if sc.reference is None:
                raise EngineFailure(E.INTERNAL_ERROR, f"{E.FIXED_SLOT_REFERENCE_MISSING}: slot {sc.name}")
            out[sc.name] = sc.reference  # R5
    return out


class Evaluator:
    def __init__(self, prep: Prepared, services: Any, solver_name: str, solver_options: Mapping[str, Any],
                 run_timeout_s: float) -> None:
        self.prep = prep
        self.services = services
        self.cfg = services.config
        reg = services.registry
        self.solver_name = reg.resolve_name("solver", solver_name)
        self.solver_entry = reg.enabled_entry("solver", self.solver_name)
        self.adapter = reg.create("solver", self.solver_name, dict(solver_options or {}))
        self.timeout = float(run_timeout_s)
        self.ncpu = int(self.cfg.get("solver.lsdyna.ncpu"))
        self.counter = 0
        self.best_prepared: dict[str, list[PreparedRun]] = {}
        self.keep_all = bool(self.cfg.get("solver.keep_all_files"))
        self.keep_mode = str(self.cfg.get("solver.keep_d3plot"))
        self.solver_versions: list[str] = []
        self.history: list[dict[str, Any]] = []

    # ---- 평가 ----
    def evaluate(self, theta: Mapping[str, float], stage: str, *, elastic_only: bool, record: bool = True,
                 history_stage: str | None = None) -> EvalRecord:
        cancel = self.services.cancel
        if cancel is not None and cancel.is_set():
            raise CalibrationCancelled("cancelled")
        t0 = time.perf_counter()
        started = datetime.now(timezone.utc).isoformat()
        idx = self.counter
        self.counter += 1
        theta = {k: float(v) for k, v in theta.items()}
        mats = materials_for(self.prep, theta, elastic_only=elastic_only)
        _, test_theta = split_theta(self.prep, theta)
        rw_stage = {"plastic": 0.0} if elastic_only else None  # 탄성 단계: w_plastic = 0 (Ω1만)
        cases: dict[str, CaseOutcome] = {}
        pairs: dict[tuple[str, str], AL.AlignedPair] = {}
        per_test: list[dict[str, Any]] = []
        per_run: list[dict[str, Any]] = []
        jcomp: dict[str, Any] = {}
        resid_parts: list[np.ndarray] = []
        penalized = False
        any_curve = False
        all_curve = True
        failed_J = float(self.cfg.get("objective.failed_run_J"))
        W_sum = sum(tc.weight for tc in self.prep.tests) or 1.0
        combine: list[tuple[float, dict[str, tuple[float, float]]]] = []
        for tc in self.prep.tests:
            overrides: dict[str, float] = {}
            for pname, val in test_theta.get(tc.key, {}).items():
                tgt = tc.targets.get(pname, "")
                if tgt.startswith("recipe:"):
                    overrides[tgt.split(":", 1)[1]] = val
            for cc in tc.cases:
                sc_case = cc.sim_case
                assert sc_case is not None
                if overrides:
                    sc_case = replace(sc_case, params_si={**dict(sc_case.params_si), **overrides})
                wd = Path(self.services.workdir("evals", f"{idx:05d}", cc.case_id))
                cases[cc.case_id] = self._run_case(sc_case, mats, wd, tc.key)
            resp_terms: dict[str, tuple[float, float]] = {}
            tj: dict[str, Any] = {}
            sw = sum(r.weight for r in tc.responses if r.kind == "curve") or 1.0
            for resp in tc.responses:
                if resp.kind != "curve":
                    continue
                rw = dict(resp.window.region_weights or {})
                if rw_stage is not None:
                    rw.update(rw_stage)
                jr_list = []
                comps = {"elastic": [], "plastic": [], "shortfall": []}
                for rc in tc.runs:
                    at = rc.aligned.get(resp.response_key)
                    if at is None or not at.ok:
                        continue
                    co = cases[rc.case_id]
                    curve = co.extracted.curves.get(resp.response_key) if co.extracted is not None else None
                    has = curve is not None and len(np.asarray(curve.x)) >= 2
                    if not has:
                        all_curve = False
                        penalized = True
                        jr = failed_J
                        pair = AL.align_sim(at, None, self.cfg, quasi_static=tc.quasi_static)
                        terms = {"J": jr, "elastic": math.nan, "plastic": math.nan, "shortfall": 1.0}
                    else:
                        any_curve = True
                        pair = AL.align_sim(at, curve, self.cfg, quasi_static=tc.quasi_static)
                        terms = OBJ.curve_terms(pair, rw, self.cfg)
                        jr = terms["J"]
                        if co.status is not None and co.status.state != RunState.NORMAL:
                            penalized = True
                        if pair.shortfall > 0:
                            penalized = True
                    pairs[(rc.run.run_id, resp.response_key)] = pair
                    jr_list.append(jr)
                    for k in comps:
                        comps[k].append(terms[k])
                    scale = (tc.weight / W_sum) * (resp.weight / sw) / max(1, len(tc.runs))
                    resid_parts.append(OBJ.residual_vector(pair, rw, self.cfg, scale=scale))
                    per_run.append(self._run_metrics(rc, tc.key, resp.response_key, pair, jr))
                mean_j = float(np.mean(jr_list)) if jr_list else failed_J
                resp_terms[resp.response_key] = (resp.weight, mean_j)
                tj[resp.response_key] = {k: (float(np.nanmean(v)) if v and np.any(np.isfinite(v)) else None)
                                         for k, v in comps.items()}
                tj[resp.response_key]["scalar"] = None
                tj[resp.response_key]["J"] = mean_j
            combine.append((tc.weight, resp_terms))
            jcomp[tc.key] = tj
        J, Jt = OBJ.combine_tests(combine)
        for tc, jt in zip(self.prep.tests, Jt):
            per_test.append({"test_key": tc.key, "J_t": float(jt),
                             "per_response": [{"response_key": rk, "J": float(v["J"]),
                                               "elastic": v["elastic"], "plastic": v["plastic"],
                                               "shortfall": v["shortfall"]} for rk, v in jcomp[tc.key].items()]})
        if not math.isfinite(J):
            J = failed_J
            penalized = True
        reg_parts: list[np.ndarray] = []
        if not elastic_only:  # 선택: 모델 정칙화 잔차(최적화기 잔차에만 — J·사후 진단 잔차는 데이터만)
            locals_, _ = split_theta(self.prep, theta)
            for sc in self.prep.slots:
                fn = getattr(sc.model, "regularization_residuals", None) if sc.identify else None
                if fn is not None:
                    rr = np.asarray(fn(locals_[sc.name]), dtype=float)
                    if rr.size:
                        reg_parts.append(rr)
        residual = np.concatenate(resid_parts) if resid_parts else np.array([])
        reg_residual = np.concatenate(reg_parts) if reg_parts else np.array([])
        rec = EvalRecord(eval_index=idx, stage=history_stage or stage, theta=theta, J=float(J), penalized=penalized,
                         failed=not any_curve, valid=all_curve, components=jcomp, residual=residual,
                         per_test=per_test, per_run=per_run, pairs=pairs, cases=cases, materials=mats,
                         wall_time_s=time.perf_counter() - t0, started_at=started, reg_residual=reg_residual)
        return rec

    def _run_case(self, case, mats, wd: Path, test_key: str) -> CaseOutcome:
        ad = self.adapter
        try:
            prepared = ad.prepare(case, mats, wd)
        except Exception as e:
            return CaseOutcome(case.case_id, test_key, None, None, None, wd, error=f"prepare: {e!r}")
        try:
            status = ad.run(prepared, self.ncpu, self.timeout, self.services.cancel)
        except (FileNotFoundError, PermissionError) as e:
            raise EngineFailure(E.SOLVER_UNAVAILABLE, f"solver unavailable: {e!r}") from e
        except OSError as e:
            raise EngineFailure(E.SOLVER_UNAVAILABLE, f"solver unavailable: {e!r}") from e
        except Exception as e:
            return CaseOutcome(case.case_id, test_key, prepared, None, None, wd, error=f"run: {e!r}")
        if status.state == RunState.CANCELLED:
            raise CalibrationCancelled("cancelled during solver run")
        if status.state == RunState.LICENSE_TIMEOUT:
            raise EngineFailure(E.LICENSE_TIMEOUT, "license wait exceeded solver.license_retry.max_wait_s")
        if status.solver_version:
            self.solver_versions.append(status.solver_version)
        try:
            ext = ad.extract(prepared, status)
        except Exception as e:
            return CaseOutcome(case.case_id, test_key, prepared, status, None, wd, error=f"extract: {e!r}")
        return CaseOutcome(case.case_id, test_key, prepared, status, ext, wd)

    @staticmethod
    def _run_metrics(rc, test_key: str, rk: str, pair: AL.AlignedPair, J: float) -> dict[str, Any]:
        ok = np.isfinite(pair.f_sim)
        fmax = float(np.max(np.abs(pair.f_exp))) if pair.f_exp.size else 0.0
        if np.any(ok):
            err = pair.f_sim[ok] - pair.f_exp[ok]
            rmse = float(math.sqrt(float(np.mean(err * err))))
            mae = float(np.max(np.abs(err)))
        else:
            rmse = mae = math.nan
        return {"run_id": rc.run.run_id, "test_key": test_key, "response_key": rk, "J": float(J),
                "rmse_N": rmse, "max_abs_err_N": mae, "nrmse": (rmse / fmax if fmax > 0 and math.isfinite(rmse) else None),
                "shortfall": float(pair.shortfall)}

    # ---- 정리·이력 ----
    def finish(self, rec: EvalRecord, *, is_best: bool, record: bool = True) -> None:
        rec.is_best = is_best
        if self.keep_all:
            keep_this = "all"
        elif self.keep_mode == "all":
            keep_this = "results"
        elif self.keep_mode == "none":
            keep_this = "minimal"
        else:
            keep_this = "results" if is_best else "minimal"
        stage = rec.stage
        if is_best and self.keep_mode == "best" and not self.keep_all:
            for p in self.best_prepared.get(stage, []):
                self._cleanup(p, "minimal")
            self.best_prepared[stage] = [c.prepared for c in rec.cases.values() if c.prepared is not None]
        for c in rec.cases.values():
            if c.prepared is not None:
                self._cleanup(c.prepared, keep_this)
        if record:
            item = {
                "eval_index": rec.eval_index,
                "stage": rec.stage,
                "theta": dict(rec.theta),
                "J": float(rec.J),
                "J_components": _jsonable(rec.components),
                "status": "penalized" if rec.penalized else "ok",
                "run_status": {f"{c.test_key}/{cid}": c.state for cid, c in rec.cases.items()},
                "mode_used": {f"{c.test_key}/{cid}": (c.status.mode_used if c.status else None)
                              for cid, c in rec.cases.items()},
                "wall_time_s": float(rec.wall_time_s),
                "is_best": bool(is_best),
                "started_at": rec.started_at,
            }
            self.history.append(item)
            self.services.record_eval(item)

    def _cleanup(self, prepared: PreparedRun, keep: str) -> None:
        try:
            self.adapter.cleanup(prepared, keep)
        except Exception:
            pass


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return _jsonable(obj.item())
    return obj
