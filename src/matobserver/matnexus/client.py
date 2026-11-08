"""MatNexus 읽기 전용 HTTP 클라이언트 (api.md §2·A-5, result.md §7.1).

MatNexus 는 수정하지 않는다. 쓰는 엔드포인트(openapi.json 기준):

* `GET /api/test-runs/{run_id}` -> `TestRunDetailOut` (status, parse_error, channels[], conditions, instrument,
  specimen_id, material_id, source_sha256, test_type_key)
* `GET /api/test-runs/{run_id}/curve?x=&y=&max_points=` -> `CurvePointsOut` (`points` [[x, y]], LTTB 축약, max_points ≤ 5000)
* `GET /api/specimens/{specimen_id}/dimensions` -> `SpecimenSizesOut` (`fields[]` 의 measured ?? nominal, SI)

실패 분류: 404 -> `RunNotFound`(400 `RUN_NOT_FOUND`), 파싱 안 된 run -> `RunUnreadable`(400 `RUN_UNREADABLE`),
네트워크·타임아웃·5xx·401/403 -> `UpstreamUnavailable`(502 `UPSTREAM_UNAVAILABLE`).

곡선 채널은 `(채널, force)` 쌍으로 가져온다. LTTB 축약은 쌍마다 달라 길이가 `max_points` 를 넘는 곡선에서는 채널 간 표본이 어긋날 수 있다.
힘 배열이 첫 쌍과 같지 않은 채널은 버리고 `conditions["_dropped_channels"]` 에 기록한다 (행 수 ≤ `matnexus.curve_max_points` 면 무손실).
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx
import numpy as np

from ..core import errors as E
from ..core.config import Config
from ..core.errors import MatObserverError
from ..core.hashing import sha256_json
from ..core.types import RunRecord

READY_STATUSES = frozenset({"parsed", "imported"})
FORCE = "force"


class UpstreamUnavailable(MatObserverError):
    code = E.UPSTREAM_UNAVAILABLE


class RunNotFound(MatObserverError):
    code = E.RUN_NOT_FOUND


class RunUnreadable(MatObserverError):
    code = E.RUN_UNREADABLE


class MatNexusClient:
    def __init__(
        self,
        base_url: str | None,
        token: str | None,
        timeout_s: float = 30.0,
        curve_max_points: int = 5000,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout_s = float(timeout_s)
        self.curve_max_points = int(curve_max_points)
        self._client = client
        self._transport = transport

    @classmethod
    def from_config(cls, config: Config, *, transport: httpx.BaseTransport | None = None) -> "MatNexusClient":
        # 토큰이 `env:VAR` 참조인데 환경변수가 없으면 ConfigError 가 나므로, 호출 시점(요청 처리)이 아니라 여기서 풀지 않고 지연한다.
        try:
            token = config.secret("matnexus.token")
        except Exception:
            token = None
        return cls(config.get("matnexus.base_url"), token, config.get("matnexus.timeout_s"),
                   config.get("matnexus.curve_max_points"), transport=transport)

    # ------------------------------------------------------------------ HTTP
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s, transport=self._transport)
        return self._client

    def _get(self, path: str, params: Mapping[str, Any] | None = None, *, missing_ok: bool = False) -> Any:
        if not self.base_url:
            raise UpstreamUnavailable("matnexus.base_url 이 설정되지 않았습니다.")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self._http().get(self.base_url + path, params=dict(params or {}), headers=headers, timeout=self.timeout_s)
        except httpx.HTTPError as e:  # 타임아웃·연결 실패 등
            raise UpstreamUnavailable(f"MatNexus 에 연결할 수 없습니다: {type(e).__name__}") from e
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as e:
                raise UpstreamUnavailable("MatNexus 응답이 JSON 이 아닙니다.") from e
        if resp.status_code == 404:
            if missing_ok:
                return None
            raise RunNotFound(path)
        if resp.status_code == 422 and missing_ok:
            return None
        if resp.status_code in (401, 403):
            raise UpstreamUnavailable(f"MatNexus 인증 실패 (HTTP {resp.status_code})")
        if resp.status_code >= 500:
            raise UpstreamUnavailable(f"MatNexus 오류 (HTTP {resp.status_code})")
        raise UpstreamUnavailable(f"MatNexus 가 예상하지 못한 응답을 보냈습니다 (HTTP {resp.status_code})")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ 엔드포인트
    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get(f"/api/test-runs/{run_id}")

    def get_curve(self, run_id: str, x: str, y: str) -> dict[str, Any] | None:
        """(x, y) 채널 쌍. 채널이 없으면 None (MatNexus 422)."""
        return self._get(f"/api/test-runs/{run_id}/curve",
                         {"x": x, "y": y, "max_points": self.curve_max_points}, missing_ok=True)

    def get_specimen_dimensions(self, specimen_id: str) -> dict[str, float]:
        """시편 치수 {key: measured ?? nominal} (SI). 시편·치수가 없으면 빈 매핑."""
        body = self._get(f"/api/specimens/{specimen_id}/dimensions", missing_ok=True)
        dims: dict[str, float] = {}
        for f in (body or {}).get("fields", []) or []:
            v = f.get("measured")
            if v is None:
                v = f.get("nominal")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                dims[str(f["key"])] = float(v)
        return dims

    # ------------------------------------------------------------------ RunRecord
    def fetch_run_record(self, run_id: str) -> RunRecord:
        detail = self.get_run(run_id)
        status = detail.get("status")
        if status not in READY_STATUSES:
            why = detail.get("parse_error") or f"status={status}"
            raise RunUnreadable(f"{run_id}: {why}")
        channel_keys = [str(c) for c in (detail.get("channels") or [])]
        channels: dict[str, np.ndarray] = {}
        dropped: list[str] = []
        if FORCE in channel_keys:
            force: np.ndarray | None = None
            for ch in channel_keys:
                if ch == FORCE:
                    continue
                body = self.get_curve(run_id, ch, FORCE)
                pts = (body or {}).get("points") or []
                if not pts:
                    dropped.append(ch)
                    continue
                arr = np.asarray(pts, dtype=float)
                xs, ys = arr[:, 0], arr[:, 1]
                if force is None:
                    force = ys
                    channels[FORCE] = ys
                elif len(ys) != len(force) or not np.array_equal(ys, force):
                    dropped.append(ch)
                    continue
                channels[ch] = xs
            if force is None:  # force 외 채널이 없다: (force, force) 쌍으로 힘만 가져온다
                body = self.get_curve(run_id, FORCE, FORCE)
                pts = (body or {}).get("points") or []
                if pts:
                    channels[FORCE] = np.asarray(pts, dtype=float)[:, 1]
        dims: dict[str, float] = {}
        specimen_id = detail.get("specimen_id")
        if specimen_id:
            dims = self.get_specimen_dimensions(str(specimen_id))
        conditions = dict(detail.get("conditions") or {})
        if dropped:
            conditions["_dropped_channels"] = dropped
        material_id = detail.get("material_id")
        return RunRecord(
            run_id=str(detail.get("id", run_id)),
            test_type_key=str(detail.get("test_type_key", "")),
            channels=channels,
            specimen_dimensions=dims,
            conditions=conditions,
            instrument=detail.get("instrument"),
            specimen_id=None if specimen_id is None else str(specimen_id),
            material_id=None if material_id is None else str(material_id),
            source_sha256=detail.get("source_sha256"),
            fetched_hash=sha256_json({"test_type_key": detail.get("test_type_key"), "channels": channels,
                                      "specimen_dimensions": dims}),
        )
