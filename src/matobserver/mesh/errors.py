"""메시 플러그인 예외."""

from __future__ import annotations

from ..core.errors import MESH_FAILED, MatObserverError


class MeshError(MatObserverError, ValueError):
    """메시 생성 실패. `code`는 api.md 코드(기본 `MESH_FAILED`, STP 형상 문제면 `STP_*`)."""

    def __init__(self, message: str, *, code: str = MESH_FAILED) -> None:
        super().__init__(message, code=code)
