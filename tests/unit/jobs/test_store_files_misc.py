"""JobStore · FileService · 취소 토큰 · 경로 가리기 단위 시험."""

from __future__ import annotations

from pathlib import Path

import pytest

from matobserver.jobs.clock import iso, parse_iso
from matobserver.jobs.db import Database
from matobserver.jobs.services import DbCancelToken
from matobserver.jobs.worker import scrub_paths

from be_support import calibration_body

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


def upload(e, name="pkg.stp", data=STEP, purpose="geometry_stp", headers=None):
    return e.client.post("/v1/files", files={"file": (name, data, "application/step")}, data={"purpose": purpose}, headers=headers or e.svc)


# ---------------------------------------------------------------- E2 파일
def test_upload_ok_variants(env):
    assert upload(env, "A.STEP").status_code == 201
    assert upload(env, "dir/with/path.stp").json()["filename"] == "path.stp"  # 경로 성분 제거
    assert upload(env, "bom.stp", b"\xef\xbb\xbfISO-10303-21;\n").status_code == 201
    assert upload(env, "crlf.stp", b"ISO-10303-21;\r\nx").status_code == 201


@pytest.mark.parametrize("name,data,purpose", [
    ("pkg.txt", STEP, "geometry_stp"),
    ("pkg.stp", b"not step\n", "geometry_stp"),
    ("pkg.stp", b"", "geometry_stp"),
    ("pkg.stp", STEP, "other_purpose"),
    ("noext", STEP, "geometry_stp"),
])
def test_upload_rejected_400(env, name, data, purpose):
    r = upload(env, name, data, purpose)
    assert r.status_code == 400 and r.json()["error"]["code"] == "FILE_TYPE_UNSUPPORTED"
    files = env.config.path("paths.data_dir") / "files"
    assert not files.exists() or not list(files.iterdir())  # 거절된 파일은 남기지 않는다


def test_upload_too_large_413_and_cleanup(make_env):
    e = make_env(config_over={"files.max_bytes": 100})
    r = upload(e, data=STEP + b"x" * 200)
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    files = e.config.path("paths.data_dir") / "files"
    assert not files.exists() or not list(files.iterdir())
    assert upload(e, data=STEP).status_code == 201  # 한도 이내는 통과


def test_upload_missing_fields_400(env):
    assert env.client.post("/v1/files", data={"purpose": "geometry_stp"}, headers=env.svc).status_code == 400
    assert env.client.post("/v1/files", files={"file": ("a.stp", STEP)}, headers=env.svc).status_code == 400


def test_upload_records_sha_and_creator(env):
    r = upload(env).json()
    import hashlib

    assert r["sha256"] == hashlib.sha256(STEP).hexdigest() and r["bytes"] == len(STEP)
    rec = env.ctx.files.get(r["file_id"])
    assert rec["created_by"] == "matnexus-test" and rec["file_id"].startswith("fil_")
    assert env.ctx.files.path(rec).read_bytes() == STEP


def test_file_path_cannot_leave_root(env):
    fid = upload(env).json()["file_id"]
    rec = dict(env.ctx.files.get(fid))
    rec["rel_path"] = "../../etc/hostname"
    with pytest.raises(FileNotFoundError):
        env.ctx.files.path(rec)


def test_stp_file_is_copied_into_job_inputs(active_env):
    e = active_env
    fid = upload(e, data=(Path(__file__).parents[2] / "fixtures" / "backend" / "stp" / "block.stp").read_bytes()).json()["file_id"]
    r = e.post_job(calibration_body(geometry={"kind": "stp", "file_id": fid}))
    assert r.status_code == 201, r.text
    jid = r.json()["job_id"]
    assert (Path(e.config.get("paths.work_dir")) / jid / "inputs" / "stp" / f"{fid}.stp").is_file()


# ---------------------------------------------------------------- JobStore
def test_job_rows_and_tests(active_env):
    e = active_env
    jid = e.post_job(calibration_body(name="이름")).json()["job_id"]
    row = e.ctx.store.get_job(jid)
    assert row["name"] == "이름" and row["status"] == "queued" and row["iter"] == 0 and row["best_j"] is None
    assert e.ctx.store.job_tests(jid)[0]["recipe_id"] == "bending3p-param" and e.ctx.store.get_job("cal_none") is None
    assert parse_iso(row["created_at"]) and iso(parse_iso(row["created_at"])) == row["created_at"]


def test_created_job_lists_newest_first(active_env):
    e = active_env
    ids = [e.post_job(calibration_body(name=f"n{i}")).json()["job_id"] for i in range(4)]
    assert [j["job_id"] for j in e.ctx.store.list_jobs()[0]] == ids[::-1]


def test_history_overwrite_and_best_j_ignores_penalized(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    s = e.ctx.store
    s.record_eval(jid, {"eval_index": 0, "stage": "elastic", "J": 5.0, "status": "ok"})
    s.record_eval(jid, {"eval_index": 1, "stage": "plastic", "J": 0.1, "status": "penalized"})
    s.record_eval(jid, {"eval_index": 2, "stage": "diagnostics", "J": 0.01, "status": "ok"})  # 예산 밖 단계
    s.record_eval(jid, {"eval_index": 3, "stage": "plastic", "J": 2.0, "status": "ok"})
    row = s.get_job(jid)
    assert row["iter"] == 3 and row["best_j"] == 2.0  # 예산 단계(elastic·plastic) 평가 수, ok 평가 중 최소 J
    s.record_eval(jid, {"eval_index": 3, "stage": "plastic", "J": 1.0, "status": "ok"})  # 같은 인덱스는 덮어쓴다
    assert s.get_job(jid)["iter"] == 3 and s.get_job(jid)["best_j"] == 1.0
    with pytest.raises(ValueError):
        s.record_eval(jid, {"stage": "elastic"})


def test_progress_includes_elapsed_only_while_running(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    seen = []

    def run(spec, services):
        services.progress("plastic", stage_iter=4, max_evaluations=20, evaluations_failed=1, best_J_stage=0.5)
        seen.append(e.get(f"/v1/calibrations/{jid}").json())
        return {}

    from be_support import make_hooks

    e.worker(make_hooks(run=run)).run_once()
    p = seen[0]["progress"]
    assert seen[0]["phase"] == "plastic" and p["stage"] == "plastic" and p["stage_iter"] == 4 and p["evaluations_failed"] == 1
    assert "elapsed_s" in p
    assert "elapsed_s" not in (e.get(f"/v1/calibrations/{jid}").json()["progress"] or {})


# ---------------------------------------------------------------- 취소 토큰 / 경로 가리기
def test_db_cancel_token_polls_at_interval_and_latches(active_env):
    e = active_env
    jid = e.post_job(calibration_body()).json()["job_id"]
    t = [0.0]
    tok = DbCancelToken(e.ctx.store, jid, 5.0, lambda: t[0])
    assert tok.is_set() is False
    with e.ctx.db.tx() as c:
        c.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (jid,))
    t[0] = 1.0
    assert tok.is_set() is False  # 폴링 간격(5s) 안에는 DB 를 다시 보지 않는다
    t[0] = 5.0
    assert tok.is_set() is True
    with e.ctx.db.tx() as c:
        c.execute("UPDATE jobs SET cancel_requested=0 WHERE job_id=?", (jid,))
    t[0] = 100.0
    assert tok.is_set() is True  # 한 번 True 면 계속 True


def test_scrub_paths(tmp_path):
    a, b = tmp_path / "work", tmp_path / "data"
    msg = f"실패 {a}/cal_1/run.log 와 {b}/jobs.sqlite 및 {a.resolve()}"
    out = scrub_paths(msg, [a, b])
    assert str(a) not in out and str(b) not in out and out.count("<path>") == 3
    assert scrub_paths("경로 없음", [a]) == "경로 없음" and scrub_paths("x /", [Path("/")]) == "x /"


# ---------------------------------------------------------------- DB
def test_database_wal_and_isolated_transactions(tmp_path):
    db = Database(tmp_path / "d" / "x.sqlite")
    with db.connect() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO queue (kind, ref_id, state, enqueued_at) VALUES ('job','x','queued','t')")
            raise RuntimeError("롤백")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 0
