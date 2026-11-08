"""InlineRun(MatNexus 밖 시험 데이터, api.md §4.5) -> RunRecord 정규화."""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from ..core import errors as E
from ..core.hashing import sha256_json
from ..core.types import RunRecord

#: 채널 키 -> SI 정본 단위 (MatNexus BUILTIN_TEST_TYPES flexural/compression 채널 정의와 같다)
CHANNEL_UNITS: dict[str, str] = {"force": "N", "displacement": "m", "deflection": "m", "time": "s", "strain": "1"}


class InlineRunError(ValueError):
    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def normalize_inline_run(run: Mapping[str, Any]) -> RunRecord:
    """검증된 InlineRun 매핑 -> RunRecord. 단위는 SI 정본만 허용(`CHANNEL_UNIT_INVALID`), 길이 불일치·비수치는 `INLINE_RUN_INVALID`."""
    key = run["run_key"]
    channels: dict[str, np.ndarray] = {}
    length: int | None = None
    for name, ch in (run.get("channels") or {}).items():
        f = f"channels.{name}"
        if name not in CHANNEL_UNITS:
            raise InlineRunError(E.INLINE_RUN_INVALID, f"알 수 없는 채널 키: {name!r} (허용: {sorted(CHANNEL_UNITS)})", f)
        unit = ch.get("unit")
        if unit != CHANNEL_UNITS[name]:
            raise InlineRunError(E.CHANNEL_UNIT_INVALID, f"채널 {name} 의 단위는 SI 정본 {CHANNEL_UNITS[name]!r} 이어야 합니다: {unit!r}",
                                 f + ".unit")
        vals = ch.get("values")
        if not isinstance(vals, list) or not vals:
            raise InlineRunError(E.INLINE_RUN_INVALID, f"채널 {name} 의 values 가 비어 있습니다.", f + ".values")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vals):
            raise InlineRunError(E.INLINE_RUN_INVALID, f"채널 {name} 에 유한한 수가 아닌 값이 있습니다.", f + ".values")
        if length is None:
            length = len(vals)
        elif len(vals) != length:
            raise InlineRunError(E.INLINE_RUN_INVALID, "모든 채널의 길이가 같아야 합니다.", f + ".values")
        channels[name] = np.asarray(vals, dtype=float)
    dims: dict[str, float] = {}
    for k, v in (run.get("specimen_dimensions") or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise InlineRunError(E.INLINE_RUN_INVALID, f"치수 {k} 가 유한한 수가 아닙니다.", f"specimen_dimensions.{k}")
        dims[str(k)] = float(v)
    return RunRecord(
        run_id=f"inline:{key}",
        test_type_key=run["test_type_key"],
        channels=channels,
        specimen_dimensions=dims,
        conditions=dict(run.get("conditions") or {}),
        instrument=run.get("instrument"),
        specimen_id=run.get("specimen_ref"),
        material_id=None,
        source_sha256=None,
        fetched_hash=sha256_json({"test_type_key": run["test_type_key"], "channels": channels, "specimen_dimensions": dims}),
    )
