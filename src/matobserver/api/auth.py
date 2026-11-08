"""Bearer 토큰 인증 (api.md §2): 서비스 토큰 / 관리자 토큰 2등급. 해시(SHA-256) 상수 시간 비교."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..core import errors as E
from .errors import ApiError


@dataclass(frozen=True)
class Principal:
    name: str
    admin: bool


def _norm_hash(h: str) -> str:
    h = h.strip().lower()
    return h[len("sha256:"):] if h.startswith("sha256:") else h


class TokenAuth:
    def __init__(self, service_tokens: Sequence[Mapping[str, Any]], admin_tokens: Sequence[Mapping[str, Any]]) -> None:
        self._service = [(str(t["name"]), _norm_hash(str(t["sha256"]))) for t in service_tokens]
        self._admin = [(str(t["name"]), _norm_hash(str(t["sha256"]))) for t in admin_tokens]

    @staticmethod
    def _find(digest: str, table: Sequence[tuple[str, str]]) -> str | None:
        found: str | None = None
        for name, h in table:  # 조기 종료 없이 전부 비교
            if hmac.compare_digest(digest, h) and found is None:
                found = name
        return found

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization:
            raise ApiError(401, E.AUTH_REQUIRED, "Authorization: Bearer <token> 헤더가 필요합니다.")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, E.AUTH_INVALID, "Authorization 헤더 형식이 올바르지 않습니다.")
        digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
        admin = self._find(digest, self._admin)
        service = self._find(digest, self._service)
        if admin is not None:
            return Principal(admin, True)
        if service is not None:
            return Principal(service, False)
        raise ApiError(401, E.AUTH_INVALID, "토큰이 올바르지 않습니다.")
