"""B-4: 잡 상태 머신 — queued->running->succeeded/failed/cancelled, 취소 2경로, 워커 재시작 시 WORKER_LOST (api.md §5)."""

from __future__ import annotations

from matobserver.core import errors as E
from matobserver.core.errors import EngineFailure
from matobserver.jobs.store import KIND_JOB, KIND_VERIFICATION

from be_support import calibration_body, make_hooks, recipe_zip


def status(e, jid):
    return e.get(f"/v1/calibrations/{jid}").json()


def submit(e, **over):
    r = e.post_job(calibration_body(**over))
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def test_queued_to_running_to_succeeded(active_env):
    e = active_env
    jid = submit(e)
    seen = []

    def run(spec, services):
        seen.append(status(e, jid))  # 실행 중 상태를 API 로 본다
        return {"best_J": 0.5}

    st0 = status(e, jid)
    assert st0["status"] == "queued" and st0["started_at"] is None and st0["finished_at"] is None
    assert e.worker(make_hooks(run=run)).run_once() is True
    assert seen[0]["status"] == "running" and seen[0]["started_at"] and seen[0]["finished_at"] is None
    assert seen[0]["phase"] == "preprocess"
    done = status(e, jid)
    assert done["status"] == "succeeded" and done["finished_at"] and done["error"] is None


def test_running_to_failed_with_code(active_env):
    e = active_env
    jid = submit(e)

    def run(spec, services):
        raise EngineFailure(E.ALL_INITIAL_RUNS_FAILED, "초기 설계점 전부 실패")

    e.worker(make_hooks(run=run)).run_once()
    st = status(e, jid)
    assert st["status"] == "failed" and st["error"] == {"code": "ALL_INITIAL_RUNS_FAILED", "message": "초기 설계점 전부 실패"}
    assert st["finished_at"]


def test_cancel_queued_path_is_immediate(active_env):
    e = active_env
    jid = submit(e)
    r = e.client.post(f"/v1/calibrations/{jid}/cancel", headers=e.svc)
    assert r.status_code == 202 and r.json()["status"] == "cancelled" and r.json()["finished_at"]
    calls = []
    assert e.worker(make_hooks(run=lambda s, v: calls.append(1) or {})).run_once() is False  # 큐에서 빠졌다
    assert not calls and status(e, jid)["status"] == "cancelled"


def test_cancel_running_path_sets_flag_then_worker_finishes_cancelled(active_env):
    e = active_env
    jid = submit(e)
    during = {}

    def run(spec, services):
        assert services.cancel.is_set() is False
        r = e.client.post(f"/v1/calibrations/{jid}/cancel", headers=e.svc)  # 실행 중 취소 요청
        during["resp"] = r
        during["status"] = status(e, jid)
        assert services.cancel.is_set() is True  # 워커 취소 폴링(worker.cancel_poll_s=0)이 요청을 본다
        raise EngineFailure(E.INTERNAL_ERROR, "취소로 중단")  # 취소 요청이 있으면 종료 사유와 무관하게 cancelled

    e.worker(make_hooks(run=run)).run_once()
    assert during["resp"].status_code == 202
    assert during["resp"].json()["status"] == "running" and during["resp"].json()["cancel_requested"] is True
    assert during["status"]["status"] == "running"
    st = status(e, jid)
    assert st["status"] == "cancelled" and st["cancel_requested"] is True and st["error"] is None


def test_cancel_running_even_if_engine_returns_result(active_env):
    e = active_env
    jid = submit(e)

    def run(spec, services):
        e.client.post(f"/v1/calibrations/{jid}/cancel", headers=e.svc)
        return {"best_J": 1.0}

    e.worker(make_hooks(run=run)).run_once()
    assert status(e, jid)["status"] == "cancelled"
    assert e.get(f"/v1/calibrations/{jid}/result").status_code == 409


def test_cancel_requested_before_claim_never_runs(active_env):
    """큐에서 가져오기 직전에 cancel_requested 가 서 있으면 엔진을 부르지 않고 cancelled."""
    e = active_env
    jid = submit(e)
    with e.ctx.db.tx() as c:  # running 전환 직전 경쟁 상황을 재현: 플래그만 설정
        c.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (jid,))
    calls = []
    e.worker(make_hooks(run=lambda s, v: calls.append(1) or {})).run_once()
    assert not calls and status(e, jid)["status"] == "cancelled"


def test_terminal_states_reject_cancel_409(active_env):
    e = active_env
    jid = submit(e)
    e.worker().run_once()
    assert status(e, jid)["status"] == "succeeded"
    r = e.client.post(f"/v1/calibrations/{jid}/cancel", headers=e.svc)
    assert r.status_code == 409 and r.json()["error"]["code"] == "JOB_ALREADY_FINISHED"
    assert status(e, jid)["status"] == "succeeded"


def test_worker_lost_on_restart_marks_running_job_failed(active_env):
    e = active_env
    j1, j2 = submit(e), submit(e)
    item = e.ctx.store.claim_next()  # 첫 잡을 running 으로 만들고 워커가 죽은 상황
    assert item.ref_id == j1 and status(e, j1)["status"] == "running"
    restarted = e.worker()  # 새 워커 프로세스
    lost = restarted.recover()
    assert lost[KIND_JOB] == [j1]
    st = status(e, j1)
    assert st["status"] == "failed" and st["error"]["code"] == "WORKER_LOST" and st["finished_at"]
    assert status(e, j2)["status"] == "queued"
    assert restarted.run_once() is True  # 큐가 다시 흐른다
    assert status(e, j2)["status"] == "succeeded"


def test_worker_lost_marks_running_verification_rejected(env):
    reg = env.add_recipe().json()
    item = env.ctx.store.claim_next()
    assert item.kind == KIND_VERIFICATION
    lost = env.worker().recover()
    assert lost[KIND_VERIFICATION] == [reg["verification_id"]]
    d = env.client.get("/v1/admin/recipes/bending3p-param/versions/1", headers=env.admin).json()
    assert d["status"] == "rejected"
    assert d["verification"]["status"] == "error" and d["verification"]["error"]["code"] == "WORKER_LOST"
    # rejected 이므로 같은 버전을 새 내용으로 다시 올릴 수 있다
    r = env.add_recipe(recipe_zip(edit_manifest=lambda t: t.replace("fixture: 3-point", "fixture v2: 3-point")))
    assert r.status_code == 202


def test_recover_is_noop_when_nothing_running(active_env):
    e = active_env
    j = submit(e)
    assert e.worker().recover() == {KIND_JOB: [], KIND_VERIFICATION: []}
    assert status(e, j)["status"] == "queued"


def test_run_forever_recovers_then_processes_until_stopped(active_env):
    import threading

    e = active_env
    j1, j2 = submit(e), submit(e)
    e.ctx.store.claim_next()  # j1 은 이전 워커가 죽으며 남긴 running
    stop = threading.Event()
    done = threading.Event()

    def run(spec, services):
        if spec.job_id == j2:
            done.set()
        return {}

    t = threading.Thread(target=e.worker(make_hooks(run=run)).run_forever, args=(stop, 0.01))
    t.start()
    assert done.wait(10)
    stop.set()
    t.join(10)
    assert not t.is_alive()
    assert status(e, j1)["error"]["code"] == "WORKER_LOST" and status(e, j2)["status"] == "succeeded"


def test_run_forever_survives_loop_error(active_env, monkeypatch):
    import threading

    e = active_env
    submit(e)
    w = e.worker()
    calls = {"n": 0}
    real = w.run_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("일시 오류")
        return real()

    monkeypatch.setattr(w, "run_once", flaky)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop, 0.01))
    t.start()
    for _ in range(500):
        if calls["n"] >= 3:
            break
        stop.wait(0.01)
    stop.set()
    t.join(10)
    assert calls["n"] >= 2 and not t.is_alive()
