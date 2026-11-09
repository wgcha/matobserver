"""배치 실행 래퍼: 명령 템플릿, 정상/오류/시간 초과/취소/라이선스 재시도 (가짜 배치 스크립트)."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

from matobserver.core.errors import SOLVER_UNAVAILABLE
from matobserver.core.types import RunState
import os
import subprocess

from matobserver.solvers.lsdyna import runner as R
from matobserver.solvers.lsdyna.runner import SolverUnavailable, build_command, run_batch

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


ss = _support()


class Never:
    def is_set(self):
        return False


class Flag:
    def __init__(self):
        self.ev = threading.Event()

    def is_set(self):
        return self.ev.is_set()


@pytest.fixture
def prepared(tmp_path):
    a = ss.fake_adapter()
    case = ss.make_case("bending3p-param", tmp_path)
    return a, a.prepare(case, {"body": ss.body_material()}, tmp_path / "run")


def test_command_template_from_config(tmp_path):
    a = ss.fake_adapter(**{"solver.lsdyna.batch_command": ["{script}", "i={main_name}", "ncpu={ncpu}", "{mode}",
                                                            "{precision}", "{analysis}"],
                           "solver.lsdyna.script": "/opt/site/run_dyna.sh"})
    cmd = build_command(a.runner_settings(), tmp_path / "main.k", 8, "implicit")
    assert cmd == ["/opt/site/run_dyna.sh", "i=main.k", "ncpu=8", "smp", "double", "implicit"]


def test_missing_script_is_solver_unavailable(tmp_path):
    a = ss.fake_adapter(**{"solver.lsdyna.script": None})
    with pytest.raises(SolverUnavailable) as e:
        build_command(a.runner_settings(), tmp_path / "main.k", 1, "implicit")
    assert e.value.code == SOLVER_UNAVAILABLE
    b = ss.fake_adapter(**{"solver.lsdyna.batch_command": ["/nonexistent/dyna_xyz", "{main}"]})
    (tmp_path / "main.k").write_text("*KEYWORD\n*END\n")
    with pytest.raises(SolverUnavailable):
        run_batch(b.runner_settings(), tmp_path / "main.k", 1, 5, Never(), "implicit")


def test_normal_error_diverged(prepared, monkeypatch, tmp_path):
    a, prep = prepared
    s = a.runner_settings()
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "normal"})
    r = run_batch(s, prep.main_file, 2, 30, Never(), "implicit")
    assert r.state is RunState.NORMAL and r.version == "R13.1.1" and "N o r m a l" in r.message
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "error"})
    assert run_batch(s, prep.main_file, 2, 30, Never(), "implicit").state is RunState.ERROR
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "diverge"})
    assert run_batch(s, prep.main_file, 2, 30, Never(), "implicit").state is RunState.DIVERGED


def test_stale_logs_are_cleared_between_runs(prepared, monkeypatch, tmp_path):
    a, prep = prepared
    (prep.workdir / "d3hsp").write_text(" N o r m a l    t e r m i n a t i o n\n")
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "error"})
    assert run_batch(a.runner_settings(), prep.main_file, 1, 30, Never(), "implicit").state is RunState.ERROR


def test_timeout_kills_within_grace(prepared, monkeypatch, tmp_path):
    a, prep = prepared
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "hang"})
    t0 = time.monotonic()
    r = run_batch(a.runner_settings(), prep.main_file, 1, 1.0, Never(), "implicit")
    assert r.state is RunState.TIMEOUT
    assert time.monotonic() - t0 <= 1.0 + a.config.get("solver.kill_grace_s") + 1.0  # +1 s: 기동 여유


def test_cancel(prepared, monkeypatch, tmp_path):
    a, prep = prepared
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "hang"})
    tok = Flag()
    threading.Timer(0.8, tok.ev.set).start()
    t0 = time.monotonic()
    r = run_batch(a.runner_settings(), prep.main_file, 1, 60, tok, "implicit")
    assert r.state is RunState.CANCELLED
    assert time.monotonic() - t0 <= 0.8 + a.config.get("solver.poll_interval_s") + a.config.get("solver.kill_grace_s") + 1.0


def test_license_retry_then_success_and_timeout(prepared, monkeypatch, tmp_path):
    a, prep = prepared
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "license_then_normal", "license_failures": 2})
    r = run_batch(a.runner_settings(), prep.main_file, 1, 30, Never(), "implicit")
    assert r.state is RunState.NORMAL and r.license_waits == 2
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "license"})
    r = run_batch(a.runner_settings(), prep.main_file, 1, 30, Never(), "implicit")
    # interval 0.1 s, max_wait 0.35 s → 3회 대기 후 4번째 실패에서 LICENSE_TIMEOUT
    assert r.state is RunState.LICENSE_TIMEOUT and r.license_waits == 3


# ---------------------------------------------------------------- 프로세스 트리 종료
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # 거둬지지 않은 좀비는 죽은 것으로 본다
        with open(f"/proc/{pid}/status") as f:
            return not any(ln.startswith("State:") and "Z" in ln.split()[1] for ln in f)
    except OSError:
        return True


def _wait_dead(pid: int, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group kill")
@pytest.mark.parametrize("how", ["timeout", "cancel"])
def test_child_process_tree_is_killed(prepared, monkeypatch, tmp_path, how):
    a, prep = prepared
    ss.set_behavior(monkeypatch, tmp_path, {"implicit": "hang_child"})
    tok = Flag()
    if how == "cancel":
        threading.Timer(1.0, tok.ev.set).start()
    r = run_batch(a.runner_settings(), prep.main_file, 1, 1.0 if how == "timeout" else 60, tok, "implicit")
    assert r.state is (RunState.TIMEOUT if how == "timeout" else RunState.CANCELLED)
    pid = int((prep.workdir / "fake_child.pid").read_text())
    assert _wait_dead(pid), "LS-DYNA child process survived"


def test_popen_kwargs_per_os():
    assert R.popen_kwargs("posix") == {"start_new_session": True}
    assert R.popen_kwargs("nt") == {"creationflags": 0x00000200}
    assert R.taskkill_command(4242) == ["taskkill", "/T", "/F", "/PID", "4242"]


class _FakeProc:
    def __init__(self):
        self.pid, self.killed, self.waited, self.rc = 4242, False, False, None

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


@pytest.mark.parametrize("taskkill_rc, fallback", [(0, False), (128, True), (None, True)])
def test_windows_terminate_uses_taskkill_tree_then_kill_fallback(monkeypatch, taskkill_rc, fallback):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if taskkill_rc is None:
            raise FileNotFoundError("taskkill")
        return subprocess.CompletedProcess(cmd, taskkill_rc)

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    p = _FakeProc()
    R._terminate(p, 1.0, os_name="nt")
    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]
    assert p.killed is fallback and p.waited
