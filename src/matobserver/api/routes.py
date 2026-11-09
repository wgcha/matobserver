"""엔드포인트 (api.md §3 E1–E12, A1–A4)."""

from __future__ import annotations

import dataclasses
import json
import shutil
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import __version__
from ..core import errors as E
from ..core.hashing import sha256_json
from ..core.ids import new_job_id
from ..jobs.clock import iso, parse_iso
from ..jobs.store import ALL_STATUSES, JobNotFound, SUCCEEDED
from ..recipes.repository import RecipeNotFound
from ..validation.errors import ValidationFailed, make_detail
from ..validation.pipeline import parse_json_body, run_json_files, validate_request
from . import views
from .auth import Principal
from .context import AppContext
from .errors import ApiError, schema_detail
from .schemas import SCHEMA_VERSION, RetireRequest

router = APIRouter()
admin = APIRouter(prefix="/v1/admin")


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def service_principal(request: Request) -> Principal:
    return get_ctx(request).auth.authenticate(request.headers.get("authorization"))


def admin_principal(request: Request) -> Principal:
    p = service_principal(request)
    if not p.admin:
        raise ApiError(403, E.FORBIDDEN, "관리자 토큰이 필요합니다.")
    return p


def _bad(message: str, field: str | None = None) -> ApiError:
    return ApiError(400, E.VALIDATION_FAILED, "요청 검증 실패", [schema_detail(message, field)])


def page_params(request: Request, ctx: AppContext) -> tuple[int, int]:
    q = request.query_params
    default, mx = int(ctx.config.get("api.page_default_limit")), int(ctx.config.get("api.page_max_limit"))
    try:
        limit = int(q["limit"]) if "limit" in q else default
        offset = int(q["offset"]) if "offset" in q else 0
    except ValueError:
        raise _bad("limit/offset 은 정수여야 합니다.", "limit") from None
    if not 1 <= limit <= mx:
        raise _bad(f"limit 은 1..{mx} 이어야 합니다.", "limit")
    if offset < 0:
        raise _bad("offset 은 0 이상이어야 합니다.", "offset")
    return limit, offset


def _parse_time(q: str | None, name: str) -> str | None:
    if q is None:
        return None
    try:
        return iso(parse_iso(q))
    except ValueError:
        raise _bad(f"{name} 는 ISO 8601 시각이어야 합니다.", name) from None


# ====================================================================== E1
@router.get("/v1/health")
def health(request: Request) -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "schema_version": SCHEMA_VERSION}


# ====================================================================== E2
@router.post("/v1/files", status_code=201)
async def upload_file(request: Request, file: UploadFile = File(...), purpose: str = Form(...),
                      p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    rec = await run_in_threadpool(ctx.files.save_upload, file.file, file.filename or "upload", purpose, p.name)
    return {"schema_version": SCHEMA_VERSION, "file_id": rec["file_id"], "purpose": rec["purpose"], "filename": rec["filename"],
            "bytes": rec["bytes"], "sha256": rec["sha256"], "created_at": rec["created_at"]}


# ====================================================================== E3
def _create_job(ctx: AppContext, raw: bytes, idem_key: str | None, principal: Principal) -> tuple[int, dict[str, Any]]:
    body = parse_json_body(raw)
    ttl = float(ctx.config.get("api.idempotency_ttl_s"))
    body_hash = sha256_json(body) if idem_key is not None else None
    if idem_key is not None:
        existing = ctx.store.find_idempotent(idem_key, body_hash, ttl)  # type: ignore[arg-type]
        if existing is not None:
            return 200, views.job_created(existing)
    job_id = new_job_id()
    vr = validate_request(body, ctx.validation, job_id)
    work = ctx.artifacts.work_root("job", job_id)
    inputs = work / "inputs"
    try:
        (inputs / "runs").mkdir(parents=True, exist_ok=True)
        (inputs / "request.json").write_text(json.dumps(vr.request, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        for fname, content in run_json_files(vr.runs).items():
            (inputs / "runs" / fname).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8", newline="\n")
        for fid, src in vr.stp_files.items():
            (inputs / "stp").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, inputs / "stp" / f"{fid}.stp")
        row = ctx.store.create_job(
            job_id=job_id, name=vr.request.get("name"), created_by=principal.name, schema_version=SCHEMA_VERSION,
            request_hash=vr.request_hash, request=vr.request, spec=vr.snapshot, warnings=vr.warnings, material_id=vr.material_id,
            tests=vr.tests, idempotency_key=idem_key, idempotency_hash=body_hash,
        )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return 201, views.job_created(row)


@router.post("/v1/calibrations")
async def create_calibration(request: Request, p: Principal = Depends(service_principal)) -> JSONResponse:
    ctx = get_ctx(request)
    key = request.headers.get("idempotency-key")
    if key is not None and not 1 <= len(key) <= 128:
        raise _bad("Idempotency-Key 는 1..128자여야 합니다.", "Idempotency-Key")
    raw = await request.body()
    status, body = await run_in_threadpool(_create_job, ctx, raw, key, p)
    return JSONResponse(status_code=status, content=body)


# ====================================================================== E4
@router.get("/v1/calibrations")
def list_calibrations(request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    limit, offset = page_params(request, ctx)
    q = request.query_params
    statuses = None
    if q.get("status"):
        statuses = [s.strip() for s in q["status"].split(",") if s.strip()]
        bad = [s for s in statuses if s not in ALL_STATUSES]
        if bad:
            raise _bad(f"알 수 없는 status: {bad}", "status")
    mid = q.get("material_id")
    if mid is not None and len(mid) != 36:
        raise _bad("material_id 는 UUID 여야 합니다.", "material_id")
    rows, total = ctx.store.list_jobs(
        statuses=statuses, created_from=_parse_time(q.get("created_from"), "created_from"),
        created_to=_parse_time(q.get("created_to"), "created_to"), material_id=mid, test_type=q.get("test_type"),
        recipe_id=q.get("recipe_id"), limit=limit, offset=offset,
    )
    items = [views.job_summary(r, ctx.store.job_tests(r["job_id"])) for r in rows]
    return views.page(items, total, limit, offset)


def _job_or_404(ctx: AppContext, job_id: str) -> dict[str, Any]:
    row = ctx.store.get_job(job_id)
    if row is None:
        raise JobNotFound(job_id)
    return row


# ====================================================================== E5
@router.get("/v1/calibrations/{job_id}")
def get_calibration(job_id: str, request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    return views.job_status(_job_or_404(ctx, job_id), ctx.clock())


# ====================================================================== E6
@router.get("/v1/calibrations/{job_id}/result")
def get_result(job_id: str, request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    row = _job_or_404(ctx, job_id)
    if row["status"] != SUCCEEDED:
        raise ApiError(409, E.JOB_NOT_SUCCEEDED, "잡이 아직 성공하지 않았습니다.",
                       [{"code": E.JOB_NOT_SUCCEEDED, "message": f"현재 상태: {row['status']}", "status": row["status"]}])
    result = ctx.store.get_result(job_id) or {}
    result.setdefault("schema_version", row["result_schema_version"] or SCHEMA_VERSION)
    return result


# ====================================================================== E7
@router.get("/v1/calibrations/{job_id}/history")
def get_history(job_id: str, request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    _job_or_404(ctx, job_id)
    limit, offset = page_params(request, ctx)
    items, total = ctx.store.history_page(job_id, stage=request.query_params.get("stage"), limit=limit, offset=offset)
    return views.page(items, total, limit, offset)


# ====================================================================== E8
@router.post("/v1/calibrations/{job_id}/cancel", status_code=202)
def cancel_calibration(job_id: str, request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    row = ctx.store.request_cancel(job_id)
    return views.job_status(row, ctx.clock())


# ====================================================================== E9
@router.get("/v1/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, request: Request, p: Principal = Depends(service_principal)) -> Response:
    ctx = get_ctx(request)
    rec, path = ctx.artifacts.resolve_for_download(artifact_id)
    etag = f'"sha256:{rec.sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    name = rec.filename
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    disposition = f'attachment; filename="{ascii_name}"'
    if ascii_name != name:
        disposition += f"; filename*=UTF-8''{quote(name)}"
    return FileResponse(path, media_type=rec.media_type, headers={"Content-Disposition": disposition, "ETag": etag})


# ====================================================================== E10–E12
@router.get("/v1/recipes")
def list_recipes(request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    limit, offset = page_params(request, ctx)
    rows, total = ctx.recipes.list_active(test_type=request.query_params.get("test_type"), limit=limit, offset=offset)
    return views.page([views.recipe_public_summary(r) for r in rows], total, limit, offset)


@router.get("/v1/recipes/{recipe_id}/versions/{version}")
def get_recipe(recipe_id: str, version: int, request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    row = get_ctx(request).recipes.get_best(recipe_id, version)
    if row is None or row["status"] not in ("active", "retired"):
        raise RecipeNotFound(f"{recipe_id}@{version}")
    return views.recipe_public(row)


def _caps_json(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _caps_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (set, frozenset)):
        return sorted(_caps_json(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): _caps_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_caps_json(x) for x in obj]
    return obj


def _capabilities(reg: Any, entry: Any) -> Any:
    try:
        cls = reg.plugin_class(entry.type, entry.name)
        if entry.type in ("solver", "material_model"):
            return _caps_json(reg.create(entry.type, entry.name, {}).capabilities())
        if entry.type == "test_spec":
            inst = reg.create(entry.type, entry.name, {})
            return _caps_json({"matnexus_test_type_keys": list(cls.matnexus_test_type_keys),
                               "allowed_geometries": list(inst.allowed_geometries()),
                               "required_channels": [list(g) for g in inst.required_channels()],
                               "required_dimensions": list(inst.required_dimensions())})
        if entry.type == "optimizer":
            return {"max_batch": cls.max_batch}
        if entry.type == "geometry_mesher":
            return {"role": cls.role}
    except Exception:  # noqa: BLE001 - 능력 조회 실패는 null (목록 자체는 준다)
        return None
    return None


@router.get("/v1/registry")
def get_registry(request: Request, p: Principal = Depends(service_principal)) -> dict[str, Any]:
    reg = get_ctx(request).registry
    out: dict[str, list[dict[str, Any]]] = {}
    for e in reg.entries(enabled_only=True):
        out.setdefault(e.type, []).append({
            "name": e.name, "version": e.version, "aliases": sorted({a.name for a in e.aliases}),
            "capabilities": _capabilities(reg, e),
        })
    return {"schema_version": SCHEMA_VERSION, "plugins": out}


# ====================================================================== A1–A4
@admin.post("/recipes")
async def register_recipe(request: Request, bundle: UploadFile = File(...), strict: bool = Form(False),
                          p: Principal = Depends(admin_principal)) -> JSONResponse:
    ctx = get_ctx(request)
    limit = int(ctx.config.get("recipes.max_bundle_bytes"))
    data = await bundle.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed([make_detail(E.RECIPE_BUNDLE_INVALID, f"번들이 너무 큽니다 (최대 {limit} bytes).", field="bundle")])
    out = await run_in_threadpool(ctx.recipes.register_bundle, data, strict=strict, created_by=p.name, registry=ctx.registry)
    warnings = [w.to_detail() for w in out.warnings]
    return JSONResponse(status_code=202 if out.created else 200, content=views.recipe_registered(out.row, warnings))


@admin.get("/recipes")
def list_admin_recipes(request: Request, p: Principal = Depends(admin_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    limit, offset = page_params(request, ctx)
    q = request.query_params
    statuses = [s for s in (q.get("status") or "").split(",") if s] or None
    rows, total = ctx.recipes.list_admin(statuses=statuses, test_type=q.get("test_type"), recipe_id=q.get("recipe_id"),
                                         limit=limit, offset=offset)
    return views.page([views.recipe_admin_summary(r) for r in rows], total, limit, offset)


@admin.get("/recipes/{recipe_id}/versions/{version}")
def get_admin_recipe(recipe_id: str, version: int, request: Request, p: Principal = Depends(admin_principal)) -> dict[str, Any]:
    repo = get_ctx(request).recipes
    row = repo.get_best(recipe_id, version)
    if row is None:
        raise RecipeNotFound(f"{recipe_id}@{version}")
    return views.recipe_admin_detail(row, repo.attempts(recipe_id, version))


@admin.post("/recipes/{recipe_id}/versions/{version}/retire")
async def retire_recipe(recipe_id: str, version: int, request: Request, p: Principal = Depends(admin_principal)) -> dict[str, Any]:
    ctx = get_ctx(request)
    raw = await request.body()
    body = parse_json_body(raw)
    if not isinstance(body, dict):
        raise _bad("본문은 JSON 객체여야 합니다.")
    try:
        req = RetireRequest.model_validate(body)
    except Exception as e:  # noqa: BLE001 - pydantic ValidationError
        errs = getattr(e, "errors", lambda: [])()
        raise ApiError(400, E.VALIDATION_FAILED, "요청 검증 실패",
                       [schema_detail(er["msg"], ".".join(str(x) for x in er["loc"])) for er in errs] or [schema_detail(str(e))]) from None
    row = await run_in_threadpool(ctx.recipes.retire, recipe_id, version, req.reason)
    return views.recipe_admin_summary(row) | {"schema_version": SCHEMA_VERSION}
