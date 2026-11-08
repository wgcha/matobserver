"""정규화 JSON sha256 (api/result 계약의 request_hash·config_hash·input_hash·content_hash 공통 기반).

정규화 규칙(결정적, 키 정렬·공백 없음 — result.md §4.11 `request_hash` 정의):

* 객체 키 정렬, 구분자 `(",", ":")`, UTF-8, `ensure_ascii=False`.
* 매핑 키는 문자열이어야 한다(아니면 TypeError — 문자열화 충돌 방지).
* tuple·list·numpy 배열 → 배열, set/frozenset → 정렬된 배열, Path → POSIX 문자열, Enum → 값,
  dataclass → 필드 매핑, numpy 스칼라 → Python 수.
* 유한한 정수값 float(예 `1.0`)은 정수로 쓴다(`1`과 `1.0`이 같은 해시). -0.0 → 0.
  NaN/inf 는 JSON 으로 표현할 수 없으므로 ValueError.
* 다이제스트 표기는 `"sha256:" + hex` (recipe.md §5 `content_hash`와 같은 접두사 형식).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from enum import Enum
from pathlib import PurePath
from typing import Any

try:  # numpy 는 core 의존성이지만 import 실패에도 해시 모듈이 죽지 않게 한다.
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

HASH_PREFIX = "sha256:"
_MAX_EXACT_INT_FLOAT = 2.0**53


def _normalize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"non-finite float cannot be hashed: {obj!r}")
        if obj == 0.0:
            return 0
        if obj.is_integer() and abs(obj) < _MAX_EXACT_INT_FLOAT:
            return int(obj)
        return obj
    if isinstance(obj, Enum):
        return _normalize(obj.value)
    if isinstance(obj, PurePath):
        return obj.as_posix()
    if np is not None:
        if isinstance(obj, np.ndarray):
            return [_normalize(v) for v in obj.tolist()]
        if isinstance(obj, np.generic):
            return _normalize(obj.item())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _normalize(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict) or (hasattr(obj, "keys") and hasattr(obj, "__getitem__")):
        out: dict[str, Any] = {}
        for k in obj.keys():
            if not isinstance(k, str):
                raise TypeError(f"mapping keys must be str for hashing, got {type(k).__name__}: {k!r}")
            out[k] = _normalize(obj[k])
        return out
    if isinstance(obj, (set, frozenset)):
        items = [_normalize(v) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    raise TypeError(f"cannot canonicalize object of type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """정규화 JSON 문자열."""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_json_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """바이트의 sha256 hex(접두사 없음)."""
    return hashlib.sha256(data).hexdigest()


def sha256_digest(data: bytes) -> str:
    """바이트의 `sha256:<hex>`."""
    return HASH_PREFIX + sha256_hex(data)


def sha256_json(obj: Any) -> str:
    """정규화 JSON 의 `sha256:<hex>` (request_hash·config_hash 등)."""
    return sha256_digest(canonical_json_bytes(obj))
