"""식별자: ULID(26자 Crockford base32 대문자)와 접두사 ID (api.md §1.1).

| 대상 | 형식 |
| 잡 | `cal_` + ULID |
| 입력 파일 | `fil_` + ULID |
| 산출 파일 | `art_` + ULID |
| 레시피 검증 | `ver_` + ULID |

ID는 불투명 문자열이다. 시간 정렬 가능(상위 48비트 = epoch ms)하며, 같은 밀리초 안에서는 단조 증가시킨다.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Final

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE: Final = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_MAX_RAND: Final = (1 << 80) - 1
_MAX_TS: Final = (1 << 48) - 1

JOB_PREFIX: Final = "cal_"
FILE_PREFIX: Final = "fil_"
ARTIFACT_PREFIX: Final = "art_"
VERIFICATION_PREFIX: Final = "ver_"
ID_PREFIXES: Final[dict[str, str]] = {
    "job": JOB_PREFIX,
    "file": FILE_PREFIX,
    "artifact": ARTIFACT_PREFIX,
    "verification": VERIFICATION_PREFIX,
}

_lock = threading.Lock()
_last_ts = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """새 ULID. `timestamp_ms`를 주면 그 시각으로 만든다(시험용). 같은 ms 안에서는 단조 증가."""
    global _last_ts, _last_rand
    ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if not 0 <= ts <= _MAX_TS:
        raise ValueError(f"timestamp_ms out of 48-bit range: {ts}")
    with _lock:
        if ts == _last_ts:
            rand = _last_rand + 1
            if rand > _MAX_RAND:  # 오버플로 시 다음 ms 로 넘기지 않고 새 난수(극히 드묾)
                rand = int.from_bytes(os.urandom(10), "big")
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ts, _last_rand = ts, rand
    return _encode(ts, 10) + _encode(rand, 16)


def is_ulid(value: str) -> bool:
    # 첫 문자는 0–7 (48비트 시각이 26자 중 상위 10자 = 50비트에 들어가는 상한)
    return isinstance(value, str) and bool(_ULID_RE.match(value)) and value[0] in "01234567"


def ulid_timestamp_ms(ulid: str) -> int:
    if not is_ulid(ulid):
        raise ValueError(f"not a ULID: {ulid!r}")
    v = 0
    for ch in ulid[:10]:
        v = (v << 5) | _CROCKFORD.index(ch)
    return v


def new_id(kind: str, timestamp_ms: int | None = None) -> str:
    """접두사 ID. kind ∈ {"job", "file", "artifact", "verification"}."""
    try:
        prefix = ID_PREFIXES[kind]
    except KeyError:
        raise ValueError(f"unknown id kind {kind!r}; known: {sorted(ID_PREFIXES)}") from None
    return prefix + new_ulid(timestamp_ms)


def new_job_id() -> str:
    return new_id("job")


def new_file_id() -> str:
    return new_id("file")


def new_artifact_id() -> str:
    return new_id("artifact")


def new_verification_id() -> str:
    return new_id("verification")


def is_valid_id(kind: str, value: str) -> bool:
    prefix = ID_PREFIXES[kind]
    return isinstance(value, str) and value.startswith(prefix) and is_ulid(value[len(prefix):])
