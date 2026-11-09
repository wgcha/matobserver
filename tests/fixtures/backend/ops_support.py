"""운영 명령 시험 공용 (tests/unit/ops/conftest.py 가 불러온다): 임시 폴더 기반 설정·가짜 외부 효과 (deploy.md §2)."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from matobserver import cli
from matobserver.core.config import load_config
from matobserver.ops.service import OpsEnv

REPO = Path(__file__).resolve().parents[3]
VERSION_CMD = [sys.executable, "-c", "print('R13.1.1 d')"]


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def write_config(root: Path, **over) -> Path:
    """`over` 는 점 표기 키 -> 값. 기본은 doctor 가 전부 통과하는 설정."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    script = root / "bin" / "run_dyna.bat"
    script.write_text("@echo off\r\n", encoding="utf-8")
    flat = {
        "paths.data_dir": str(root / "data"), "paths.work_dir": str(root / "data" / "work"),
        "recipes.store_dir": str(root / "data" / "recipes"), "jobstore.sqlite_path": str(root / "data" / "db" / "jobstore.sqlite"),
        "logging.dir": str(root / "logs"), "runtime.pid_dir": str(root / "data" / "run"),
        "auth.service_tokens": [{"name": "svc", "sha256": sha("s")}], "auth.admin_tokens": [{"name": "adm", "sha256": sha("a")}],
        "solver.lsdyna.script": str(script), "solver.lsdyna.version_command": VERSION_CMD,
        "registry.paths": [str(REPO / "config" / "plugins.d")], "doctor.min_free_bytes": 1,
        "api.port": 18040, "heartbeat": None,
    }
    flat.pop("heartbeat")
    flat.update(over)
    nested: dict = {}
    for k, v in flat.items():
        node = nested
        parts = k.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = v
    path = root / "config" / "matobserver.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(nested), encoding="utf-8")
    return path


class FakeProc:
    def __init__(self, popen):
        self.p = popen
        self.pid = popen.pid


class Ops:
    """설정·가짜 spawn/run/http 를 묶은 시험 하네스."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.config_path = write_config(tmp_path)
        self.procs: list[subprocess.Popen] = []
        self.spawned: list[list[str]] = []
        self.run_calls: list[list[str]] = []
        self.run_rc: dict[str, int] = {}
        self.run_stdout = ""
        self.http: dict[str, tuple] = {}
        self.sleeper_code = "import time\nwhile True: time.sleep(0.2)"

    def cfg(self):
        return load_config(self.config_path, env={})

    def spawn(self, cmd, log_path, env):
        self.spawned.append(list(cmd))
        p = subprocess.Popen([sys.executable, "-c", self.sleeper_code])
        self.procs.append(p)
        return FakeProc(p)

    def run(self, cmd, **kw):
        self.run_calls.append(list(cmd))
        key = " ".join(cmd[:2])
        return SimpleNamespace(returncode=self.run_rc.get(key, 0), stdout=self.run_stdout, stderr="")

    def http_get(self, url, **kw):
        for frag, res in self.http.items():
            if frag in url:
                return res
        return None, None, 0.0

    def env(self, *, windows=False) -> OpsEnv:
        return OpsEnv(spawn=self.spawn, run=self.run, http_get=self.http_get, sleep=lambda s: __import__("time").sleep(min(s, 0.02)),
                      is_windows=windows, environ={})

    def cli(self, *argv, windows=False, config=True):
        out, err = io.StringIO(), io.StringIO()
        pre = ["--config", str(self.config_path)] if config else []
        code = cli.main([*pre, *argv], env={}, stdout=out, stderr=err, ops_env=self.env(windows=windows))
        return code, out.getvalue(), err.getvalue()

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()


