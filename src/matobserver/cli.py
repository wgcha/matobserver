"""명령행: `matobserver serve | worker | recipe add|list|retire` (module-boundaries.md §1, recipe.md §6).

레시피 명령은 서버 API(관리자 토큰)를 호출한다. 토큰은 환경변수 `MATOBSERVER_ADMIN_TOKEN` 또는 `--token-file` 로만 받는다(명령행 평문 토큰 없음).

`recipe` 종료 코드 (recipe.md §6):
    0 등록·검증 통과(또는 비대기 접수) / 목록 / 폐기 성공
    2 정적 검사 실패 (로컬 또는 서버 400)
    3 검증 rejected
    4 409 충돌 (`RECIPE_VERSION_CONFLICT`, `RECIPE_NOT_RETIRABLE`)
    5 통신·인증 오류 (연결 실패, 401/403, 5xx, `--wait` 시간 초과, 토큰 없음)
그 외 사용법·설정 오류는 1.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

import httpx

from . import CONTRACT_SCHEMA_VERSION, __version__
from .core.config import Config, load_config
from .core.errors import ConfigError, RegistryError
from .core.registry import Registry
from .recipes.bundle import content_hash
from .recipes.static_check import check_folder
from .ops import service as ops_service
from .ops.doctor import run_doctor
from .ops.runtime import RUN_MODE_ENV, ServiceLifecycle, setup_file_logging
from .ops.service import OpsEnv
from .validation.hooks import EngineHooks

EXIT_OK, EXIT_USAGE, EXIT_STATIC, EXIT_REJECTED, EXIT_CONFLICT, EXIT_COMM = 0, 1, 2, 3, 4, 5
TOKEN_ENV = "MATOBSERVER_ADMIN_TOKEN"
_REF_RE = re.compile(r"^([a-z0-9][a-z0-9-]{2,63})@([1-9][0-9]*)$")

ClientFactory = Callable[[str, str, float | None], httpx.Client]


class _Ctx:
    def __init__(self, out: TextIO, err: TextIO, env: Mapping[str, str], client_factory: ClientFactory | None, stdin: TextIO) -> None:
        self.out, self.err, self.env, self.client_factory, self.stdin = out, err, env, client_factory, stdin


def _default_client(base_url: str, token: str, timeout: float | None) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(timeout))


# ---------------------------------------------------------------------- 출력
def _table(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> str:
    cells = [[str(c) for c in header]] + [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells)


def _print_json(ctx: _Ctx, obj: Any) -> None:
    ctx.out.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _print_issues(ctx: _Ctx, issues: Sequence[Mapping[str, Any]]) -> None:
    for i in issues:
        loc = f" [{i['field']}]" if i.get("field") else ""
        ctx.err.write(f"  {i['code']}{loc}: {i['message']}\n")


# ---------------------------------------------------------------------- 공통
def _load(args: argparse.Namespace, ctx: _Ctx) -> Config:
    return load_config(args.config, env=ctx.env)


def _token(args: argparse.Namespace, ctx: _Ctx) -> str | None:
    if getattr(args, "token_file", None):
        try:
            return Path(args.token_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            ctx.err.write("--token-file 을 읽을 수 없습니다.\n")  # 파일 이름만 틀려도 토큰 값을 에코하지 않는다
            return None
    t = ctx.env.get(TOKEN_ENV, "").strip()
    return t or None


def _client(args: argparse.Namespace, ctx: _Ctx, cfg: Config) -> httpx.Client | None:
    token = _token(args, ctx)
    if token is None:
        ctx.err.write(f"관리자 토큰이 없습니다. 환경변수 {TOKEN_ENV} 또는 --token-file 을 쓰세요.\n")
        return None
    url = args.server or cfg.get("cli.server_url")
    factory = ctx.client_factory or _default_client
    return factory(url, token, args.http_timeout)


def _error_of(resp: httpx.Response) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        e = resp.json().get("error", {})
        return str(e.get("code", "")), str(e.get("message", "")), list(e.get("details") or [])
    except (ValueError, AttributeError):
        return "", resp.text[:200], []


def _report_http_error(ctx: _Ctx, resp: httpx.Response) -> int:
    code, msg, details = _error_of(resp)
    ctx.err.write(f"HTTP {resp.status_code} {code}: {msg}\n")
    _print_issues(ctx, details)
    if resp.status_code == 400 and code == "VALIDATION_FAILED":
        return EXIT_STATIC
    if resp.status_code == 409:
        return EXIT_CONFLICT
    return EXIT_COMM


# ---------------------------------------------------------------------- recipe add
def _zip_folder(folder: Path) -> bytes:
    """결정적 zip (경로 사전순, 고정 시각). 서버가 해시를 다시 계산하므로 압축 방식은 해시에 영향이 없다."""
    buf = io.BytesIO()
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zi = zipfile.ZipInfo(p.relative_to(folder).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, p.read_bytes())
    return buf.getvalue()


def _cmd_recipe_add(args: argparse.Namespace, ctx: _Ctx) -> int:
    cfg = _load(args, ctx)
    folder = Path(args.folder)
    if not folder.is_dir():
        ctx.err.write(f"폴더가 아닙니다: {folder}\n")
        return EXIT_STATIC
    registry: Registry | None
    try:
        registry = Registry.from_config(cfg, check_classes=False)
    except RegistryError as e:
        ctx.err.write(f"경고: 레지스트리를 읽지 못해 레지스트리 관련 검사는 서버에서만 합니다 ({e}).\n")
        registry = None
    res = check_folder(folder, registry)
    blocking = res.errors + (res.warnings if args.strict else [])
    for w in res.warnings:
        ctx.err.write(f"경고 {w.code}: {w.message}\n")
    if blocking:
        ctx.err.write(f"로컬 정적 검사 실패 {len(blocking)}건:\n")
        _print_issues(ctx, [i.to_detail() for i in blocking])
        return EXIT_STATIC
    local_hash = content_hash(res.entries)
    ctx.err.write(f"로컬 content_hash: {local_hash}\n")
    client = _client(args, ctx, cfg)
    if client is None:
        return EXIT_COMM
    data = _zip_folder(folder)
    try:
        with client:
            resp = client.post("/v1/admin/recipes", files={"bundle": ("recipe.zip", data, "application/zip")},
                               data={"strict": "true" if args.strict else "false"})
            if resp.status_code not in (200, 202):
                return _report_http_error(ctx, resp)
            reg = resp.json()
            if reg.get("content_hash") != local_hash:
                ctx.err.write(f"경고: 서버 content_hash({reg.get('content_hash')}) 가 로컬 값과 다릅니다.\n")
            if not args.wait:
                if args.json:
                    _print_json(ctx, reg)
                else:
                    ctx.out.write(f"{reg['recipe_ref']}  {reg['status']}  {reg['content_hash']}  {reg['verification_id']}\n")
                return EXIT_OK
            return _wait_for_verification(args, ctx, cfg, client, reg)
    except httpx.HTTPError as e:
        ctx.err.write(f"서버와 통신할 수 없습니다: {type(e).__name__}\n")
        return EXIT_COMM


def _wait_for_verification(args: argparse.Namespace, ctx: _Ctx, cfg: Config, client: httpx.Client, reg: Mapping[str, Any]) -> int:
    path = f"/v1/admin/recipes/{reg['recipe_id']}/versions/{reg['version']}"
    interval = float(cfg.get("cli.poll_interval_s"))
    deadline = None if args.timeout is None else time.monotonic() + float(args.timeout)
    while True:
        resp = client.get(path)
        if resp.status_code != 200:
            return _report_http_error(ctx, resp)
        detail = resp.json()
        if detail["status"] != "verifying":
            break
        if deadline is not None and time.monotonic() >= deadline:
            ctx.err.write("검증 완료를 기다리다 시간이 초과되었습니다.\n")
            return EXIT_COMM
        time.sleep(interval)
    if args.json:
        _print_json(ctx, detail)
    else:
        ctx.out.write(f"{detail['recipe_ref']}  {detail['status']}  경고 {detail['warnings_count']}건\n")
        for chk in (detail.get("verification") or {}).get("checks", []):
            extra = chk.get("detail") or (f"값 {chk['value']}" if "value" in chk else "")
            ctx.out.write(f"  [{chk['result']}] {chk['code']} {extra}\n".rstrip() + "\n")
        err = (detail.get("verification") or {}).get("error")
        if err:
            ctx.out.write(f"  오류 {err.get('code')}: {err.get('message')}\n")
    return EXIT_REJECTED if detail["status"] == "rejected" else EXIT_OK


# ---------------------------------------------------------------------- recipe list / retire
def _cmd_recipe_list(args: argparse.Namespace, ctx: _Ctx) -> int:
    cfg = _load(args, ctx)
    client = _client(args, ctx, cfg)
    if client is None:
        return EXIT_COMM
    params: dict[str, Any] = {}
    if args.status:
        params["status"] = ",".join(args.status)
    for k, v in (("test_type", args.test_type), ("recipe_id", args.recipe_id), ("limit", args.limit), ("offset", args.offset)):
        if v is not None:
            params[k] = v
    try:
        with client:
            resp = client.get("/v1/admin/recipes", params=params)
    except httpx.HTTPError as e:
        ctx.err.write(f"서버와 통신할 수 없습니다: {type(e).__name__}\n")
        return EXIT_COMM
    if resp.status_code != 200:
        return _report_http_error(ctx, resp)
    body = resp.json()
    if args.json:
        _print_json(ctx, body)
    else:
        rows = [(r["recipe_ref"], r["kind"], r["test_type"], r["status"], r["warnings_count"], r["created_at"]) for r in body["items"]]
        ctx.out.write(_table(rows, ("recipe_ref", "kind", "test_type", "status", "warnings", "created_at")) + "\n")
        ctx.out.write(f"총 {body['total']}건\n")
    return EXIT_OK


def _cmd_recipe_retire(args: argparse.Namespace, ctx: _Ctx) -> int:
    m = _REF_RE.match(args.recipe_ref)
    if not m:
        ctx.err.write("recipe_id@version 형식이어야 합니다 (예: flex3p-d790-utm01@3).\n")
        return EXIT_USAGE
    cfg = _load(args, ctx)
    if not args.yes:
        ctx.err.write(f"{args.recipe_ref} 를 폐기합니다. 계속하려면 y: ")
        ctx.err.flush()
        if ctx.stdin.readline().strip().lower() not in ("y", "yes"):
            ctx.err.write("취소했습니다.\n")
            return EXIT_USAGE
    client = _client(args, ctx, cfg)
    if client is None:
        return EXIT_COMM
    try:
        with client:
            resp = client.post(f"/v1/admin/recipes/{m.group(1)}/versions/{m.group(2)}/retire",
                               json={"schema_version": "1.0", "reason": args.reason})
    except httpx.HTTPError as e:
        ctx.err.write(f"서버와 통신할 수 없습니다: {type(e).__name__}\n")
        return EXIT_COMM
    if resp.status_code != 200:
        return _report_http_error(ctx, resp)
    body = resp.json()
    if args.json:
        _print_json(ctx, body)
    else:
        ctx.out.write(f"{body['recipe_ref']}  {body['status']}  {body.get('retired_at') or ''}\n")
    return EXIT_OK


# ---------------------------------------------------------------------- serve / worker
def _run_mode(args: argparse.Namespace, ctx: _Ctx) -> str:
    return getattr(args, "run_mode", None) or ctx.env.get(RUN_MODE_ENV) or "process"


def _cmd_serve(args: argparse.Namespace, ctx: _Ctx, runner: Callable[..., Any] | None) -> int:
    from .api.app import create_app
    from .jobs.db import SchemaOutdated, SchemaTooNew

    try:
        cfg = _load(args, ctx)
        registry = Registry.from_config(cfg)
        app = create_app(cfg, registry)
    except (ConfigError, RegistryError, SchemaOutdated, SchemaTooNew) as e:
        ctx.err.write(f"기동 실패: {e}\n")
        return EXIT_USAGE
    host, port = args.host or cfg.get("api.host"), args.port or cfg.get("api.port")
    if runner is not None:  # 시험용 주입: PID 파일·로그 파일 없이 실행
        runner(app, host=host, port=int(port))
        return EXIT_OK
    import uvicorn

    setup_file_logging(cfg, "server")
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=int(port), log_config=None))

    def request_stop() -> None:
        server.should_exit = True

    with ServiceLifecycle(cfg, "server", _run_mode(args, ctx), request_stop):
        server.run()
    return EXIT_OK


def _cmd_worker(args: argparse.Namespace, ctx: _Ctx, hooks: EngineHooks | None) -> int:
    from .api.context import build_context
    from .jobs.db import SchemaOutdated, SchemaTooNew
    from .jobs.worker import Worker, WorkerLock

    try:
        cfg = _load(args, ctx)
        registry = Registry.from_config(cfg)
        actx = build_context(cfg, registry, hooks=hooks, discover_hooks=hooks is None)
    except (ConfigError, RegistryError, SchemaOutdated, SchemaTooNew) as e:
        ctx.err.write(f"기동 실패: {e}\n")
        return EXIT_USAGE
    lock = WorkerLock(cfg.path("paths.data_dir") / "worker.lock")
    if not lock.acquire():
        ctx.err.write("다른 워커가 이미 실행 중입니다 (워커는 1개만 둡니다).\n")
        return EXIT_USAGE
    try:
        worker = Worker(config=cfg, registry=registry, store=actx.store, recipes=actx.recipes, artifacts=actx.artifacts,
                        hooks=actx.hooks, clock=actx.clock)
        if args.once:
            worker.recover()
            while worker.run_once():
                pass
            return EXIT_OK
        stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
        setup_file_logging(cfg, "worker")
        with ServiceLifecycle(cfg, "worker", _run_mode(args, ctx), stop.set,
                              heartbeat=lambda: actx.store.beat_worker(os.getpid()),
                              heartbeat_s=float(cfg.get("worker.heartbeat_s"))):
            worker.run_forever(stop, idle_sleep_s=args.idle_sleep)
        return EXIT_OK
    finally:
        lock.release()


# ---------------------------------------------------------------------- 운영 명령 (deploy.md §2)
def _ops_cfg(args: argparse.Namespace, ctx: _Ctx) -> tuple[Config, Path | None] | None:
    try:
        cfg = _load(args, ctx)
    except ConfigError as e:
        ctx.err.write(f"설정 오류: {e}\n")
        return None
    return cfg, cfg.source_path


def _emit(ctx: _Ctx, args: argparse.Namespace, report: Mapping[str, Any], table: Callable[[Mapping[str, Any]], str]) -> None:
    if getattr(args, "json", False):
        _print_json(ctx, report)
    else:
        ctx.out.write(table(report) + "\n")


def _flat_table(report: Mapping[str, Any]) -> str:
    rows: list[tuple[str, str]] = []

    def walk(prefix: str, v: Any) -> None:
        if isinstance(v, Mapping):
            for k, x in v.items():
                walk(f"{prefix}.{k}" if prefix else str(k), x)
        else:
            rows.append((prefix, "" if v is None else str(v)))

    walk("", report)
    return _table(rows, ("항목", "값"))


def _cmd_start(args: argparse.Namespace, ctx: _Ctx, env: OpsEnv) -> int:
    loaded = _ops_cfg(args, ctx)
    if loaded is None:
        return EXIT_USAGE
    code, report = ops_service.start(loaded[0], loaded[1], args.service, args.mode, env)
    _emit(ctx, args, report, _flat_table)
    return code


def _cmd_stop(args: argparse.Namespace, ctx: _Ctx, env: OpsEnv) -> int:
    loaded = _ops_cfg(args, ctx)
    if loaded is None:
        return EXIT_USAGE
    code, report = ops_service.stop(loaded[0], args.service, args.timeout, args.force, env)
    _emit(ctx, args, report, _flat_table)
    return code


def _cmd_status(args: argparse.Namespace, ctx: _Ctx, env: OpsEnv) -> int:
    loaded = _ops_cfg(args, ctx)
    if loaded is None:
        return EXIT_USAGE
    code, report = ops_service.status(loaded[0], loaded[1], env)
    _emit(ctx, args, report, _flat_table)
    return code


def _doctor_table(report: Mapping[str, Any]) -> str:
    rows = [(c["id"], c["name"], c["status"], c["detail"]) for c in report["checks"]]
    s = report["summary"]
    return (_table(rows, ("ID", "항목", "상태", "상세"))
            + f"\n\nOK {s['ok']}  WARN {s['warn']}  FAIL {s['fail']}  SKIP {s['skip']}")


def _cmd_doctor(args: argparse.Namespace, ctx: _Ctx, env: OpsEnv) -> int:
    unknown: list[str] = []
    cfg: Config | None = None
    err: str | None = None
    try:
        cfg = load_config(args.config, env=ctx.env, unknown=unknown)
    except ConfigError as e:
        err = str(e)
    code, report = run_doctor(cfg, unknown, cfg.source_path if cfg else None, env, load_error=err)
    _emit(ctx, args, report, _doctor_table)
    return code


def _cmd_db_migrate(args: argparse.Namespace, ctx: _Ctx) -> int:
    from .jobs.db import SchemaTooNew, migrate

    loaded = _ops_cfg(args, ctx)
    if loaded is None:
        return EXIT_USAGE
    path = loaded[0].path("jobstore.sqlite_path")
    try:
        res = migrate(path, backup=not args.no_backup)
    except SchemaTooNew as e:
        ctx.err.write(f"{e}\n")
        return 2
    except Exception as e:  # noqa: BLE001 - 롤백된 실패
        ctx.err.write(f"마이그레이션 실패(롤백됨): {type(e).__name__}: {e}\n")
        return 1
    report = {"from_version": res.from_version, "to_version": res.to_version, "backup": None if res.backup is None else str(res.backup),
              "changed": res.from_version != res.to_version}
    _emit(ctx, args, report, _flat_table)
    return EXIT_OK


# ---------------------------------------------------------------------- 진입점
class _Parser(argparse.ArgumentParser):
    """사용법 오류는 argparse 기본 종료 코드 2 가 아니라 1 로 낸다 (2 는 `recipe add` 의 정적 검사 실패와 겹친다)."""

    def error(self, message: str):  # type: ignore[no-untyped-def]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 오류: {message}\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="matobserver", description="MatNexus용 재료 역공학(FEMU) 엔진", allow_abbrev=False)
    p.add_argument("--version", action="version", version=f"matobserver {__version__} (contract schema_version {CONTRACT_SCHEMA_VERSION})")
    p.add_argument("--config", default=None, help="설정 파일 (기본: MATOBSERVER_CONFIG 또는 ./config/matobserver.yaml)")
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    # `--config` 는 하위 명령 뒤에도 받는다 (작업 스케줄러 명령행 `matobserver serve --config <설정>`, deploy.md §1.6)
    cp = argparse.ArgumentParser(add_help=False)
    cp.add_argument("--config", default=argparse.SUPPRESS, help="설정 파일 (기본: MATOBSERVER_CONFIG 또는 ./config/matobserver.yaml)")
    sp_ = argparse.ArgumentParser(add_help=False, parents=[cp])
    sp_.add_argument("--run-mode", choices=("scheduler", "process"), default=None, help=argparse.SUPPRESS)

    s = sub.add_parser("serve", help="REST API 서버", allow_abbrev=False, parents=[sp_])
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    w = sub.add_parser("worker", help="잡 워커 (1개만 실행)", allow_abbrev=False, parents=[sp_])
    w.add_argument("--once", action="store_true", help="큐를 비울 때까지 처리하고 종료")
    w.add_argument("--idle-sleep", type=float, default=1.0, help="큐가 비었을 때 대기(s)")

    st = sub.add_parser("start", help="서버·워커 시작", allow_abbrev=False, parents=[cp])
    st.add_argument("--service", choices=("server", "worker", "all"), default="all")
    st.add_argument("--mode", choices=("auto", "scheduler", "process"), default="auto")
    st.add_argument("--json", action="store_true")
    sp = sub.add_parser("stop", help="서버·워커 정지", allow_abbrev=False, parents=[cp])
    sp.add_argument("--service", choices=("server", "worker", "all"), default="all")
    sp.add_argument("--timeout", type=float, default=30.0, help="정상 종료 대기(s), 이후 강제 종료")
    sp.add_argument("--force", action="store_true", help="잡 실행 중이어도 정지")
    sp.add_argument("--json", action="store_true")
    ss_ = sub.add_parser("status", help="실행 상태", allow_abbrev=False, parents=[cp])
    ss_.add_argument("--json", action="store_true")
    dr = sub.add_parser("doctor", help="환경 점검 D01-D11", allow_abbrev=False, parents=[cp])
    dr.add_argument("--json", action="store_true")
    db = sub.add_parser("db", help="JobStore DB", allow_abbrev=False)
    dbs = db.add_subparsers(dest="db_command", required=True, parser_class=_Parser)
    mg = dbs.add_parser("migrate", help="DB 스키마를 코드 버전까지 전진", allow_abbrev=False, parents=[cp])
    mg.add_argument("--no-backup", action="store_true")
    mg.add_argument("--json", action="store_true")

    r = sub.add_parser("recipe", help="레시피 관리 (관리자 토큰)", allow_abbrev=False)
    rs = r.add_subparsers(dest="recipe_command", required=True, parser_class=_Parser)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--server", help="서버 주소 (기본: 설정 cli.server_url)")
        sp.add_argument("--token-file", help=f"관리자 토큰 파일 (또는 환경변수 {TOKEN_ENV})")
        sp.add_argument("--json", action="store_true", help="API 응답 JSON 그대로 출력")
        sp.add_argument("--http-timeout", type=float, default=None, help="HTTP 요청 제한시간(s). 기본: 제한 없음")

    a = rs.add_parser("add", help="레시피 폴더 등록", allow_abbrev=False)
    common(a)
    a.add_argument("folder")
    a.add_argument("--strict", action="store_true", help="경고도 거절")
    a.add_argument("--wait", action="store_true", help="검증 완료까지 대기")
    a.add_argument("--timeout", type=float, default=None, help="--wait 제한시간(s)")
    ls = rs.add_parser("list", help="레시피 목록", allow_abbrev=False)
    common(ls)
    ls.add_argument("--status", action="append", help="상태 필터 (복수 가능)")
    ls.add_argument("--test-type")
    ls.add_argument("--recipe-id")
    ls.add_argument("--limit", type=int)
    ls.add_argument("--offset", type=int)
    rt = rs.add_parser("retire", help="레시피 폐기", allow_abbrev=False)
    common(rt)
    rt.add_argument("recipe_ref", help="recipe_id@version")
    rt.add_argument("--reason", required=True)
    rt.add_argument("--yes", action="store_true", help="확인 생략")
    return p


def _force_utf8_console() -> None:
    """Windows 콘솔(cp949 등)에서 한글 출력이 UnicodeEncodeError 가 되지 않게 표준 출력을 UTF-8 로 맞춘다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    client_factory: ClientFactory | None = None,
    hooks: EngineHooks | None = None,
    serve_runner: Callable[..., Any] | None = None,
    ops_env: OpsEnv | None = None,
) -> int:
    if stdout is None and stderr is None:
        _force_utf8_console()
    ctx = _Ctx(stdout or sys.stdout, stderr or sys.stderr, os.environ if env is None else env, client_factory, stdin or sys.stdin)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help/--version(0), 사용법 오류(1)
        return int(e.code) if isinstance(e.code, int) else EXIT_USAGE
    try:
        if args.command == "serve":
            return _cmd_serve(args, ctx, serve_runner)
        if args.command == "worker":
            return _cmd_worker(args, ctx, hooks)
        oenv = ops_env or OpsEnv(environ=ctx.env)
        if args.command == "start":
            return _cmd_start(args, ctx, oenv)
        if args.command == "stop":
            return _cmd_stop(args, ctx, oenv)
        if args.command == "status":
            return _cmd_status(args, ctx, oenv)
        if args.command == "doctor":
            return _cmd_doctor(args, ctx, oenv)
        if args.command == "db":
            return _cmd_db_migrate(args, ctx)
        if args.recipe_command == "add":
            return _cmd_recipe_add(args, ctx)
        if args.recipe_command == "list":
            return _cmd_recipe_list(args, ctx)
        return _cmd_recipe_retire(args, ctx)
    except ConfigError as e:
        ctx.err.write(f"설정 오류: {e}\n")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
