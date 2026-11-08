"""HTTP 공통 동작: 오류 본문, 페이지 파라미터, 잘못된 본문, 서버 경로 비노출."""

from __future__ import annotations

import json

import pytest

from matobserver.api.auth import TokenAuth
from matobserver.api.errors import ApiError

from be_support import calibration_body, sha256_hex


def test_unknown_path_404_uses_error_body(env):
    r = env.get("/v1/nothing")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_405(env):
    r = env.client.put("/v1/calibrations", headers=env.svc)
    assert r.status_code == 405 and "error" in r.json()


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 100000}, {"limit": "x"}, {"offset": -1}, {"offset": "y"}])
def test_bad_paging_is_400(env, params):
    r = env.get("/v1/calibrations", params=params)
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_FAILED"
    assert r.json()["error"]["details"][0]["code"] == "SCHEMA_INVALID"


def test_bad_filters_are_400(env):
    assert env.get("/v1/calibrations", params={"status": "weird"}).status_code == 400
    assert env.get("/v1/calibrations", params={"created_from": "yesterday"}).status_code == 400
    assert env.get("/v1/calibrations", params={"material_id": "x"}).status_code == 400
    assert env.get("/v1/calibrations", params={"created_from": "2020-01-01T00:00:00Z", "created_to": "2099-01-01T00:00:00Z"}).status_code == 200


def test_paging_defaults_from_config(make_env):
    e = make_env(config_over={"api.page_default_limit": 2, "api.page_max_limit": 3})
    e.activate_recipe()
    for _ in range(3):
        e.post_job(calibration_body())
    p = e.get("/v1/calibrations").json()
    assert p["limit"] == 2 and len(p["items"]) == 2 and p["total"] == 3
    assert e.get("/v1/calibrations", params={"limit": 4}).status_code == 400
    assert len(e.get("/v1/calibrations", params={"limit": 3, "offset": 2}).json()["items"]) == 1


def test_body_not_json_is_schema_invalid(env):
    r = env.client.post("/v1/calibrations", content=b"{not json", headers={**env.svc, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["details"][0]["code"] == "SCHEMA_INVALID"


def test_nan_and_infinity_rejected(env):
    raw = json.dumps(calibration_body()).replace('"max_evaluations": 20', '"max_evaluations": NaN')
    r = env.client.post("/v1/calibrations", content=raw.encode(), headers={**env.svc, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["details"][0]["code"] == "SCHEMA_INVALID"


def test_body_not_object_is_schema_invalid(env):
    r = env.client.post("/v1/calibrations", content=b"[1,2]", headers={**env.svc, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["details"][0]["code"] == "SCHEMA_INVALID"


def test_idempotency_key_length(env):
    r = env.post_job(calibration_body(), headers={"Idempotency-Key": "k" * 129})
    assert r.status_code == 400


def test_idempotency_key_expires_after_ttl(tmp_path):
    from datetime import datetime, timedelta, timezone

    from be_support import build_env

    now = [datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)]
    e = build_env(tmp_path, config_over={"api.idempotency_ttl_s": 60}, clock=lambda: now[0])
    try:
        e.activate_recipe()
        a = e.post_job(calibration_body(), headers={"Idempotency-Key": "once"})
        assert a.status_code == 201
        assert e.post_job(calibration_body(), headers={"Idempotency-Key": "once"}).status_code == 200
        now[0] += timedelta(seconds=61)
        b = e.post_job(calibration_body(), headers={"Idempotency-Key": "once"})
        assert b.status_code == 201 and b.json()["job_id"] != a.json()["job_id"]
    finally:
        from matobserver.recipes.repository import unlock_tree

        e.client.close()
        unlock_tree(tmp_path / "data" / "recipes")


def test_idempotency_not_stored_when_validation_fails(active_env):
    bad = calibration_body(optimizer="nope")
    assert active_env.post_job(bad, headers={"Idempotency-Key": "kk"}).status_code == 400
    ok = active_env.post_job(calibration_body(), headers={"Idempotency-Key": "kk"})
    assert ok.status_code == 201


def test_unhandled_error_is_500_without_details(active_env, monkeypatch):
    e = active_env

    def boom(*a, **k):
        raise RuntimeError(f"내부 비밀 {e.root}")

    monkeypatch.setattr(e.ctx.store, "list_jobs", boom)
    from fastapi.testclient import TestClient

    c = TestClient(e.app, raise_server_exceptions=False)
    r = c.get("/v1/calibrations", headers=e.svc)
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "비밀" not in r.text and str(e.root) not in r.text


class TestTokenAuthUnit:
    def make(self):
        return TokenAuth([{"name": "svc", "sha256": sha256_hex("s1")}], [{"name": "adm", "sha256": sha256_hex("a1")}])

    def test_principals(self):
        a = self.make()
        assert (a.authenticate("Bearer s1").name, a.authenticate("Bearer s1").admin) == ("svc", False)
        assert (a.authenticate("bearer a1").name, a.authenticate("bearer a1").admin) == ("adm", True)

    @pytest.mark.parametrize("hdr,code", [(None, "AUTH_REQUIRED"), ("", "AUTH_REQUIRED"), ("Bearer nope", "AUTH_INVALID"),
                                          ("Basic s1", "AUTH_INVALID"), ("Bearer", "AUTH_INVALID")])
    def test_failures(self, hdr, code):
        with pytest.raises(ApiError) as ei:
            self.make().authenticate(hdr)
        assert ei.value.status == 401 and ei.value.code == code

    def test_plaintext_is_never_stored(self):
        a = self.make()
        assert "s1" not in repr(vars(a)) and "a1" not in repr(vars(a))

    def test_uses_constant_time_compare(self, monkeypatch):
        import hmac

        calls = []
        real = hmac.compare_digest
        monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
        self.make().authenticate("Bearer s1")
        assert len(calls) >= 2  # 조기 종료 없이 모든 항목과 비교
