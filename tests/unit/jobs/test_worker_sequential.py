"""B-5: 워커 1개·잡 1건 동시 실행 — 두 잡을 제출하면 순차 실행 (api.md §5, module-boundaries.md §5.4)."""

from __future__ import annotations

import threading

from matobserver.jobs.worker import WorkerLock

from be_support import calibration_body, make_hooks


def submit(e):
    r = e.post_job(calibration_body())
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def test_two_jobs_run_sequentially_even_with_two_worker_threads(active_env):
    e = active_env
    j1, j2 = submit(e), submit(e)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "order": []}
    first_started, release_first = threading.Event(), threading.Event()

    def run(spec, services):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["order"].append(spec.job_id)
        if spec.job_id == j1:
            first_started.set()
            assert release_first.wait(10)
        with lock:
            state["active"] -= 1
        return {}

    hooks = make_hooks(run=run)
    w1, w2 = e.worker(hooks), e.worker(hooks)  # 일부러 워커를 2개 둔다: DB 획득 규칙이 동시 실행을 막아야 한다
    t1 = threading.Thread(target=w1.run_once)
    t1.start()
    assert first_started.wait(10)
    # 첫 잡이 실행 중인 동안 두 번째 워커는 아무것도 가져오지 못한다
    assert w2.run_once() is False
    st = e.get(f"/v1/calibrations/{j2}").json()
    assert st["status"] == "queued" and e.get(f"/v1/calibrations/{j1}").json()["status"] == "running"
    release_first.set()
    t1.join(10)
    assert w2.run_once() is True  # 이제 두 번째
    assert state["order"] == [j1, j2] and state["max_active"] == 1
    assert [e.get(f"/v1/calibrations/{j}").json()["status"] for j in (j1, j2)] == ["succeeded", "succeeded"]


def test_fifo_order_by_submission(active_env):
    e = active_env
    ids = [submit(e) for _ in range(4)]
    order = []
    w = e.worker(make_hooks(run=lambda spec, svc: order.append(spec.job_id) or {}))
    while w.run_once():
        pass
    assert order == ids


def test_started_at_of_second_job_is_after_first_finished(active_env):
    e = active_env
    j1, j2 = submit(e), submit(e)
    w = e.worker()
    w.run_once()
    mid = e.get(f"/v1/calibrations/{j2}").json()
    assert mid["status"] == "queued" and mid["started_at"] is None  # 한 번에 한 건만 가져간다
    w.run_once()
    a, b = (e.get(f"/v1/calibrations/{j}").json() for j in (j1, j2))
    assert a["finished_at"] <= b["started_at"]


def test_recipe_verification_shares_the_same_single_slot(env):
    """레시피 검증도 같은 워커·큐에서 순차 실행한다 (api.md §8.2)."""
    jobs_seen = []
    release = threading.Event()
    started = threading.Event()

    def verify(recipe, services, strict):
        started.set()
        assert release.wait(10)
        return {"status": "passed", "checks": []}

    hooks = make_hooks(verify=verify, run=lambda spec, svc: jobs_seen.append(spec.job_id) or {})
    env.hooks = hooks
    env.add_recipe()
    t = threading.Thread(target=env.worker(hooks).run_once)
    t.start()
    assert started.wait(10)
    assert env.worker(hooks).run_once() is False  # 검증 중에는 다른 항목을 못 가져온다
    release.set()
    t.join(10)
    assert env.get("/v1/recipes").json()["total"] == 1
    jid = env.post_job(calibration_body()).json()["job_id"]
    assert env.worker(hooks).run_once() is True and jobs_seen == [jid]


def test_claim_is_atomic_under_many_threads(active_env):
    e = active_env
    for _ in range(6):
        submit(e)
    got, lock = [], threading.Lock()
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        item = e.ctx.store.claim_next()
        with lock:
            got.append(item)

    ts = [threading.Thread(target=grab) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sum(1 for g in got if g is not None) == 1  # running 이 비기 전에는 단 한 건만 성공


def test_worker_lock_allows_only_one_process_holder(tmp_path):
    a, b = WorkerLock(tmp_path / "w.lock"), WorkerLock(tmp_path / "w.lock")
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()
