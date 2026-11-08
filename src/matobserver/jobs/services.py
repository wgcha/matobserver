"""EngineServices 구현 (module-boundaries.md §4.1) — 워커가 엔진/레시피 검증기에 주는 서비스 객체."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from ..artifacts.service import OWNER_JOB, ArtifactService
from ..core.config import Config
from ..core.registry import Registry
from .store import JobStore


class DbCancelToken:
    """`CancelToken`: 취소 요청(DB `cancel_requested`)을 `worker.cancel_poll_s` 간격으로 확인한다 (api.md §4.12).

    한 번 True 가 되면 계속 True. 사이 호출은 캐시된 값을 돌려줘 DB 부하를 줄인다.
    """

    def __init__(self, store: JobStore, job_id: str, poll_s: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._store, self._job_id, self._poll_s, self._mono = store, job_id, float(poll_s), monotonic
        self._set = False
        self._last = float("-inf")

    def is_set(self) -> bool:
        if self._set:
            return True
        now = self._mono()
        if now - self._last >= self._poll_s:
            self._last = now
            self._set = self._store.is_cancel_requested(self._job_id)
        return self._set


class NeverCancel:
    def is_set(self) -> bool:
        return False


class JobServices:
    """`EngineServices`. 잡(owner_kind="job")과 레시피 검증(owner_kind="recipe_verification") 공용."""

    def __init__(
        self,
        *,
        owner_kind: Literal["job", "recipe_verification"],
        owner_id: str,
        registry: Registry,
        config: Config,
        store: JobStore,
        artifacts: ArtifactService,
        cancel: Any,
        job_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.registry = registry
        self.config = config
        self.cancel = cancel
        #: 요청에서 정해진 잡 옵션 (CalibrationSpec 에 필드가 없는 것): `plasticity_mode`, `explicit_check`(해석된 값). 계약 개정 전까지의 전달 통로.
        self.job_options: Mapping[str, Any] = dict(job_options or {})
        self._store = store
        self._artifacts = artifacts

    def workdir(self, *parts: str) -> Path:
        base = self._artifacts.work_root(self.owner_kind, self.owner_id)
        p = base.joinpath(*parts) if parts else base
        rb = base.resolve()
        if not p.resolve().is_relative_to(rb):
            raise ValueError("workdir 경로가 작업 폴더를 벗어납니다.")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_artifact(self, path: Path, kind: str, *, slot: str | None = None, test_key: str | None = None) -> str:
        return self._artifacts.register(self.owner_kind, self.owner_id, path, kind, slot=slot, test_key=test_key)

    def record_eval(self, item: Mapping[str, Any]) -> None:
        if self.owner_kind == OWNER_JOB:
            self._store.record_eval(self.owner_id, item)

    def progress(self, stage: str, **fields: Any) -> None:
        if self.owner_kind == OWNER_JOB:
            self._store.set_progress(self.owner_id, stage, fields)
