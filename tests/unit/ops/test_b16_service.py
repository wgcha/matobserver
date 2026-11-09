"""B-16: start/stop/status — process 모드 PID 파일, 멱등 start, 잡 실행 중 stop 거부, status 항목·종료 코드, scheduler 모드(schtasks 대역)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from matobserver.jobs.db import Database
from matobserver.jobs.store import JobStore
from matobserver.ops import service as svc
from matobserver.ops.runtime import ServiceLifecycle, pid_alive, pid_path, read_pidfile, service_state, stop_path, write_pidfile


def pidfile(ops, name):
    return read_pidfile(pid_path(ops.cfg(), name))


def test_start_process_mode_writes_pid_file_and_is_idempotent(ops):
    code, out, _ = ops.cli("start", "--mode", "process", "--json")
    rep = json.loads(out)
    assert code == 0 and rep["server"]["result"] == "started" and rep["worker"]["result"] == "started"
    info = pidfile(ops, "server")
    assert info["pid"] == rep["server"]["pid"] and info["mode"] == "process" and "started_at" in info and "version" in info
    assert pid_alive(info["pid"]) and len(ops.spawned) == 2
    assert "serve" in ops.spawned[0] and "worker" in ops.spawned[1] and "--config" in ops.spawned[0]
    code, out, _ = ops.cli("start", "--mode", "process", "--json")
    rep2 = json.loads(out)
    assert code == 0 and rep2["server"]["result"] == "already_running" and len(ops.spawned) == 2  # 멱등


def test_start_single_service(ops):
    code, out, _ = ops.cli("start", "--service", "worker", "--mode", "process", "--json")
    assert code == 0 and pidfile(ops, "worker") and pidfile(ops, "server") is None


def test_stale_pid_is_reported_and_replaced_on_start(ops):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_pidfile(pid_path(ops.cfg(), "server"), dead.pid, "process")
    assert service_state(ops.cfg(), "server")["state"] == "stale_pid"
    code, out, _ = ops.cli("status", "--json")
    assert json.loads(out)["server"]["state"] == "stale_pid"
    code, out, _ = ops.cli("start", "--service", "server", "--mode", "process", "--json")
    assert code == 0 and pidfile(ops, "server")["pid"] != dead.pid and len(ops.spawned) == 1


def test_stop_graceful_via_stop_file(ops):
    ops.sleeper_code = ("import os, sys, time\nstop = sys.argv[1]\n"
                        "while not os.path.exists(stop): time.sleep(0.05)\n")
    ops.spawn_args = None
    orig = ops.spawn

    def spawn(cmd, log, env):  # 정지 요청 파일을 보는 협조적 프로세스
        p = subprocess.Popen([sys.executable, "-c", ops.sleeper_code, str(stop_path(ops.cfg(), "server"))])
        ops.procs.append(p)
        ops.spawned.append(list(cmd))
        return type("P", (), {"pid": p.pid})()

    ops.spawn = spawn
    ops.cli("start", "--service", "server", "--mode", "process")
    pid = pidfile(ops, "server")["pid"]
    code, out, _ = ops.cli("stop", "--service", "server", "--timeout", "10", "--json")
    rep = json.loads(out)
    assert code == 0 and rep["server"]["result"] == "stopped" and not pid_alive(pid)
    assert pidfile(ops, "server") is None and not stop_path(ops.cfg(), "server").exists()


def test_stop_forces_after_timeout(ops):
    ops.cli("start", "--service", "server", "--mode", "process")  # 정지 요청을 무시하는 프로세스
    pid = pidfile(ops, "server")["pid"]
    code, out, _ = ops.cli("stop", "--service", "server", "--timeout", "0.2", "--json")
    assert code == 0 and json.loads(out)["server"]["result"] == "killed" and not pid_alive(pid) and pidfile(ops, "server") is None


def test_stop_when_not_running(ops):
    code, out, _ = ops.cli("stop", "--json")
    assert code == 0 and json.loads(out)["server"]["result"] == "not_running"


def _make_busy_worker(ops):
    ops.cli("start", "--service", "worker", "--mode", "process")
    db = ops.cfg().path("jobstore.sqlite_path")
    store = JobStore(Database(db))
    with store.db.tx() as c:
        c.execute("INSERT INTO jobs (job_id, status, created_at, schema_version) VALUES ('cal_busy','running','2026-01-01T00:00:00Z','1.3')")
    return pidfile(ops, "worker")["pid"], store


def test_stop_refused_while_job_running_then_force(ops):
    pid, _ = _make_busy_worker(ops)
    code, out, _ = ops.cli("stop", "--service", "worker", "--timeout", "0.2", "--json")
    assert code == 4 and json.loads(out)["worker"]["result"] == "refused" and pid_alive(pid)
    code, out, _ = ops.cli("stop", "--service", "worker", "--timeout", "0.2", "--force", "--json")
    assert code == 0 and not pid_alive(pid)


def test_status_items_and_exit_codes(ops):
    code, out, _ = ops.cli("status", "--json")
    rep = json.loads(out)
    assert code == 4 and rep["server"]["state"] == "stopped" and rep["worker"]["state"] == "stopped"
    for key in ("version", "contract_schema_version", "config_path"):
        assert rep[key]
    assert rep["server"]["url"] == "http://127.0.0.1:18040" and rep["server"]["health"] == "unreachable"
    assert rep["queue"] == {"queued": 0, "running_job_id": None} and rep["recipes"]["active"] == 0
    assert rep["worker"]["last_heartbeat_age_s"] is None
    ops.http = {"/v1/health": (200, {"status": "ok", "version": "0.4.0", "schema_version": "1.3"}, 0.0)}
    ops.cli("start", "--service", "server", "--mode", "process")
    code, out, _ = ops.cli("status", "--json")
    rep = json.loads(out)
    assert code == 3 and rep["server"]["state"] == "running" and rep["server"]["health"] == "ok" and rep["server"]["health_schema_version"] == "1.3"
    ops.cli("start", "--service", "worker", "--mode", "process")
    code, out, _ = ops.cli("status", "--json")
    assert code == 0
    code, out, _ = ops.cli("status")  # 사람용 표
    assert "server.state" in out and "queue.queued" in out
    code, out, err = ops.cli("status", config=False)  # 기본 설정 경로도 없음 -> 기본값으로 동작하거나 설정 오류
    assert code in (0, 1, 3, 4)


def test_status_config_error_is_1(tmp_path, ops):
    code, out, err = ops.cli("status", "--config", str(tmp_path / "missing.yaml"), config=False)
    assert code == 1 and "설정 오류" in err


def test_service_lifecycle_pid_stop_file_and_heartbeat(ops):
    cfg = ops.cfg()
    stopped, beats = [], []
    with ServiceLifecycle(cfg, "worker", "scheduler", lambda: stopped.append(1), heartbeat=lambda: beats.append(1),
                          heartbeat_s=0.05, poll_s=0.02):
        info = read_pidfile(pid_path(cfg, "worker"))
        assert info["pid"] == os.getpid() and info["mode"] == "scheduler"
        stop_path(cfg, "worker").write_text("stop")
        deadline = time.time() + 5
        while (not stopped or len(beats) < 3) and time.time() < deadline:
            time.sleep(0.02)
        assert stopped == [1] and len(beats) >= 3
    assert read_pidfile(pid_path(cfg, "worker")) is None and not stop_path(cfg, "worker").exists()


def test_status_reports_heartbeat_age(ops):
    store = JobStore(Database(ops.cfg().path("jobstore.sqlite_path")))
    store.beat_worker(1234, ts=time.time() - 7)
    code, out, _ = ops.cli("status", "--json")
    age = json.loads(out)["worker"]["last_heartbeat_age_s"]
    assert 6 <= age < 30
    assert store.last_heartbeat()["pid"] == 1234


# ---- scheduler 모드 (schtasks 대역)
def test_scheduler_mode_calls_schtasks(ops):
    code, out, _ = ops.cli("start", "--mode", "scheduler", "--json", windows=True)
    assert code == 0 and not ops.spawned
    assert ["schtasks", "/Run", "/TN", svc.TASK_NAMES["server"]] in ops.run_calls
    assert ["schtasks", "/Run", "/TN", svc.TASK_NAMES["worker"]] in ops.run_calls
    assert json.loads(out)["server"]["mode"] == "scheduler"


def test_scheduler_run_failure_is_exit_1(ops):
    ops.run_rc = {"schtasks /Run": 1}
    code, _, _ = ops.cli("start", "--service", "server", "--mode", "scheduler", windows=True)
    assert code == 1


def test_auto_mode_prefers_scheduler_task_when_it_exists(ops):
    ops.cli("start", "--service", "server", "--json", windows=True)
    assert ["schtasks", "/Query", "/TN", svc.TASK_NAMES["server"]] in ops.run_calls
    assert ["schtasks", "/Run", "/TN", svc.TASK_NAMES["server"]] in ops.run_calls and not ops.spawned


def test_auto_mode_falls_back_to_process_without_task(ops):
    ops.run_rc = {"schtasks /Query": 1}
    ops.cli("start", "--service", "server", "--json", windows=True)
    assert len(ops.spawned) == 1 and pidfile(ops, "server")["mode"] == "process"
    ops2_code, out, _ = ops.cli("start", "--service", "worker", "--json")  # 비 Windows: schtasks 조회 자체를 안 함
    assert len(ops.spawned) == 2


def test_stop_scheduler_mode_ends_task_when_not_graceful(ops):
    p = subprocess.Popen([sys.executable, "-c", ops.sleeper_code])
    ops.procs.append(p)
    write_pidfile(pid_path(ops.cfg(), "server"), p.pid, "scheduler")
    code, out, _ = ops.cli("stop", "--service", "server", "--timeout", "0.1", "--json", windows=True)
    assert code == 0 and ["schtasks", "/End", "/TN", svc.TASK_NAMES["server"]] in ops.run_calls
