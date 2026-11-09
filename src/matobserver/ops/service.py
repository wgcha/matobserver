"""`matobserver start|stop|status` (deploy.md §2.1).

외부 효과(프로세스 기동·`schtasks`·HTTP·대기)는 `OpsEnv` 로 주입해 시험에서 대역을 쓴다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from .. import CONTRACT_SCHEMA_VERSION, __version__
from ..core.config import Config
from . import dbinfo
from .runtime import (RUN_MODE_ENV, SERVICES, kill_process, log_dir, pid_alive, pid_path, read_pidfile, service_state,
                      stop_path, write_pidfile)

TASK_NAMES = {"server": r"\matobserver\matobserver-server", "worker": r"\matobserver\matobserver-worker"}
EXIT_OK, EXIT_CONFIG, EXIT_PARTIAL, EXIT_ALL_STOPPED = 0, 1, 3, 4
EXIT_WORKER_BUSY = 4


def default_spawn(cmd: list[str], log_path: Path, env: Mapping[str, str]) -> Any:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "ab")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - Windows
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=dict(env), close_fds=True, **kwargs)
    finally:
        out.close()


def default_http_get(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 3.0) -> tuple[int | None, Any, float]:
    """(상태 코드 또는 None=연결 실패, JSON 본문 또는 None, 경과 초)."""
    t0 = time.monotonic()
    try:
        r = httpx.get(url, headers=dict(headers or {}), timeout=timeout)
    except httpx.HTTPError:
        return None, None, time.monotonic() - t0
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, time.monotonic() - t0


@dataclass
class OpsEnv:
    spawn: Callable[[list[str], Path, Mapping[str, str]], Any] = default_spawn
    run: Callable[..., Any] = subprocess.run
    http_get: Callable[..., tuple[int | None, Any, float]] = default_http_get
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    is_windows: bool = field(default_factory=lambda: sys.platform == "win32")
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)


def _services(sel: str) -> tuple[str, ...]:
    return SERVICES if sel == "all" else (sel,)


def server_url(cfg: Config) -> str:
    host = str(cfg.get("api.host"))
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.get('api.port')}"


def scheduler_task_exists(env: OpsEnv, service: str) -> bool:
    if not env.is_windows:
        return False
    try:
        return env.run(["schtasks", "/Query", "/TN", TASK_NAMES[service]], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _command(service: str, config_path: Path | None) -> list[str]:
    cmd = [sys.executable, "-m", "matobserver.cli", "serve" if service == "server" else "worker"]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    return cmd + ["--run-mode", "process"]


# ------------------------------------------------------------------------------ start
def start(cfg: Config, config_path: Path | None, service: str, mode: str, env: OpsEnv) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {}
    code = EXIT_OK
    for svc in _services(service):
        st = service_state(cfg, svc)
        if st["state"] == "running":
            report[svc] = {"result": "already_running", "pid": st["pid"], "mode": st["mode"]}
            continue
        if st["state"] == "stale_pid":
            pid_path(cfg, svc).unlink(missing_ok=True)
        use = mode
        if mode == "auto":
            use = "scheduler" if scheduler_task_exists(env, svc) else "process"
        if use == "scheduler":
            r = env.run(["schtasks", "/Run", "/TN", TASK_NAMES[svc]], capture_output=True, check=False)
            if r.returncode != 0:
                report[svc] = {"result": "failed", "mode": "scheduler", "detail": "schtasks /Run 실패"}
                code = EXIT_CONFIG
            else:
                report[svc] = {"result": "started", "mode": "scheduler"}
            continue
        e = dict(env.environ)
        e[RUN_MODE_ENV] = "process"
        try:
            proc = env.spawn(_command(svc, config_path), log_dir(cfg) / f"{svc}.console.log", e)
        except OSError as ex:
            report[svc] = {"result": "failed", "mode": "process", "detail": str(ex)}
            code = EXIT_CONFIG
            continue
        write_pidfile(pid_path(cfg, svc), proc.pid, "process")
        report[svc] = {"result": "started", "mode": "process", "pid": proc.pid}
    return code, report


# ------------------------------------------------------------------------------ stop
def stop(cfg: Config, service: str, timeout_s: float, force: bool, env: OpsEnv) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {}
    targets = _services(service)
    if "worker" in targets and not force:
        ws = service_state(cfg, "worker")
        q = dbinfo.queue_info(cfg.path("jobstore.sqlite_path"))
        if ws["state"] == "running" and q["running_job_id"]:
            report["worker"] = {"result": "refused", "detail": f"잡 {q['running_job_id']} 실행 중 — 끝난 뒤 멈추거나 --force 를 쓰세요"}
            return EXIT_WORKER_BUSY, report
    for svc in targets[::-1] if service == "all" else targets:  # all: 워커 먼저, 서버 나중
        st = service_state(cfg, svc)
        if st["state"] == "stopped":
            report[svc] = {"result": "not_running"}
            continue
        if st["state"] == "stale_pid":
            pid_path(cfg, svc).unlink(missing_ok=True)
            report[svc] = {"result": "stale_pid_removed", "pid": st["pid"]}
            continue
        pid = st["pid"]
        sp = stop_path(cfg, svc)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("stop", encoding="utf-8")
        deadline = env.monotonic() + max(timeout_s, 0.0)
        graceful = False
        while True:
            if not pid_alive(pid):
                graceful = True
                break
            if env.monotonic() >= deadline:
                break
            env.sleep(0.1)
        if not graceful:
            if st["mode"] == "scheduler" and env.is_windows:
                env.run(["schtasks", "/End", "/TN", TASK_NAMES[svc]], capture_output=True, check=False)
            kill_process(pid, run=env.run)
            for _ in range(50):
                if not pid_alive(pid):
                    break
                env.sleep(0.1)
        sp.unlink(missing_ok=True)
        info = read_pidfile(pid_path(cfg, svc))
        if info and info["pid"] == pid:
            pid_path(cfg, svc).unlink(missing_ok=True)
        report[svc] = {"result": "stopped" if graceful else "killed", "pid": pid}
    return EXIT_OK, report


# ------------------------------------------------------------------------------ status
def status(cfg: Config, config_path: Path | None, env: OpsEnv) -> tuple[int, dict[str, Any]]:
    db = cfg.path("jobstore.sqlite_path")
    url = server_url(cfg)
    ss = service_state(cfg, "server")
    health: Any = "unreachable"
    hver = hschema = None
    code, body, _ = env.http_get(f"{url}/v1/health", timeout=2.0)
    if code is not None:
        health = "ok" if code == 200 and isinstance(body, dict) and body.get("status") == "ok" else code
        if isinstance(body, dict):
            hver, hschema = body.get("version"), body.get("schema_version")
    ws = service_state(cfg, "worker")
    q = dbinfo.queue_info(db)
    report = {
        "version": __version__,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "config_path": None if config_path is None else str(config_path),
        "server": {"url": url, "state": ss["state"], "pid": ss["pid"], "mode": ss["mode"],
                   "health": health, "health_version": hver, "health_schema_version": hschema},
        "worker": {"state": ws["state"], "pid": ws["pid"], "mode": ws["mode"],
                   "last_heartbeat_age_s": dbinfo.heartbeat_age_s(db)},
        "queue": q,
        "recipes": {"active": dbinfo.active_recipes(db)},
    }
    up = (ss["state"] == "running") + (ws["state"] == "running")
    return {2: EXIT_OK, 1: EXIT_PARTIAL, 0: EXIT_ALL_STOPPED}[up], report
