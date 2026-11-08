"""B-12: 계약 1.1 — CalibrationSpec 옵션 필드, 취소 3경로(api.md §4.12), CalibrationCancelled."""

from __future__ import annotations

import time

from matobserver.core.errors import MatObserverError

from be_support import calibration_body, make_hooks


class CalibrationCancelled(MatObserverError):  # 엔진 것과 같은 이름·기반 클래스 (엔진 import 없이 대역)
    pass


def status(e, jid):
    return e.get(f"/v1/calibrations/{jid}").json()


def submit(e, **over):
    r = e.post_job(calibration_body(**over))
    assert r.status_code == 201, r.text
    return r.json()["job_id"]


def test_spec_carries_plasticity_mode_and_explicit_check_equal_to_job_options(active_env):
    e = active_env
    seen = {}

    def run(spec, services):
        seen["spec"], seen["opts"] = spec, dict(services.job_options)
        return {"best_J": 0.0}

    submit(e, plasticity_mode="table", explicit_check={"enabled": False})
    e.worker(make_hooks(run=run)).run_once()
    spec = seen["spec"]
    assert spec.plasticity_mode == seen["opts"]["plasticity_mode"] and spec.plasticity_mode is not None
    assert spec.explicit_check == seen["opts"]["explicit_check"] and spec.explicit_check is not None


def request_cancel(e, jid, services):
    """실행 중 취소 요청: 응답 확인 후 워커 토큰이 요청을 볼 때까지 상한 있는 폴링 (시간 의존 제거)."""
    r = e.client.post(f"/v1/calibrations/{jid}/cancel", headers=e.svc)
    assert r.status_code == 202 and r.json()["cancel_requested"] is True, r.text
    for _ in range(1000):
        if services.cancel.is_set():
            return
        time.sleep(0.005)
    raise AssertionError("cancel flag not observed")


def _assert_cancelled_without_result(e, jid):
    st = status(e, jid)
    assert st["status"] == "cancelled" and st["error"] is None
    assert e.get(f"/v1/calibrations/{jid}/result").status_code == 409
    assert e.get(f"/v1/calibrations/{jid}/result").json()["error"]["code"] == "JOB_NOT_SUCCEEDED"
    return st


def test_path1_calibration_cancelled_without_flag(active_env):
    e = active_env
    jid = submit(e)

    def run(spec, services):
        services.record_eval({"eval_index": 0, "stage": "plastic", "theta": {}, "J": 1.0, "status": "ok", "wall_time_s": 0.1, "is_best": True})
        raise CalibrationCancelled("cancelled")

    e.worker(make_hooks(run=run)).run_once()
    _assert_cancelled_without_result(e, jid)
    assert e.get(f"/v1/calibrations/{jid}/history").json()["total"] == 1  # history 보존


def test_path2_other_exception_after_cancel_request(active_env):
    e = active_env
    jid = submit(e)

    def run(spec, services):
        services.record_eval({"eval_index": 0, "stage": "plastic", "theta": {}, "J": 1.0, "status": "ok", "wall_time_s": 0.1, "is_best": True})
        request_cancel(e, jid, services)
        raise RuntimeError("boom")

    e.worker(make_hooks(run=run)).run_once()
    _assert_cancelled_without_result(e, jid)
    assert e.get(f"/v1/calibrations/{jid}/history").json()["total"] == 1


def test_path3_normal_return_while_cancel_requested(active_env):
    e = active_env
    jid = submit(e)

    def run(spec, services):
        services.record_eval({"eval_index": 0, "stage": "plastic", "theta": {}, "J": 1.0, "status": "ok", "wall_time_s": 0.1, "is_best": True})
        request_cancel(e, jid, services)
        return {"best_J": 1.0}

    e.worker(make_hooks(run=run)).run_once()
    _assert_cancelled_without_result(e, jid)
    assert e.get(f"/v1/calibrations/{jid}/history").json()["total"] == 1
