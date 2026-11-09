"""B-1: E1–E12, A1–A4 요청·응답 스키마 계약 (api.md §3–§8)."""

from __future__ import annotations

import re

from be_support import RECIPE_REF, calibration_body, mn_body

ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
STR_OR_NONE = (str, type(None))


def check(obj, spec, where="$"):
    """spec: {key: type | tuple(types) | 'any'} 의 필수 키·타입 검사. 정의 밖 키는 허용(계약상 추가 필드 허용)."""
    assert isinstance(obj, dict), f"{where}: dict 가 아님 {obj!r}"
    for key, typ in spec.items():
        assert key in obj, f"{where}: 키 {key!r} 없음 (있는 키: {sorted(obj)})"
        if typ != "any":
            assert isinstance(obj[key], typ), f"{where}.{key}: {type(obj[key]).__name__} (기대 {typ})"


def run_all(env):
    w = env.worker()
    while w.run_once():
        pass


# ---------------------------------------------------------------- E1
def test_e1_health(env):
    r = env.client.get("/v1/health")
    assert r.status_code == 200
    check(r.json(), {"status": str, "version": str, "schema_version": str})
    assert r.json()["status"] == "ok" and r.json()["schema_version"] == "1.2"


# ---------------------------------------------------------------- E2
def test_e2_upload_file(env):
    stp = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"
    r = env.client.post("/v1/files", files={"file": ("pkg.stp", stp, "application/step")}, data={"purpose": "geometry_stp"},
                        headers=env.svc)
    assert r.status_code == 201, r.text
    body = r.json()
    check(body, {"schema_version": str, "file_id": str, "purpose": str, "filename": str, "bytes": int, "sha256": str, "created_at": str})
    assert body["file_id"].startswith("fil_") and body["purpose"] == "geometry_stp" and body["filename"] == "pkg.stp"
    assert body["bytes"] == len(stp) and len(body["sha256"]) == 64 and ISO.match(body["created_at"])
    assert str(env.root) not in r.text  # 경로 비노출


# ---------------------------------------------------------------- E3
def test_e3_create_job_response(active_env):
    r = active_env.post_job(calibration_body())
    assert r.status_code == 201, r.text
    body = r.json()
    check(body, {"schema_version": str, "job_id": str, "status": str, "created_at": str, "warnings": list})
    assert body["job_id"].startswith("cal_") and body["status"] == "queued" and ISO.match(body["created_at"])


def test_e3_warning_shape_and_idempotency(active_env):
    body = calibration_body()
    body["materials"]["slots"][0]["reference"]["elastic"].pop("density")  # DENSITY_DEFAULTED 경고
    r1 = active_env.post_job(body, headers={"Idempotency-Key": "k-1"})
    assert r1.status_code == 201
    assert any(w["code"] == "DENSITY_DEFAULTED" for w in r1.json()["warnings"])
    for w in r1.json()["warnings"]:
        check(w, {"code": str, "message": str})
    r2 = active_env.post_job(body, headers={"Idempotency-Key": "k-1"})
    assert r2.status_code == 200 and r2.json()["job_id"] == r1.json()["job_id"]
    r3 = active_env.post_job(calibration_body(name="다른 이름"), headers={"Idempotency-Key": "k-1"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_e3_tests_array_and_matnexus_runs(active_env):
    body = mn_body()
    t = {k: body.pop(k) for k in ("test_type", "recipe", "geometry", "test_run_ids")}
    t["test_key"] = "a1"
    body["tests"] = [t]
    r = active_env.post_job(body)
    assert r.status_code == 201, r.text
    got = active_env.get(f"/v1/calibrations/{r.json()['job_id']}").json()
    assert got["status"] == "queued"


# ---------------------------------------------------------------- E4
def test_e4_list_jobs(active_env):
    ids = [active_env.post_job(calibration_body(name=f"잡{i}")).json()["job_id"] for i in range(3)]
    r = active_env.get("/v1/calibrations")
    assert r.status_code == 200
    page = r.json()
    check(page, {"schema_version": str, "items": list, "total": int, "limit": int, "offset": int})
    assert page["total"] == 3
    for it in page["items"]:
        check(it, {"job_id": str, "name": STR_OR_NONE, "status": str, "phase": STR_OR_NONE, "test_types": list, "recipes": list,
                   "material_id": STR_OR_NONE, "best_J": (float, int, type(None)), "created_at": str, "finished_at": STR_OR_NONE})
    assert page["items"][0]["test_types"] == ["bending"] and page["items"][0]["recipes"] == [RECIPE_REF]
    assert {i["job_id"] for i in page["items"]} == set(ids)
    created = [i["created_at"] for i in page["items"]]
    assert created == sorted(created, reverse=True)  # created_at 내림차순
    # 필터·페이지
    assert active_env.get("/v1/calibrations", params={"limit": 2}).json()["items"].__len__() == 2
    assert active_env.get("/v1/calibrations", params={"status": "succeeded"}).json()["total"] == 0
    assert active_env.get("/v1/calibrations", params={"status": "queued,running"}).json()["total"] == 3
    assert active_env.get("/v1/calibrations", params={"test_type": "bending"}).json()["total"] == 3
    assert active_env.get("/v1/calibrations", params={"recipe_id": "bending3p-param"}).json()["total"] == 3
    assert active_env.get("/v1/calibrations", params={"recipe_id": "nope"}).json()["total"] == 0


# ---------------------------------------------------------------- E5–E7
def test_e5_e6_e7_after_run(active_env):
    jid = active_env.post_job(calibration_body()).json()["job_id"]
    st = active_env.get(f"/v1/calibrations/{jid}").json()
    check(st, {"schema_version": str, "job_id": str, "status": str, "phase": STR_OR_NONE, "iter": int, "best_J": (int, float, type(None)),
               "progress": (dict, type(None)), "cancel_requested": bool, "created_at": str, "started_at": STR_OR_NONE,
               "finished_at": STR_OR_NONE, "created_by": str, "warnings": list, "error": (dict, type(None))})
    assert st["status"] == "queued" and st["created_by"] == "matnexus-test"
    run_all(active_env)
    st = active_env.get(f"/v1/calibrations/{jid}").json()
    assert st["status"] == "succeeded" and st["iter"] == 3 and st["best_J"] is not None and st["error"] is None
    assert ISO.match(st["started_at"]) and ISO.match(st["finished_at"])
    res = active_env.get(f"/v1/calibrations/{jid}/result")
    assert res.status_code == 200
    check(res.json(), {"schema_version": str, "job_id": str, "status": str, "artifacts": list})
    assert res.json()["job_id"] == jid
    for a in res.json()["artifacts"]:
        check(a, {"artifact_id": str, "kind": str, "media_type": str, "filename": str, "bytes": int, "sha256": str})
        assert "path" not in a and "rel_path" not in a
    h = active_env.get(f"/v1/calibrations/{jid}/history")
    assert h.status_code == 200
    check(h.json(), {"schema_version": str, "items": list, "total": int, "limit": int, "offset": int})
    assert [i["eval_index"] for i in h.json()["items"]] == [0, 1, 2]
    for it in h.json()["items"]:
        check(it, {"eval_index": int, "stage": str, "theta": dict, "J": (int, float, type(None)), "status": str})
    assert active_env.get(f"/v1/calibrations/{jid}/history", params={"stage": "plastic"}).json()["total"] == 1
    assert [i["eval_index"] for i in active_env.get(f"/v1/calibrations/{jid}/history", params={"limit": 1, "offset": 1}).json()["items"]] == [1]


def test_e6_not_succeeded_is_409(active_env):
    jid = active_env.post_job(calibration_body()).json()["job_id"]
    r = active_env.get(f"/v1/calibrations/{jid}/result")
    assert r.status_code == 409
    e = r.json()["error"]
    assert e["code"] == "JOB_NOT_SUCCEEDED" and e["details"][0]["status"] == "queued"


# ---------------------------------------------------------------- E8
def test_e8_cancel_queued(active_env):
    jid = active_env.post_job(calibration_body()).json()["job_id"]
    r = active_env.client.post(f"/v1/calibrations/{jid}/cancel", headers=active_env.svc)
    assert r.status_code == 202
    check(r.json(), {"job_id": str, "status": str, "cancel_requested": bool})
    assert r.json()["status"] == "cancelled"
    r2 = active_env.client.post(f"/v1/calibrations/{jid}/cancel", headers=active_env.svc)
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "JOB_ALREADY_FINISHED"


def test_unknown_job_is_404(active_env):
    for path in ("", "/result", "/history"):
        r = active_env.get(f"/v1/calibrations/cal_00000000000000000000000000{path}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert active_env.client.post("/v1/calibrations/cal_nope/cancel", headers=active_env.svc).status_code == 404


# ---------------------------------------------------------------- E9 (자세한 규칙은 test_b9_artifacts)
def test_e9_download_headers(active_env):
    jid = active_env.post_job(calibration_body()).json()["job_id"]
    run_all(active_env)
    art = next(a for a in active_env.get(f"/v1/calibrations/{jid}/result").json()["artifacts"] if a["kind"] == "history")
    r = active_env.get(f"/v1/artifacts/{art['artifact_id']}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith("attachment; filename=")
    assert r.headers["etag"] == f'"sha256:{art["sha256"]}"'
    assert int(r.headers["content-length"]) == art["bytes"] == len(r.content)


# ---------------------------------------------------------------- E10–E12
def test_e10_list_recipes(active_env):
    r = active_env.get("/v1/recipes")
    assert r.status_code == 200
    page = r.json()
    check(page, {"schema_version": str, "items": list, "total": int, "limit": int, "offset": int})
    assert page["total"] == 1
    check(page["items"][0], {"recipe_ref": str, "recipe_id": str, "version": int, "kind": str, "test_type": str, "title": str,
                              "standards": list, "compliance_available": bool})
    assert page["items"][0]["recipe_ref"] == RECIPE_REF and page["items"][0]["compliance_available"] is True
    assert active_env.get("/v1/recipes", params={"test_type": "compression"}).json()["total"] == 0


def test_e11_recipe_public(active_env):
    r = active_env.get("/v1/recipes/bending3p-param/versions/1")
    assert r.status_code == 200
    body = r.json()
    check(body, {"schema_version": str, "recipe_ref": str, "kind": str, "test_type": str, "title": str, "standards": list,
                 "unit_system": str, "parameters": list, "material_slots": list, "specimen": dict, "responses": list,
                 "compliance": dict, "status": str})
    check(body["parameters"][0], {"name": str, "dimension": str, "unit_si": str, "default": "any", "range": list, "matnexus_key": "any"})
    check(body["material_slots"][0], {"slot": str, "region": list})
    check(body["responses"][0], {"response_key": str, "test_channel": str})
    check(body["compliance"], {"available": bool, "value_m_per_n": "any"})
    assert body["specimen"]["geometry_kinds"] == ["box", "layered_box", "stp"] and body["status"] == "active"
    text = r.text
    assert "deck/main.k" not in text and "extractor" not in text  # 덱·파일 비노출
    assert active_env.get("/v1/recipes/bending3p-param/versions/9").status_code == 404
    assert active_env.get("/v1/recipes/nothing/versions/1").status_code == 404


def test_e12_registry(env):
    r = env.get("/v1/registry")
    assert r.status_code == 200
    body = r.json()
    check(body, {"schema_version": str, "plugins": dict})
    assert {"optimizer", "solver", "material_model", "geometry_mesher", "test_spec"} <= set(body["plugins"])
    for plugins in body["plugins"].values():
        for p in plugins:
            check(p, {"name": str, "version": str, "aliases": list, "capabilities": "any"})
    names = {p["name"]: p for p in body["plugins"]["material_model"]}
    assert "mat024" in names["mat024_coeff"]["aliases"] and "mat024" in names["mat024_table"]["aliases"]
    assert "lsdyna" in {p["name"] for p in body["plugins"]["solver"]}


# ---------------------------------------------------------------- A1–A4
def test_a1_register_recipe(env):
    r = env.add_recipe()
    assert r.status_code == 202, r.text
    body = r.json()
    check(body, {"schema_version": str, "recipe_ref": str, "recipe_id": str, "version": int, "content_hash": str, "status": str,
                 "verification_id": str})
    assert body["recipe_ref"] == RECIPE_REF and body["status"] == "verifying" and re.match(r"^sha256:[0-9a-f]{64}$", body["content_hash"])
    assert body["verification_id"].startswith("ver_")
    again = env.add_recipe()  # 같은 해시 -> 200 기존 레코드
    assert again.status_code == 200 and again.json()["verification_id"] == body["verification_id"]


def test_a2_a3_a4(env):
    reg = env.add_recipe().json()
    # 검증 전: verifying
    lst = env.client.get("/v1/admin/recipes", headers=env.admin)
    assert lst.status_code == 200
    check(lst.json(), {"schema_version": str, "items": list, "total": int, "limit": int, "offset": int})
    row = lst.json()["items"][0]
    check(row, {"recipe_ref": str, "recipe_id": str, "version": int, "kind": str, "test_type": str, "status": str, "content_hash": str,
                "created_at": str, "created_by": str, "retired_at": STR_OR_NONE, "retire_reason": STR_OR_NONE, "warnings_count": int})
    assert row["status"] == "verifying" and row["created_by"] == "admin-test"
    # A4: verifying 은 폐기 불가
    r = env.client.post("/v1/admin/recipes/bending3p-param/versions/1/retire", json={"reason": "시험"}, headers=env.admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECIPE_NOT_RETIRABLE"
    run_all(env)
    # A3
    d = env.client.get("/v1/admin/recipes/bending3p-param/versions/1", headers=env.admin)
    assert d.status_code == 200
    body = d.json()
    check(body, {"recipe_ref": str, "status": str, "manifest": dict, "files": list, "verification": dict, "attempts": list})
    check(body["files"][0], {"path": str, "bytes": int, "sha256": str})
    assert body["status"] == "active" and body["manifest"]["recipe_id"] == "bending3p-param"
    check(body["verification"], {"status": str, "checks": list})
    assert "manifest.yaml" in [f["path"] for f in body["files"]]
    assert env.client.get("/v1/admin/recipes", params={"status": "active"}, headers=env.admin).json()["total"] == 1
    assert env.client.get("/v1/admin/recipes", params={"status": "retired"}, headers=env.admin).json()["total"] == 0
    # A4: 폐기 -> 200, 이미 retired 면 200 그대로
    r = env.client.post("/v1/admin/recipes/bending3p-param/versions/1/retire", json={"reason": "구형"}, headers=env.admin)
    assert r.status_code == 200
    check(r.json(), {"recipe_ref": str, "status": str, "retired_at": str, "retire_reason": str})
    assert r.json()["status"] == "retired" and r.json()["retire_reason"] == "구형" and ISO.match(r.json()["retired_at"])
    again = env.client.post("/v1/admin/recipes/bending3p-param/versions/1/retire", json={"reason": "또"}, headers=env.admin)
    assert again.status_code == 200 and again.json()["retire_reason"] == "구형"
    assert reg["recipe_ref"] == RECIPE_REF
    # 폐기 후 E10 목록에서 빠지고 E11 은 조회됨
    assert env.get("/v1/recipes").json()["total"] == 0
    assert env.get("/v1/recipes/bending3p-param/versions/1").json()["status"] == "retired"


def test_a4_reason_validation(env):
    env.activate_recipe()
    p = "/v1/admin/recipes/bending3p-param/versions/1/retire"
    assert env.client.post(p, json={}, headers=env.admin).status_code == 400
    assert env.client.post(p, json={"reason": "x" * 501}, headers=env.admin).status_code == 400
    assert env.client.post("/v1/admin/recipes/none-such/versions/1/retire", json={"reason": "r"}, headers=env.admin).status_code == 404


def test_error_body_shape(env):
    r = env.client.get("/v1/calibrations/cal_x")  # 인증 없음
    assert r.status_code == 401
    check(r.json(), {"schema_version": str, "error": dict})
    check(r.json()["error"], {"code": str, "message": str})
    r = env.post_job({"schema_version": "1.0"})
    assert r.status_code == 400
    e = r.json()["error"]
    assert e["code"] == "VALIDATION_FAILED" and e["details"]
    for d in e["details"]:
        check(d, {"code": str, "message": str})
