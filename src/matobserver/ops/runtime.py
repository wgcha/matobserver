"""서비스(서버·워커) 실행 보조: PID 파일, 정지 요청 파일, 프로세스 확인·종료, 파일 로그 (deploy.md §1.8, §2.1).

* PID 파일 `<runtime.pid_dir>/{server,worker}.pid` = JSON `{pid, started_at, version, mode}`.
* 정상 종료 요청은 `<pid_dir>/<service>.stop` 파일이다(Windows 에는 분리 프로세스에 보낼 SIGTERM 이 없어 OS 공통으로 쓴다).
  서비스가 0.5초마다 확인하고 스스로 멈춘다. 시간이 지나도 안 멈추면 `kill_process` 가 강제 종료한다.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import CONTRACT_SCHEMA_VERSION, __version__  # noqa: F401 (CONTRACT_SCHEMA_VERSION 재노출)
from ..core.config import Config

SERVICES = ("server", "worker")
RUN_MODE_ENV = "MATOBSERVER_RUN_MODE"


def pid_dir(cfg: Config) -> Path:
    v = cfg.get("runtime.pid_dir")
    return Path(v).expanduser() if v else cfg.path("paths.data_dir") / "run"


def pid_path(cfg: Config, service: str) -> Path:
    return pid_dir(cfg) / f"{service}.pid"


def stop_path(cfg: Config, service: str) -> Path:
    return pid_dir(cfg) / f"{service}.stop"


def log_dir(cfg: Config) -> Path:
    return cfg.path("logging.dir")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_pidfile(path: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("pid"), int) else None


def write_pidfile(path: Path, pid: int, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "started_at": now_iso(), "version": __version__, "mode": mode}), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 좀비(종료했지만 부모가 거두지 않음)는 살아 있지 않은 것으로 본다
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def service_state(cfg: Config, service: str) -> dict[str, Any]:
    """`{state: running|stopped|stale_pid, pid, mode, started_at}`."""
    info = read_pidfile(pid_path(cfg, service))
    if info is None:
        return {"state": "stopped", "pid": None, "mode": None, "started_at": None}
    alive = pid_alive(info["pid"])
    return {"state": "running" if alive else "stale_pid", "pid": info["pid"], "mode": info.get("mode"),
            "started_at": info.get("started_at")}


def kill_process(pid: int, *, run: Callable[..., Any] = subprocess.run) -> None:
    """강제 종료 (자식 포함)."""
    if sys.platform == "win32":  # pragma: no cover - Windows
        run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class ServiceLifecycle:
    """서비스 프로세스 안에서: PID 파일 기록·정리, 정지 요청 감시, (선택) 하트비트."""

    def __init__(self, cfg: Config, service: str, mode: str, on_stop: Callable[[], None],
                 heartbeat: Callable[[], None] | None = None, heartbeat_s: float = 10.0, poll_s: float = 0.5) -> None:
        self.cfg, self.service, self.mode, self.on_stop = cfg, service, mode, on_stop
        self.heartbeat, self.heartbeat_s, self.poll_s = heartbeat, heartbeat_s, poll_s
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ServiceLifecycle":
        stop_path(self.cfg, self.service).unlink(missing_ok=True)
        write_pidfile(pid_path(self.cfg, self.service), os.getpid(), self.mode)
        if self.heartbeat:
            self._beat()
        self._thread = threading.Thread(target=self._loop, name=f"{self.service}-lifecycle", daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        try:
            assert self.heartbeat is not None
            self.heartbeat()
        except Exception:  # noqa: BLE001 - 하트비트 실패가 서비스를 죽이면 안 된다
            logging.getLogger("matobserver.ops").exception("heartbeat failed")

    def _loop(self) -> None:
        import time

        next_beat = time.monotonic() + self.heartbeat_s
        stopped = False
        while not self._done.wait(self.poll_s):
            if not stopped and stop_path(self.cfg, self.service).exists():
                stopped = True
                self.on_stop()
            if self.heartbeat and time.monotonic() >= next_beat:
                next_beat = time.monotonic() + self.heartbeat_s
                self._beat()

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        if self._thread:
            self._thread.join(timeout=5)
        info = read_pidfile(pid_path(self.cfg, self.service))
        if info and info["pid"] == os.getpid():
            pid_path(self.cfg, self.service).unlink(missing_ok=True)
        stop_path(self.cfg, self.service).unlink(missing_ok=True)


def setup_file_logging(cfg: Config, name: str) -> Path | None:
    """`<logging.dir>/<name>.log` 크기 회전 로그를 루트 로거에 붙인다 (deploy.md §1.8). 실패하면 None."""
    try:
        d = log_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.log"
        h = logging.handlers.RotatingFileHandler(path, maxBytes=int(cfg.get("logging.max_bytes")),
                                                 backupCount=int(cfg.get("logging.backups")), encoding="utf-8")
    except OSError:
        return None
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    if root.level == logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return path
