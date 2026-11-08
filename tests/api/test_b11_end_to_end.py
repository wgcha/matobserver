"""B-11: 엔진 대역(가짜 run_calibration)으로 E3 -> E5 -> E6 종단 흐름 (+ E7, E9, 실패 경로)."""

from __future__ import annotations

from pathlib import Path

from matobserver.core import errors as E
from matobserver.core.errors import EngineFailure
from matobserver.core.types import CalibrationSpec

from be_support import RECIPE_REF, RUN_ID, calibration_body, make_hooks, mn_body


def drain(env, hooks=None):
    w = env.worker(hooks)
    n = 0
    while w.run_once():
        n += 1
    return n


def test_e3_e5_e6_flow_with_matnexus_runs(active_env):
    e = active_env
    r = e.post_job(mn_body(name="종단"))
    assert r.status_code == 201, r.text
    jid = r.json()["job_id"]
    assert e.get(f"/v1/calibrations/{jid}").json()["status"] == "queued"
    assert e.get(f"/v1/calibrations/{jid}/result").status_code == 409
    assert drain(e) == 1
    st = e.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "succeeded" and st["iter"] == 3 and st["error"] is None
    res = e.get(f"/v1/calibrations/{jid}/result")
    assert res.status_code == 200 and res.json()["job_id"] == jid and res.json()["status"] == "succeeded"
    assert res.json()["materials"][0]["slot"] == "body"
    assert {a["kind"] for a in res.json()["artifacts"]} == {"history", "result_json"}
    rj = next(a for a in res.json()["artifacts"] if a["kind"] == "result_json")
    assert e.get(f"/v1/artifacts/{rj['artifact_id']}").json()["job_id"] == jid
    assert e.get(f"/v1/calibrations/{jid}/history").json()["total"] == 3
    # 목록: 상태·best_J 반영
    row = e.get("/v1/calibrations", params={"status": "succeeded"}).json()["items"][0]
    assert row["job_id"] == jid and row["best_J"] == st["best_J"] and row["material_id"] == "3c3c3c3c-0000-4000-8000-000000000001"


def test_engine_receives_calibration_spec(active_env):
    """module-boundaries.md §4.1 시그니처: run_calibration(spec: CalibrationSpec, services) -> Mapping."""
    e = active_env
    seen = {}

    def run(spec, services):
        seen["spec"], seen["services"] = spec, services
        seen["workdir"] = services.workdir()
        seen["cancel"] = services.cancel.is_set()
        return {"best_J": 0.0}

    jid = e.post_job(mn_body(seed=7, budget={"max_evaluations": 33, "max_wall_time_s": 900, "run_timeout_s": 60},
                             recipe_params={"fric": 0.2})).json()["job_id"]
    drain(e, make_hooks(run=run))
    spec: CalibrationSpec = seen["spec"]
    assert isinstance(spec, CalibrationSpec) and spec.job_id == jid and spec.schema_version == "1.0"
    assert spec.seed == 7 and dict(spec.budget) == {"max_evaluations": 33, "max_wall_time_s": 900.0, "run_timeout_s": 60.0}
    assert spec.optimizer[0] == "bo" and spec.elastic_optimizer[0] == "lsq" and spec.solver[0] == "lsdyna"
    t = spec.tests[0]
    assert (t.test_key, t.test_type, t.recipe.recipe_ref) == ("t1", "bending", RECIPE_REF)
    assert t.recipe.manifest["recipe_id"] == "bending3p-param" and t.recipe.store_dir.is_dir()
    assert dict(t.recipe_params_si) == {"fric": 0.2}
    assert len(t.runs) == 1 and t.runs[0].run_id == RUN_ID and set(t.runs[0].channels) >= {"force", "displacement", "time"}
    assert t.runs[0].specimen_dimensions["span"] == 0.064 and t.runs[0].specimen_dimensions["width"] == 0.0101  # measured 우선
    assert t.geometry["kind"] == "box" and t.geometry["dimensions"]["thickness"] == 0.004
    assert spec.materials.mode == "A" and spec.materials.slots[0]["material_model"] == "mat024_coeff"  # 별칭 해석
    ref = spec.materials.slots[0]["reference"]
    assert ref.elastic.poisson_ratio == 0.33
    assert spec.request_hash and len(spec.request_hash) >= 32
    # 서비스 객체: 작업 폴더는 paths.work_dir/{job_id}
    svc = seen["services"]
    assert svc.owner_kind == "job" and svc.owner_id == jid and svc.registry is e.registry
    assert seen["workdir"] == Path(e.config.get("paths.work_dir")).resolve() / jid or seen["workdir"] == Path(e.config.get("paths.work_dir")) / jid
    assert seen["cancel"] is False


def test_same_request_gives_same_hash_and_seed_default(active_env):
    e = active_env
    specs = []
    for _ in range(2):
        e.post_job(calibration_body())
    drain(e, make_hooks(run=lambda spec, svc: specs.append(spec) or {}))
    assert specs[0].request_hash == specs[1].request_hash and specs[0].seed == e.config.get("engine.default_seed")
    assert specs[0].job_id != specs[1].job_id


def test_failed_job_carries_engine_error_code(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]

    def run(spec, services):
        raise EngineFailure(E.MESH_FAILED, f"시편 메시 실패: {services.workdir()}/mesh.log")

    drain(e, make_hooks(run=run))
    st = e.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "failed" and st["error"]["code"] == "MESH_FAILED"
    assert str(e.root) not in st["error"]["message"]  # 서버 경로는 가려진다
    r = e.get(f"/v1/calibrations/{jid}/result")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["status"] == "failed"


def test_unexpected_exception_becomes_internal_error_without_leaking(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]

    def run(spec, services):
        raise RuntimeError(f"boom at {services.workdir()}/secret")

    drain(e, make_hooks(run=run))
    st = e.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "failed" and st["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" not in st["error"]["message"] and str(e.root) not in str(st)
    log = Path(e.config.get("paths.work_dir")) / jid / "worker_error.log"
    assert log.exists() and "boom" in log.read_text(encoding="utf-8")  # 트레이스백은 서버 로그 파일에만


def test_engine_not_installed_fails_job_not_worker(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    drain(e, make_hooks(run=None))
    st = e.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "failed" and st["error"]["code"] == "INTERNAL_ERROR"


def test_result_json_artifact_not_duplicated_when_engine_registers_one(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]

    def run(spec, services):
        p = services.workdir("artifacts") / "result.json"
        p.write_text('{"mine": true}', encoding="utf-8")
        services.add_artifact(p, "result_json")
        return {"best_J": 1.0}

    drain(e, make_hooks(run=run))
    arts = e.get(f"/v1/calibrations/{jid}/result").json()["artifacts"]
    assert [a["kind"] for a in arts] == ["result_json"]
    assert e.get(f"/v1/artifacts/{arts[0]['artifact_id']}").json() == {"mine": True}


def test_job_inputs_snapshot_is_stored_for_reproducibility(active_env):
    e = active_env
    jid = e.post_job(mn_body()).json()["job_id"]
    inputs = Path(e.config.get("paths.work_dir")) / jid / "inputs"
    assert (inputs / "request.json").is_file() and (inputs / "runs" / f"{RUN_ID}.json").is_file()
    n_calls = len(e.mn.calls)
    drain(e)  # 실행 중에는 MatNexus 를 다시 부르지 않는다
    assert len(e.mn.calls) == n_calls


def test_preflight_receives_spec_draft_and_services(active_env):
    e = active_env
    got = {}

    def pre(spec, services):
        got["spec"], got["owner"] = spec, services.owner_id
        got["dir"] = services.workdir("tmp")
        return []

    e.ctx.hooks.preflight = pre
    jid = e.post_job(calibration_body()).json()["job_id"]
    assert isinstance(got["spec"], CalibrationSpec) and got["spec"].job_id == jid
    assert not got["dir"].exists()  # 임시 폴더는 정리된다
