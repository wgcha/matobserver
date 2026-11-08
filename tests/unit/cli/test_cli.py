"""B-8: CLI `recipe add/list/retire` 종료 코드 (recipe.md §6): 0 / 2 정적 검사 / 3 rejected / 4 409 / 5 통신·인증."""

from __future__ import annotations

import io
import json
import shutil
import threading

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from matobserver import cli

from be_support import ADMIN_TOKEN, RECIPE_DIR, SVC_TOKEN, make_hooks

OK, USAGE, STATIC, REJECTED, CONFLICT, COMM = 0, 1, 2, 3, 4, 5


class Cli:
    def __init__(self, env, tmp_path, token=ADMIN_TOKEN):
        self.env, self.tmp = env, tmp_path
        self.token = token
        self.config_path = tmp_path / "cli-config.yaml"
        self.config_path.write_text(yaml.safe_dump(env.config.to_dict()), encoding="utf-8")
        self.calls = []

    def factory(self, base_url, token, timeout):
        self.calls.append((base_url, token, timeout))
        return TestClient(self.env.app, base_url="http://testserver", headers={"Authorization": f"Bearer {token}"})

    def run(self, *argv, token="default", stdin="", env_extra=None, factory=None):
        out, err = io.StringIO(), io.StringIO()
        env = {} if token is None else {cli.TOKEN_ENV: self.token if token == "default" else token}
        env.update(env_extra or {})
        code = cli.main(["--config", str(self.config_path), *argv], env=env, stdout=out, stderr=err, stdin=io.StringIO(stdin),
                        client_factory=factory or self.factory)
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def c(env, tmp_path):
    return Cli(env, tmp_path)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "myrecipe"
    shutil.copytree(RECIPE_DIR, d)
    return d


def worker_thread(env, hooks=None):
    stop = threading.Event()
    t = threading.Thread(target=env.worker(hooks).run_forever, args=(stop, 0.01), daemon=True)
    t.start()
    return stop, t


# ---------------------------------------------------------------- recipe add
def test_add_accepts_and_exits_0(c, folder):
    code, out, err = c.run("recipe", "add", str(folder))
    assert code == OK, err
    assert "bending3p-param@1" in out and "verifying" in out and "sha256:" in out
    assert "로컬 content_hash" in err  # 로컬 계산값도 출력
    assert c.env.client.get("/v1/admin/recipes", headers=c.env.admin).json()["total"] == 1


def test_add_json_prints_api_response(c, folder):
    code, out, _ = c.run("recipe", "add", str(folder), "--json")
    body = json.loads(out)
    assert code == OK and body["recipe_ref"] == "bending3p-param@1" and body["status"] == "verifying"


def test_add_local_hash_equals_server_hash(c, folder):
    code, out, err = c.run("recipe", "add", str(folder), "--json")
    local = next(l for l in err.splitlines() if l.startswith("로컬 content_hash")).split(": ")[1].strip()
    assert local == json.loads(out)["content_hash"] and "서버 content_hash" not in err


def test_add_wait_passes_exit_0(c, folder):
    stop, t = worker_thread(c.env)
    try:
        code, out, err = c.run("recipe", "add", str(folder), "--wait", "--timeout", "30")
    finally:
        stop.set(), t.join(10)
    assert code == OK, err
    assert "active" in out and "CHK_DEFAULT_RUN" in out


def test_add_wait_rejected_exits_3(c, folder):
    hooks = make_hooks(verify=lambda r, s, strict: {"status": "failed", "checks": [{"code": "CHK_X", "result": "fail", "detail": "나쁨"}]})
    stop, t = worker_thread(c.env, hooks)
    try:
        code, out, _ = c.run("recipe", "add", str(folder), "--wait", "--timeout", "30")
    finally:
        stop.set(), t.join(10)
    assert code == REJECTED and "rejected" in out and "CHK_X" in out


def test_add_wait_timeout_exits_5(c, folder):
    code, _, err = c.run("recipe", "add", str(folder), "--wait", "--timeout", "0.05")  # 워커가 없어 verifying 으로 남는다
    assert code == COMM and "시간이 초과" in err


def test_add_local_static_failure_exits_2_without_calling_server(c, folder):
    p = folder / "manifest.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("force_scale: 4.0", "force_scale: 2.0"), encoding="utf-8")
    code, _, err = c.run("recipe", "add", str(folder))
    assert code == STATIC and "RECIPE_SYMMETRY_INVALID" in err
    assert c.calls == []  # 서버를 부르지 않았다


def test_add_not_a_folder_exits_2(c, tmp_path):
    assert c.run("recipe", "add", str(tmp_path / "nope"))[0] == STATIC


def test_add_strict_turns_warning_into_exit_2(c, folder):
    (folder / "deck" / "extra.k").write_text("*KEYWORD\n*DATABASE_SECFORC\n  1.0e-3\n*END\n", encoding="utf-8")
    m = folder / "manifest.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("files: [deck/contact.k]", "files: [deck/contact.k, deck/extra.k]"), encoding="utf-8")
    code, _, err = c.run("recipe", "add", str(folder), "--strict")
    assert code == STATIC and "RECIPE_OUTPUT_INTERVAL_HARDCODED" in err
    code, _, err = c.run("recipe", "add", str(folder))
    assert code == OK and "경고 RECIPE_OUTPUT_INTERVAL_HARDCODED" in err


def test_add_conflict_exits_4(c, folder):
    assert c.run("recipe", "add", str(folder))[0] == OK
    m = folder / "manifest.yaml"
    m.write_text(m.read_text(encoding="utf-8").replace("fixture: 3-point", "fixture changed: 3-point"), encoding="utf-8")
    code, _, err = c.run("recipe", "add", str(folder))
    assert code == CONFLICT and "RECIPE_VERSION_CONFLICT" in err


def test_add_same_content_again_is_idempotent_exit_0(c, folder):
    assert c.run("recipe", "add", str(folder))[0] == OK
    assert c.run("recipe", "add", str(folder))[0] == OK


def test_add_server_side_static_failure_exits_2(c, folder):
    """로컬 검사는 통과했지만 서버가 400 으로 거절하면 2."""
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"schema_version": "1.0", "error": {
        "code": "VALIDATION_FAILED", "message": "m", "details": [{"code": "RECIPE_SOLVER_UNKNOWN", "message": "서버 레지스트리에 없음"}]}}))
    code, _, err = c.run("recipe", "add", str(folder), factory=lambda u, t, to: httpx.Client(base_url="http://x", transport=transport))
    assert code == STATIC and "RECIPE_SOLVER_UNKNOWN" in err


def test_add_auth_failures_exit_5(c, folder):
    assert c.run("recipe", "add", str(folder), token="wrong-token")[0] == COMM  # 401
    code, _, err = c.run("recipe", "add", str(folder), token=SVC_TOKEN)  # 403
    assert code == COMM and "FORBIDDEN" in err


def test_add_without_token_exits_5_and_does_not_call_server(c, folder):
    code, _, err = c.run("recipe", "add", str(folder), token=None)
    assert code == COMM and cli.TOKEN_ENV in err and c.calls == []


def test_add_connection_error_exits_5(c, folder):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    code, _, err = c.run("recipe", "add", str(folder), factory=lambda u, t, to: httpx.Client(base_url="http://x", transport=httpx.MockTransport(boom)))
    assert code == COMM and "통신" in err


def test_add_server_5xx_exits_5(c, folder):
    code, _, _ = c.run("recipe", "add", str(folder), factory=lambda u, t, to: httpx.Client(
        base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down"))))
    assert code == COMM


# ---------------------------------------------------------------- 토큰 처리
def test_token_from_file_and_never_printed(c, folder, tmp_path):
    tf = tmp_path / "tok"
    tf.write_text(ADMIN_TOKEN + "\n", encoding="utf-8")
    code, out, err = c.run("recipe", "add", str(folder), "--token-file", str(tf), token=None)
    assert code == OK and ADMIN_TOKEN not in out + err
    assert c.calls[0][1] == ADMIN_TOKEN


def test_no_plaintext_token_argument_exists(c, folder):
    code, _, _ = c.run("recipe", "list", "--token", ADMIN_TOKEN)
    assert code == USAGE


def test_server_address_defaults_to_config_and_can_be_overridden(c, folder):
    c.run("recipe", "list")
    c.run("recipe", "list", "--server", "http://other:9999")
    assert c.calls[0][0] == c.env.config.get("cli.server_url") and c.calls[1][0] == "http://other:9999"


# ---------------------------------------------------------------- recipe list
def test_list_table_and_filters(c, folder):
    c.env.activate_recipe()
    code, out, _ = c.run("recipe", "list")
    assert code == OK
    header, row = out.splitlines()[0], out.splitlines()[1]
    assert header.split() == ["recipe_ref", "kind", "test_type", "status", "warnings", "created_at"]
    assert row.split()[:4] == ["bending3p-param@1", "parametric", "bending", "active"]
    assert "총 1건" in out
    assert "총 0건" in c.run("recipe", "list", "--status", "retired")[1]
    assert "총 1건" in c.run("recipe", "list", "--status", "retired", "--status", "active")[1]
    assert "총 0건" in c.run("recipe", "list", "--test-type", "compression")[1]
    assert "총 1건" in c.run("recipe", "list", "--recipe-id", "bending3p-param", "--limit", "5", "--offset", "0")[1]


def test_list_json(c):
    c.env.activate_recipe()
    code, out, _ = c.run("recipe", "list", "--json")
    assert code == OK and json.loads(out)["items"][0]["recipe_ref"] == "bending3p-param@1"


def test_list_auth_error_exits_5(c):
    assert c.run("recipe", "list", token="nope")[0] == COMM
    assert c.run("recipe", "list", token=SVC_TOKEN)[0] == COMM


# ---------------------------------------------------------------- recipe retire
def test_retire_ok_and_idempotent(c):
    c.env.activate_recipe()
    code, out, _ = c.run("recipe", "retire", "bending3p-param@1", "--reason", "구형", "--yes")
    assert code == OK and "retired" in out
    assert c.run("recipe", "retire", "bending3p-param@1", "--reason", "또", "--yes")[0] == OK
    assert c.env.client.get("/v1/admin/recipes/bending3p-param/versions/1", headers=c.env.admin).json()["retire_reason"] == "구형"


def test_retire_verifying_exits_4(c, folder):
    c.run("recipe", "add", str(folder))
    code, _, err = c.run("recipe", "retire", "bending3p-param@1", "--reason", "x", "--yes")
    assert code == CONFLICT and "RECIPE_NOT_RETIRABLE" in err


def test_retire_unknown_and_auth_exit_5(c):
    assert c.run("recipe", "retire", "nothing-here@3", "--reason", "x", "--yes")[0] == COMM  # 404
    c.env.activate_recipe()
    assert c.run("recipe", "retire", "bending3p-param@1", "--reason", "x", "--yes", token=SVC_TOKEN)[0] == COMM


def test_retire_requires_reason_and_valid_ref(c):
    assert c.run("recipe", "retire", "bending3p-param@1", "--yes")[0] == USAGE
    code, _, err = c.run("recipe", "retire", "bending3p-param", "--reason", "x", "--yes")
    assert code == USAGE and "recipe_id@version" in err


def test_retire_confirmation_prompt(c):
    c.env.activate_recipe()
    code, _, err = c.run("recipe", "retire", "bending3p-param@1", "--reason", "x", stdin="n\n")
    assert code == USAGE and "취소" in err
    assert c.env.client.get("/v1/admin/recipes/bending3p-param/versions/1", headers=c.env.admin).json()["status"] == "active"
    assert c.run("recipe", "retire", "bending3p-param@1", "--reason", "x", stdin="y\n")[0] == OK


# ---------------------------------------------------------------- serve / worker
def test_serve_passes_app_host_port_to_runner(c):
    got = {}
    code = cli.main(["--config", str(c.config_path), "serve", "--host", "0.0.0.0", "--port", "9001"], env={}, stderr=io.StringIO(),
                    serve_runner=lambda app, host, port: got.update(app=app, host=host, port=port))
    assert code == OK and (got["host"], got["port"]) == ("0.0.0.0", 9001) and callable(got["app"])
    got.clear()
    cli.main(["--config", str(c.config_path), "serve"], env={}, serve_runner=lambda app, host, port: got.update(host=host, port=port))
    assert (got["host"], got["port"]) == (c.env.config.get("api.host"), c.env.config.get("api.port"))


def test_worker_once_drains_queue_and_exits(c):
    from be_support import calibration_body

    c.env.activate_recipe()
    ids = [c.env.post_job(calibration_body()).json()["job_id"] for _ in range(2)]
    seen = []
    code = cli.main(["--config", str(c.config_path), "worker", "--once"], env={}, stderr=io.StringIO(),
                    hooks=make_hooks(run=lambda spec, svc: seen.append(spec.job_id) or {}))
    assert code == OK and seen == ids
    assert all(c.env.get(f"/v1/calibrations/{j}").json()["status"] == "succeeded" for j in ids)


def test_worker_refuses_second_instance(c):
    from matobserver.jobs.worker import WorkerLock

    lock = WorkerLock(c.env.config.path("paths.data_dir") / "worker.lock")
    assert lock.acquire()
    try:
        err = io.StringIO()
        code = cli.main(["--config", str(c.config_path), "worker", "--once"], env={}, stderr=err, hooks=make_hooks())
        assert code == USAGE and "이미 실행 중" in err.getvalue()
    finally:
        lock.release()


def test_worker_once_recovers_lost_jobs(c):
    from be_support import calibration_body

    c.env.activate_recipe()
    jid = c.env.post_job(calibration_body()).json()["job_id"]
    c.env.ctx.store.claim_next()  # 이전 워커가 죽으며 남긴 running
    assert cli.main(["--config", str(c.config_path), "worker", "--once"], env={}, stderr=io.StringIO(), hooks=make_hooks()) == OK
    assert c.env.get(f"/v1/calibrations/{jid}").json()["error"]["code"] == "WORKER_LOST"


def test_help_and_version_exit_0(capsys):
    assert cli.main(["--version"], env={}) == 0
    assert cli.main(["recipe", "add", "--help"], env={}) == 0
    assert "matobserver" in capsys.readouterr().out


def test_no_command_is_usage_error():
    assert cli.main([], env={}) == USAGE
