"""워커: 큐에서 항목 1건씩 순차 실행 (api.md §5, B-4·B-5).

* 한 번에 한 건: `JobStore.claim_next` 가 원자적으로 하나만 running 으로 바꾼다. 워커가 둘이어도 동시 실행은 최대 1건이다.
  (프로세스 하나만 띄우도록 `WorkerLock` 도 제공한다.)
* 시작 시 `recover()`: running 으로 남은 잡 -> failed(WORKER_LOST), 검증 -> rejected(error).
* 취소: `DbCancelToken` 이 `worker.cancel_poll_s` 마다 확인. 엔진 반환 후 취소 요청이 있으면 결과와 무관하게 cancelled.
* 오류 메시지에서 서버 경로는 가린다 (경로 비노출). 전체 트레이스백은 작업 폴더 `worker_error.log` 에만 남긴다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Mapping

from ..artifacts.service import OWNER_JOB, OWNER_RECIPE_VERIFICATION, ArtifactService
from ..core import errors as E
from ..core.config import Config
from ..core.errors import EngineFailure
from ..core.registry import Registry
from ..recipes.repository import RecipeRepository
from ..validation.hooks import EngineHooks
from ..validation.spec import build_spec, run_from_json
from .clock import Clock, iso, utcnow
from .services import DbCancelToken, JobServices
from .store import CANCELLED, FAILED, KIND_JOB, KIND_VERIFICATION, SUCCEEDED, JobStore, QueueItem

log = logging.getLogger("matobserver.worker")

#: 요청의 explicit_check 해석값 -> 설정 키 (CalibrationSpec 에 필드가 없어 services.config 덮어쓰기로 전한다)
_EXPLICIT_KEYS = {
    "loading_velocity_m_s": "verify.explicit_check.loading_velocity_m_s",
    "max_ke_ratio": "verify.explicit_check.max_ke_ratio",
}


def scrub_paths(message: str, roots: list[Path]) -> str:
    """메시지 안의 서버 절대 경로를 `<path>` 로 바꾼다."""
    out = message
    forms = {str(p.resolve()) for p in roots} | {str(p) for p in roots}
    forms |= {f.replace("\\", "/") for f in forms}  # Windows: 역슬래시·슬래시 두 표기 모두
    for r in sorted(forms, key=len, reverse=True):
        if r and r != "/":
            out = out.replace(r, "<path>")
    return out


def _is_cancelled_exc(e: BaseException) -> bool:
    """엔진의 `CalibrationCancelled`(core.errors.MatObserverError 하위)를 import 없이 알아본다 (엔진 미설치·지연 로딩 대비)."""
    return any(c.__name__ == "CalibrationCancelled" for c in type(e).__mro__)


class WorkerLock:
    """워커 프로세스가 하나만 돌도록 파일 잠금을 잡는다 (`paths.data_dir/worker.lock`)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            try:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # Windows
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class Worker:
    def __init__(
        self,
        *,
        config: Config,
        registry: Registry,
        store: JobStore,
        recipes: RecipeRepository,
        artifacts: ArtifactService,
        hooks: EngineHooks,
        clock: Clock = utcnow,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.config, self.registry, self.store, self.recipes = config, registry, store, recipes
        self.artifacts, self.hooks, self.clock, self._mono = artifacts, hooks, clock, monotonic

    # ------------------------------------------------------------------ 복구·루프
    def recover(self) -> dict[str, list[str]]:
        lost = self.store.recover_lost()
        for vid in lost[KIND_VERIFICATION]:
            now = iso(self.clock())
            self.recipes.finish_verification(vid, {
                "verification_id": vid, "status": "error", "started_at": None, "finished_at": now, "checks": [], "artifacts": [],
                "error": {"code": E.WORKER_LOST, "message": "워커가 비정상 종료되어 검증이 중단되었습니다."},
            })
        return lost

    def run_once(self) -> bool:
        """큐에서 한 건을 가져와 끝까지 실행한다. 가져올 항목이 없거나 다른 항목이 실행 중이면 False."""
        item = self.store.claim_next()
        if item is None:
            return False
        try:
            if item.kind == KIND_JOB:
                self._run_job(item)
            else:
                self._run_verification(item)
        finally:
            self.store.release_queue(item.kind, item.ref_id)
        return True

    def run_forever(self, stop: threading.Event, idle_sleep_s: float = 1.0) -> None:
        self.recover()
        while not stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 - 워커 루프는 죽지 않는다
                log.exception("worker loop error")
                busy = False
            if not busy:
                stop.wait(idle_sleep_s)

    # ------------------------------------------------------------------ 잡
    def _config_for(self, snapshot: Mapping[str, Any]) -> Config:
        ec = snapshot.get("explicit_check") or {}
        if not ec.get("requested") and not ec.get("enabled"):
            return self.config
        ov: dict[str, Any] = {"verify.explicit_check.enabled": bool(ec.get("enabled"))}
        for src, key in _EXPLICIT_KEYS.items():
            if ec.get(src) is not None:
                ov[key] = ec[src]
        sec = ec.get("section") or {}
        if sec.get("elform") is not None:
            ov["verify.explicit_check.elform"] = sec["elform"]
        hg = sec.get("hourglass") or {}
        if hg.get("ihq") is not None:
            ov["verify.explicit_check.hourglass.ihq"] = hg["ihq"]
        if hg.get("qm") is not None:
            ov["verify.explicit_check.hourglass.qm"] = hg["qm"]
        return self.config.with_overrides(ov)

    def _load_spec(self, job_id: str, snapshot: Mapping[str, Any]):
        work = self.artifacts.work_root(OWNER_JOB, job_id)
        runs = {}
        for t in snapshot["tests"]:
            for rid in t["run_ids"]:
                if rid in runs:
                    continue
                p = work / "inputs" / "runs" / (rid.replace(":", "_").replace("/", "_") + ".json")
                runs[rid] = run_from_json(json.loads(p.read_text(encoding="utf-8")))
        stp = {}
        for t in snapshot["tests"]:
            fid = t["geometry"].get("file_id")
            if fid:
                stp[fid] = work / "inputs" / "stp" / f"{fid}.stp"

        def resolver(ref: str):
            rid, _, ver = ref.partition("@")
            row = self.recipes.get_best(rid, int(ver))
            if row is None:
                raise EngineFailure(E.INTERNAL_ERROR, f"recipe {ref} is gone")
            return self.recipes.resolved(row)

        return build_spec(snapshot, job_id=job_id, runs=runs, recipe_resolver=resolver, stp_paths=stp)

    def _run_job(self, item: QueueItem) -> None:
        job_id = item.ref_id
        job = self.store.get_job(job_id)
        if job is None:
            return
        if job["cancel_requested"]:
            self.store.finish_job(job_id, CANCELLED)
            return
        roots = [self.config.path("paths.work_dir"), self.config.path("paths.data_dir"), self.config.path("recipes.store_dir")]
        token = DbCancelToken(self.store, job_id, float(self.config.get("worker.cancel_poll_s")), self._mono)
        try:
            if self.hooks.run_calibration is None:
                raise EngineFailure(E.INTERNAL_ERROR, "식별 엔진(matobserver.engine.run)이 설치되어 있지 않습니다.")
            snapshot = json.loads(job["spec_json"])
            spec = self._load_spec(job_id, snapshot)
            services = JobServices(
                owner_kind="job", owner_id=job_id, registry=self.registry, config=self._config_for(snapshot), store=self.store,
                artifacts=self.artifacts, cancel=token,
                job_options={"plasticity_mode": snapshot.get("plasticity_mode"), "explicit_check": snapshot.get("explicit_check")},
            )
            result = dict(self.hooks.run_calibration(spec, services))
        except Exception as e:  # noqa: BLE001
            self._on_run_exception(job_id, e, roots)
            return
        if self.store.is_cancel_requested(job_id):
            self.store.finish_job(job_id, CANCELLED)
            return
        try:
            result = self._finalize_result(job, result)
        except Exception as e:  # noqa: BLE001
            self._log_failure(job_id, e)
            self.store.finish_job(job_id, FAILED, error={"code": E.INTERNAL_ERROR, "message": "결과 저장 중 오류가 발생했습니다."})
            return
        self.store.finish_job(job_id, SUCCEEDED, result=result)

    def _on_run_exception(self, job_id: str, e: Exception, roots: list[Any]) -> None:
        """엔진 예외 -> 잡 종료 상태. 취소 3경로 중 1·2: `CalibrationCancelled`, 취소 요청 후의 다른 예외 -> cancelled (error null, 결과 없음)."""
        if _is_cancelled_exc(e) or self.store.is_cancel_requested(job_id):
            self.store.finish_job(job_id, CANCELLED)
            return
        if isinstance(e, EngineFailure):
            self.store.finish_job(job_id, FAILED, error={"code": e.code, "message": scrub_paths(str(e), roots)})
            return
        self._log_failure(job_id, e)
        self.store.finish_job(job_id, FAILED, error={"code": E.INTERNAL_ERROR, "message": "내부 오류로 잡이 실패했습니다."})

    def _log_failure(self, job_id: str, exc: BaseException) -> None:
        log.error("job %s failed: %r", job_id, exc)
        try:
            work = self.artifacts.work_root(OWNER_JOB, job_id)
            work.mkdir(parents=True, exist_ok=True)
            (work / "worker_error.log").write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
        except OSError:
            pass

    def _finalize_result(self, job: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """result.md §1 최상위 필드를 보정하고 artifact 목록을 등록 내용과 맞춘다. `result_json` artifact 가 없으면 만든다."""
        job_id = job["job_id"]
        result.setdefault("schema_version", job["schema_version"])
        result["job_id"] = job_id
        result["status"] = "succeeded"
        result.setdefault("created_at", job["created_at"])
        result.setdefault("finished_at", iso(self.clock()))
        recs = {a.artifact_id: a for a in self.artifacts.list_for_owner(OWNER_JOB, job_id)}
        if not any(a.kind == "result_json" for a in recs.values()):
            body = {k: v for k, v in result.items() if k != "artifacts"}
            work = self.artifacts.work_root(OWNER_JOB, job_id)
            out = work / "artifacts"
            out.mkdir(parents=True, exist_ok=True)
            path = out / "result.json"
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.artifacts.register(OWNER_JOB, job_id, path, "result_json")
            recs = {a.artifact_id: a for a in self.artifacts.list_for_owner(OWNER_JOB, job_id)}
        listed: list[Any] = list(result.get("artifacts") or [])
        out_refs: list[Any] = []
        used: set[str] = set()
        for it in listed:
            aid = it if isinstance(it, str) else (it.get("artifact_id") if isinstance(it, Mapping) else None)
            rec = recs.get(aid) if aid else None
            if rec is not None:
                out_refs.append(rec.ref())
                used.add(rec.artifact_id)
            elif isinstance(it, Mapping):
                out_refs.append(dict(it))
        out_refs.extend(rec.ref() for aid, rec in recs.items() if aid not in used)
        result["artifacts"] = out_refs
        return result

    # ------------------------------------------------------------------ 레시피 검증
    def _run_verification(self, item: QueueItem) -> None:
        vid = item.ref_id
        row = self.recipes.get_by_verification(vid)
        if row is None:
            return
        started = iso(self.clock())
        token = type("NoCancel", (), {"is_set": lambda self: False})()
        roots = [self.config.path("paths.work_dir"), self.config.path("paths.data_dir"), self.config.path("recipes.store_dir")]
        try:
            if self.hooks.verify_recipe is None:
                raise EngineFailure(E.INTERNAL_ERROR, "레시피 검증기(matobserver.verify)가 설치되어 있지 않습니다.")
            services = JobServices(owner_kind="recipe_verification", owner_id=vid, registry=self.registry, config=self.config,
                                   store=self.store, artifacts=self.artifacts, cancel=token)
            report = dict(self.hooks.verify_recipe(self.recipes.resolved(row), services, bool(row["strict"])))
        except Exception as e:  # noqa: BLE001
            code = e.code if isinstance(e, EngineFailure) else E.INTERNAL_ERROR
            msg = scrub_paths(str(e), roots) if isinstance(e, EngineFailure) else "검증 실행 중 내부 오류가 발생했습니다."
            if not isinstance(e, EngineFailure):
                log.error("verification %s failed: %r", vid, e)
            report = {"status": "error", "checks": [], "error": {"code": code, "message": msg}}
        report.setdefault("verification_id", vid)
        report.setdefault("started_at", started)
        report.setdefault("finished_at", iso(self.clock()))
        recs = self.artifacts.list_for_owner(OWNER_RECIPE_VERIFICATION, vid)
        if not report.get("artifacts"):
            report["artifacts"] = [{"artifact_id": a.artifact_id, "kind": a.kind} for a in recs]
        report.setdefault("status", "error")
        self.recipes.finish_verification(vid, report)
