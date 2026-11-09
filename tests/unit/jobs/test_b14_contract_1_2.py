"""B-14: 계약 1.2 — joint 단계(phase·history·iter), result 1.2 신규 필드 통과, 설정 키."""

from __future__ import annotations

from matobserver.core.config import Config

from be_support import calibration_body, make_hooks


def _item(i, stage):
    return {"eval_index": i, "stage": stage, "theta": {}, "J": 1.0 / (i + 1), "status": "ok", "wall_time_s": 0.1, "is_best": True}


RESULT_12 = {
    "best_J": 0.1,
    "termination": {"reason": "budget", "evaluations": 3, "evaluations_failed": 0, "wall_time_s": 1.0,
                    "per_stage": {"elastic": {"reason": "converged", "evaluations": 1, "best_eval_index": 0},
                                  "joint": {"reason": "budget", "evaluations": 1, "best_eval_index": 2, "accepted": True}},
                    "final_stage": "joint",
                    "e_by_stage": [{"slot": "body", "elastic": 7.0e10, "plastic": 7.0e10, "joint": 7.1e10, "rel_change_joint": 0.0143}]},
    "regularization": {"kind": "adjacent_slope", "weight": 0.01, "J_reg": 0.0007},
    "materials": [{"slot": "body", "material_model": {"name": "mat024", "version": "1.0.0",
                                                        "options": {"max_strain": 0.4, "smoothing_weight": 0.01}}}],
}


def test_joint_stage_phase_history_and_result_fields(active_env):
    e = active_env
    seen = {}

    def run(spec, services):
        services.progress("joint", stage_iter=0, max_evaluations=10)
        seen["phase"] = e.get(f"/v1/calibrations/{jid}").json()["phase"]
        for i, st in enumerate(("elastic", "plastic", "joint")):
            services.record_eval(_item(i, st))
        services.progress("diagnostics")
        return dict(RESULT_12)

    jid = e.post_job(calibration_body(schema_version="1.2")).json()["job_id"]
    e.worker(make_hooks(run=run)).run_once()
    assert seen["phase"] == "joint"
    st = e.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "succeeded" and st["schema_version"] == "1.2" and st["iter"] == 3  # joint 평가도 예산에 포함
    hist = e.get(f"/v1/calibrations/{jid}/history").json()
    assert [h["stage"] for h in hist["items"]] == ["elastic", "plastic", "joint"]
    res = e.get(f"/v1/calibrations/{jid}/result").json()
    assert res["schema_version"] == "1.2"
    assert res["termination"]["final_stage"] == "joint" and res["termination"]["e_by_stage"][0]["joint"] == 7.1e10
    assert res["termination"]["per_stage"]["joint"]["accepted"] is True
    assert res["regularization"] == RESULT_12["regularization"]
    assert res["materials"][0]["material_model"]["options"] == {"max_strain": 0.4, "smoothing_weight": 0.01}


def test_joint_config_defaults():
    c = Config({})
    assert c.get("engine.stages.joint.enabled") is True
    assert c.get("engine.stages.joint.optimizer") == "lsq"
    assert c.get("engine.stages.joint.max_evaluations") is None
    assert c.get("engine.stages.joint.e_bound_rel") == 0.05
    assert c.get("material.mat024_table.smoothing_weight") == 0.01
