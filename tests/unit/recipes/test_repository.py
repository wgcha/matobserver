"""B-7(b): 레시피 등록·상태 전이 — 해시 규칙, 멱등·409, 검증 결과 상태, 폐기 후 RECIPE_RETIRED (recipe.md §5, api.md §8)."""

from __future__ import annotations

import io
import os
import zipfile

import pytest

from matobserver.recipes.bundle import content_hash, scan_tree

from be_support import RECIPE_DIR, RECIPE_REF, calibration_body, error_codes, make_hooks, recipe_zip

URL = "/v1/admin/recipes/bending3p-param/versions/{v}"


def detail(e, v=1):
    return e.client.get(URL.format(v=v), headers=e.admin).json()


def retire(e, v=1, reason="사유"):
    return e.client.post(URL.format(v=v) + "/retire", json={"reason": reason}, headers=e.admin)


def edited(note):
    return recipe_zip(edit_manifest=lambda t: t.replace("fixture: 3-point", f"fixture {note}: 3-point"))


# ---------------------------------------------------------------- 해시
def test_server_hash_equals_local_formula(env):
    entries, _ = scan_tree(RECIPE_DIR)
    r = env.add_recipe()
    assert r.json()["content_hash"] == content_hash(entries)
    d = detail(env)
    assert d["content_hash"] == r.json()["content_hash"]
    assert sorted(f["path"] for f in d["files"]) == [e.path for e in entries]
    assert {f["path"]: f["sha256"] for f in d["files"]} == {e.path: e.sha256 for e in entries}


def test_same_content_different_zip_bytes_has_same_hash(env):
    a = env.add_recipe(recipe_zip()).json()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # 압축 방식·순서가 달라도
        for p in sorted((p for p in RECIPE_DIR.rglob("*") if p.is_file()), reverse=True):
            zf.writestr(p.relative_to(RECIPE_DIR).as_posix(), p.read_bytes())
    b = env.add_recipe(buf.getvalue())
    assert b.status_code == 200 and b.json()["content_hash"] == a["content_hash"]


# ---------------------------------------------------------------- 멱등·충돌
def test_idempotent_reregistration_for_every_live_state(env):
    first = env.add_recipe()
    assert first.status_code == 202  # verifying
    assert env.add_recipe().status_code == 200
    env.worker().run_once()  # active
    again = env.add_recipe()
    assert again.status_code == 200 and again.json()["status"] == "active" and again.json()["verification_id"] == first.json()["verification_id"]
    assert retire(env).status_code == 200  # retired
    again = env.add_recipe()
    assert again.status_code == 200 and again.json()["status"] == "retired"
    assert env.client.get("/v1/admin/recipes", headers=env.admin).json()["total"] == 1  # 새 레코드가 생기지 않는다


def test_conflict_409_for_different_hash_in_verifying_active_retired(env):
    env.add_recipe()
    r = env.add_recipe(edited("v2"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECIPE_VERSION_CONFLICT"  # verifying
    env.worker().run_once()
    assert env.add_recipe(edited("v2")).status_code == 409  # active
    retire(env)
    assert env.add_recipe(edited("v2")).status_code == 409  # retired
    assert detail(env)["status"] == "retired"  # 기존 레코드 불변


def test_new_version_with_new_content_is_accepted(env):
    env.activate_recipe()
    r = env.add_recipe(recipe_zip(version=2))
    assert r.status_code == 202 and r.json()["recipe_ref"] == "bending3p-param@2"


def test_rejected_attempt_allows_new_upload_and_keeps_history(env):
    first = env.add_recipe().json()
    env.worker(make_hooks(verify=lambda r, s, strict: {"status": "failed", "checks": [{"code": "X", "result": "fail"}]})).run_once()
    assert detail(env)["status"] == "rejected"
    second = env.add_recipe(edited("fixed"))
    assert second.status_code == 202 and second.json()["verification_id"] != first["verification_id"]
    env.worker().run_once()
    d = detail(env)
    assert d["status"] == "active" and d["verification_id"] == second.json()["verification_id"]
    assert [a["verification_id"] for a in d["attempts"]] == [first["verification_id"]]
    assert d["attempts"][0]["status"] == "rejected"
    assert env.add_recipe(edited("fixed")).status_code == 200  # active 와 같은 해시 -> 멱등
    # 이전 시도와 같은 내용을 다시 올려도 active 가 이미 있어 충돌
    assert env.add_recipe().status_code == 409


def test_same_content_after_rejected_is_accepted_again(env):
    env.add_recipe()
    env.worker(make_hooks(verify=lambda r, s, strict: {"status": "failed", "checks": []})).run_once()
    again = env.add_recipe()
    assert again.status_code == 202
    assert len(detail(env)["attempts"]) == 1


# ---------------------------------------------------------------- 상태 전이
def test_verification_statuses_map_to_recipe_status(env):
    cases = [("passed", "active"), ("passed_with_warnings", "active"), ("failed", "rejected"), ("error", "rejected")]
    for i, (rep, expected) in enumerate(cases, start=1):
        env.add_recipe(recipe_zip(version=i))
        env.worker(make_hooks(verify=lambda r, s, strict, rep=rep: {"status": rep, "checks": []})).run_once()
        assert detail(env, i)["status"] == expected, rep


def test_strict_rejects_warnings_both_statically_and_after_verification(env):
    warn_zip = recipe_zip(extra={"deck/extra.k": b"*KEYWORD\n*DATABASE_SECFORC\n  1.0e-3\n*END\n"},
                          edit_manifest=lambda t: t.replace("files: [deck/contact.k]", "files: [deck/contact.k, deck/extra.k]"))
    r = env.add_recipe(warn_zip, strict=True)
    assert r.status_code == 400 and "RECIPE_OUTPUT_INTERVAL_HARDCODED" in error_codes(r)
    lenient = env.add_recipe(warn_zip, strict=False)
    assert lenient.status_code == 202 and lenient.json()["warnings"][0]["code"] == "RECIPE_OUTPUT_INTERVAL_HARDCODED"
    assert detail(env)["warnings_count"] == 1
    # strict 로 올린 레시피가 passed_with_warnings 를 받으면 rejected
    env.add_recipe(recipe_zip(version=2), strict=True)
    warn_hooks = make_hooks(verify=lambda r, s, strict: {"status": "passed_with_warnings", "checks": [{"code": "W", "result": "warn"}]})
    w = env.worker(warn_hooks)
    while w.run_once():
        pass
    assert detail(env, 1)["status"] == "active"  # 비 strict 는 경고가 있어도 active
    assert detail(env, 2)["status"] == "rejected"


def test_verification_exception_rejects_recipe_with_error_report(env):
    def boom(recipe, services, strict):
        raise RuntimeError(f"검증기 고장 {services.workdir()}")

    env.add_recipe()
    env.worker(make_hooks(verify=boom)).run_once()
    d = detail(env)
    assert d["status"] == "rejected" and d["verification"]["status"] == "error"
    assert d["verification"]["error"]["code"] == "INTERNAL_ERROR" and str(env.root) not in str(d["verification"])


def test_verifier_missing_rejects_recipe(env):
    env.add_recipe()
    env.worker(make_hooks(verify=None)).run_once()
    assert detail(env)["status"] == "rejected"


def test_only_active_recipes_are_usable(env):
    env.add_recipe()
    assert env.get("/v1/recipes").json()["total"] == 0
    assert env.get("/v1/recipes/bending3p-param/versions/1").status_code == 404  # verifying 은 공개 안 됨
    env.worker().run_once()
    assert env.get("/v1/recipes").json()["total"] == 1


# ---------------------------------------------------------------- 폐기
def test_retired_recipe_is_refused_for_new_jobs_but_old_jobs_survive(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    e.worker().run_once()
    assert retire(e, reason="구형 지그").status_code == 200
    r = e.post_job(calibration_body())
    assert r.status_code == 400 and "RECIPE_RETIRED" in error_codes(r)
    assert e.get(f"/v1/calibrations/{jid}").json()["status"] == "succeeded"  # 기존 잡·결과는 그대로
    assert e.get(f"/v1/calibrations/{jid}/result").status_code == 200
    d = detail(e)
    assert d["status"] == "retired" and d["retire_reason"] == "구형 지그" and d["files"]  # 파일 유지


def test_retired_job_snapshot_still_resolves_for_a_queued_job(active_env):
    """폐기 전에 만들어진 큐 잡은 폐기 후에도 실행된다 (잡은 recipe_ref 와 content_hash 로 재현)."""
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    retire(e)
    e.worker().run_once()
    assert e.get(f"/v1/calibrations/{jid}").json()["status"] == "succeeded"


def test_retire_is_idempotent_and_keeps_first_reason(active_env):
    e = active_env
    assert retire(e, reason="처음").json()["retire_reason"] == "처음"
    assert retire(e, reason="나중").json()["retire_reason"] == "처음"


def test_retire_verifying_and_rejected_are_409(env):
    env.add_recipe()
    r = retire(env)
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECIPE_NOT_RETIRABLE"
    env.worker(make_hooks(verify=lambda r_, s, strict: {"status": "failed", "checks": []})).run_once()
    assert retire(env).status_code == 409  # rejected 도 폐기 대상이 아니다


def test_retired_cannot_be_revived_by_reupload(active_env):
    e = active_env
    retire(e)
    r = e.add_recipe()
    assert r.status_code == 200 and r.json()["status"] == "retired"  # 되살리기 없음


# ---------------------------------------------------------------- 정적 검사 실패와 번들 오류 (A1 400)
def test_a1_static_failure_is_400_with_recipe_codes_and_nothing_stored(env):
    bad = recipe_zip(edit_manifest=lambda t: t.replace("force_scale: 4.0", "force_scale: 2.0"))
    r = env.add_recipe(bad)
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert "RECIPE_SYMMETRY_INVALID" in error_codes(r)
    assert env.client.get("/v1/admin/recipes", headers=env.admin).json()["total"] == 0
    store = env.config.path("recipes.store_dir")
    assert not [p for p in store.rglob("*") if p.is_file()]  # 스테이징도 정리됨


@pytest.mark.parametrize("payload,code", [
    (b"not a zip", "RECIPE_BUNDLE_INVALID"),
    (b"", "RECIPE_BUNDLE_INVALID"),
])
def test_a1_bad_bundle_codes(env, payload, code):
    r = env.add_recipe(payload)
    assert r.status_code == 400 and code in error_codes(r)


def test_a1_bundle_without_manifest_or_with_unsafe_path(env):
    r = env.add_recipe(_zip_without_manifest())
    assert r.status_code == 400 and "RECIPE_BUNDLE_INVALID" in error_codes(r)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.yaml", "x: 1")
        zf.writestr("../evil.k", "x")
    r = env.add_recipe(buf.getvalue())
    assert r.status_code == 400 and "RECIPE_PATH_UNSAFE" in error_codes(r)


def _zip_without_manifest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("deck/main.k", "*KEYWORD\n*END\n")
    return buf.getvalue()


def test_a1_oversized_bundle_rejected(make_env):
    e = make_env(config_over={"recipes.max_bundle_bytes": 1000})
    r = e.add_recipe()
    assert r.status_code == 400 and "RECIPE_BUNDLE_INVALID" in error_codes(r)


def test_a1_unpacked_size_and_file_count_limits(make_env):
    e = make_env(config_over={"recipes.max_files": 3})
    assert "RECIPE_BUNDLE_INVALID" in error_codes(e.add_recipe())
    e2 = make_env(config_over={"recipes.max_unpacked_bytes": 500})
    assert "RECIPE_BUNDLE_INVALID" in error_codes(e2.add_recipe())


# ---------------------------------------------------------------- 불변·저장
def test_stored_files_are_read_only_and_paths_hidden(env):
    env.add_recipe()
    root = env.config.path("recipes.store_dir")
    files = [p for p in root.rglob("*") if p.is_file() and ".staging" not in p.parts]
    assert files and all((p.stat().st_mode & 0o222) == 0 for p in files)
    body = env.client.get(URL.format(v=1), headers=env.admin).text
    assert str(root) not in body and "store_rel" not in body


def test_stored_under_recipe_version_verification_dir(env):
    r = env.add_recipe().json()
    root = env.config.path("recipes.store_dir")
    assert (root / "bending3p-param" / "1" / r["verification_id"] / "manifest.yaml").is_file()


def test_manifest_and_files_exposed_in_a3_but_not_in_public_e11(active_env):
    e = active_env
    d = detail(e)
    assert d["manifest"]["recipe_id"] == "bending3p-param" and any(f["path"] == "deck/main.k" for f in d["files"])
    pub = e.get("/v1/recipes/bending3p-param/versions/1").text
    assert "deck/main.k" not in pub and "verification" not in pub


def test_recipe_ref_constant():
    assert RECIPE_REF == "bending3p-param@1" and os.path.isdir(RECIPE_DIR)
