"""B-3: 인증 — 토큰 없음/불일치/서비스 토큰으로 admin = 401/401/403 (api.md §2)."""

from __future__ import annotations

import pytest

from be_support import ADMIN_TOKEN, SVC_TOKEN, calibration_body

JOB = "cal_01JC0000000000000000000000"
SERVICE_ENDPOINTS = [
    ("GET", "/v1/calibrations"),
    ("POST", "/v1/calibrations"),
    ("GET", f"/v1/calibrations/{JOB}"),
    ("GET", f"/v1/calibrations/{JOB}/result"),
    ("GET", f"/v1/calibrations/{JOB}/history"),
    ("POST", f"/v1/calibrations/{JOB}/cancel"),
    ("POST", "/v1/files"),
    ("GET", "/v1/artifacts/art_01JC0000000000000000000000"),
    ("GET", "/v1/recipes"),
    ("GET", "/v1/recipes/bending3p-param/versions/1"),
    ("GET", "/v1/registry"),
]
ADMIN_ENDPOINTS = [
    ("POST", "/v1/admin/recipes"),
    ("GET", "/v1/admin/recipes"),
    ("GET", "/v1/admin/recipes/bending3p-param/versions/1"),
    ("POST", "/v1/admin/recipes/bending3p-param/versions/1/retire"),
]


def call(env, method, path, headers=None):
    return env.client.request(method, path, headers=headers or {}, json=None if method == "GET" else {})


def test_health_needs_no_token(env):
    assert env.client.get("/v1/health").status_code == 200


@pytest.mark.parametrize("method,path", SERVICE_ENDPOINTS + ADMIN_ENDPOINTS)
def test_no_token_is_401_auth_required(env, method, path):
    r = call(env, method, path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("method,path", SERVICE_ENDPOINTS + ADMIN_ENDPOINTS)
def test_wrong_token_is_401_auth_invalid(env, method, path):
    r = call(env, method, path, {"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
def test_service_token_on_admin_is_403(env, method, path):
    r = call(env, method, path, env.svc)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "Bearer ", SVC_TOKEN, "Token " + SVC_TOKEN])
def test_malformed_authorization_is_401(env, header):
    r = env.client.get("/v1/calibrations", headers={"Authorization": header})
    assert r.status_code == 401
    assert r.json()["error"]["code"] in ("AUTH_REQUIRED", "AUTH_INVALID")


def test_service_token_reaches_service_endpoints(env):
    assert env.client.get("/v1/calibrations", headers=env.svc).status_code == 200
    assert env.client.get("/v1/registry", headers=env.svc).status_code == 200


def test_admin_token_covers_service_scope_and_admin(env):
    assert env.client.get("/v1/calibrations", headers=env.admin).status_code == 200
    assert env.client.get("/v1/admin/recipes", headers=env.admin).status_code == 200


def test_created_by_is_token_name(active_env):
    jid = active_env.post_job(calibration_body()).json()["job_id"]
    assert active_env.get(f"/v1/calibrations/{jid}").json()["created_by"] == "matnexus-test"
    jid2 = active_env.client.post("/v1/calibrations", json=calibration_body(), headers=active_env.admin).json()["job_id"]
    assert active_env.get(f"/v1/calibrations/{jid2}").json()["created_by"] == "admin-test"


def test_no_configured_tokens_rejects_everything(make_env):
    e = make_env(config_over={"auth.service_tokens": [], "auth.admin_tokens": []})
    assert e.client.get("/v1/calibrations", headers={"Authorization": f"Bearer {SVC_TOKEN}"}).status_code == 401
    assert e.client.get("/v1/admin/recipes", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 401
    assert e.client.get("/v1/health").status_code == 200


def test_sha256_prefix_and_uppercase_hash_accepted(make_env):
    from be_support import sha256_hex

    e = make_env(config_over={"auth.service_tokens": [{"name": "p", "sha256": "sha256:" + sha256_hex(SVC_TOKEN).upper()}]})
    assert e.client.get("/v1/calibrations", headers={"Authorization": f"Bearer {SVC_TOKEN}"}).status_code == 200


def test_token_never_echoed(env):
    r = env.client.get("/v1/calibrations", headers={"Authorization": "Bearer secret-value-xyz"})
    assert "secret-value-xyz" not in r.text
