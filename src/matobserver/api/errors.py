"""오류 본문·예외 처리 (api.md §7).

본문: `{"schema_version": "1.0", "error": {"code", "message", "details": [...]}}`. 서버 경로·내부 예외 문자열은 넣지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..artifacts.service import ArtifactExpired, ArtifactNotFound
from ..core import errors as E
from ..jobs.files import FileTooLarge, FileTypeUnsupported
from ..jobs.store import IdempotencyKeyReused, JobAlreadyFinished, JobNotFound
from ..matnexus.client import UpstreamUnavailable
from ..recipes.repository import RecipeNotFound, RecipeNotRetirable, RecipeVersionConflict, StaticCheckFailed
from ..validation.errors import ValidationFailed
from .schemas import SCHEMA_VERSION

log = logging.getLogger("matobserver.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def error_body(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"schema_version": SCHEMA_VERSION, "error": err}


def _resp(status: int, code: str, message: str, details: list[dict[str, Any]] | None = None,
          headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code, message, details), headers=headers)


def schema_detail(message: str, field: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"code": E.SCHEMA_INVALID, "message": message}
    if field:
        d["field"] = field
    return d


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return _resp(exc.status, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(ValidationFailed)
    async def _vf(_: Request, exc: ValidationFailed) -> JSONResponse:
        return _resp(400, E.VALIDATION_FAILED, f"요청 검증 실패 {len(exc.details)}건", exc.details)

    @app.exception_handler(StaticCheckFailed)
    async def _scf(_: Request, exc: StaticCheckFailed) -> JSONResponse:
        details = [i.to_detail() for i in exc.issues]
        return _resp(400, E.VALIDATION_FAILED, f"레시피 정적 검사 실패 {len(details)}건", details)

    @app.exception_handler(UpstreamUnavailable)
    async def _up(_: Request, exc: UpstreamUnavailable) -> JSONResponse:
        return _resp(502, E.UPSTREAM_UNAVAILABLE, "MatNexus 조회에 실패했습니다. 잠시 후 다시 시도하세요.")

    @app.exception_handler(JobNotFound)
    async def _jnf(_: Request, exc: JobNotFound) -> JSONResponse:
        return _resp(404, E.NOT_FOUND, "잡을 찾을 수 없습니다.")

    @app.exception_handler(ArtifactNotFound)
    async def _anf(_: Request, exc: ArtifactNotFound) -> JSONResponse:
        return _resp(404, E.NOT_FOUND, "artifact 를 찾을 수 없습니다.")

    @app.exception_handler(RecipeNotFound)
    async def _rnf(_: Request, exc: RecipeNotFound) -> JSONResponse:
        return _resp(404, E.NOT_FOUND, "레시피를 찾을 수 없습니다.")

    @app.exception_handler(ArtifactExpired)
    async def _ae(_: Request, exc: ArtifactExpired) -> JSONResponse:
        return _resp(410, E.ARTIFACT_EXPIRED, "보존 기간이 지난 artifact 입니다.")

    @app.exception_handler(JobAlreadyFinished)
    async def _jaf(_: Request, exc: JobAlreadyFinished) -> JSONResponse:
        return _resp(409, E.JOB_ALREADY_FINISHED, "이미 종결된 잡입니다.")

    @app.exception_handler(IdempotencyKeyReused)
    async def _ikr(_: Request, exc: IdempotencyKeyReused) -> JSONResponse:
        return _resp(409, E.IDEMPOTENCY_KEY_REUSED, "같은 Idempotency-Key 가 다른 본문으로 이미 사용되었습니다.")

    @app.exception_handler(RecipeVersionConflict)
    async def _rvc(_: Request, exc: RecipeVersionConflict) -> JSONResponse:
        return _resp(409, E.RECIPE_VERSION_CONFLICT, "같은 recipe_id@version 이 다른 내용으로 이미 등록되어 있습니다.")

    @app.exception_handler(RecipeNotRetirable)
    async def _rnr(_: Request, exc: RecipeNotRetirable) -> JSONResponse:
        return _resp(409, E.RECIPE_NOT_RETIRABLE, str(exc))

    @app.exception_handler(FileTooLarge)
    async def _ftl(_: Request, exc: FileTooLarge) -> JSONResponse:
        return _resp(413, E.FILE_TOO_LARGE, str(exc))

    @app.exception_handler(FileTypeUnsupported)
    async def _ftu(_: Request, exc: FileTypeUnsupported) -> JSONResponse:
        return _resp(400, E.FILE_TYPE_UNSUPPORTED, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _rve(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for er in exc.errors():
            loc = [str(p) for p in er.get("loc", ()) if p not in ("query", "path", "body", "form", "header")]
            details.append(schema_detail(er.get("msg", "잘못된 값"), ".".join(loc) or None))
        return _resp(400, E.VALIDATION_FAILED, "요청 검증 실패", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _resp(404, E.NOT_FOUND, "리소스를 찾을 수 없습니다.")
        if exc.status_code == 405:
            return _resp(405, E.VALIDATION_FAILED, "허용되지 않는 메서드입니다.", None, {"Allow": exc.headers.get("Allow", "") if exc.headers else ""})
        return _resp(exc.status_code, E.INTERNAL_ERROR if exc.status_code >= 500 else E.VALIDATION_FAILED, str(exc.detail))

    @app.exception_handler(Exception)
    async def _any(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _resp(500, E.INTERNAL_ERROR, "내부 오류가 발생했습니다.")
