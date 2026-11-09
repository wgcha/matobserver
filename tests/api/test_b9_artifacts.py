"""B-9: artifact — 경로 비노출, 루트 밖 접근 차단, ETag, 410 (api.md §6)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from be_support import build_env, calibration_body
from matobserver.artifacts.service import OWNER_JOB


def finished_job(e, hooks=None):
    jid = e.post_job(calibration_body()).json()["job_id"]
    w = e.worker(hooks)
    while w.run_once():
        pass
    return jid


def art_of(e, jid, kind="history"):
    return next(a for a in e.get(f"/v1/calibrations/{jid}/result").json()["artifacts"] if a["kind"] == kind)


def test_download_content_headers_and_no_path(active_env):
    e = active_env
    jid = finished_job(e)
    art = art_of(e, jid)
    r = e.get(f"/v1/artifacts/{art['artifact_id']}")
    assert r.status_code == 200
    assert r.text == "eval_index,J\n0,1.0\n1,0.5\n2,0.33\n"
    assert r.headers["content-disposition"] == 'attachment; filename="history.csv"'
    assert r.headers["etag"] == f'"sha256:{art["sha256"]}"'
    assert r.headers["content-type"].startswith("text/csv")
    blob = r.text + str(dict(r.headers)) + str(e.get(f"/v1/calibrations/{jid}/result").json()) + e.get(f"/v1/calibrations/{jid}").text
    for forbidden in (str(e.root), "rel_path", "work_dir", "/artifacts/history.csv"):
        assert forbidden not in blob


def test_etag_conditional_get(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    etag = f'"sha256:{art["sha256"]}"'
    r = e.client.get(f"/v1/artifacts/{art['artifact_id']}", headers={**e.svc, "If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""
    r = e.client.get(f"/v1/artifacts/{art['artifact_id']}", headers={**e.svc, "If-None-Match": '"sha256:other"'})
    assert r.status_code == 200


def test_unknown_artifact_404(active_env):
    r = active_env.get("/v1/artifacts/art_01JC0000000000000000000000")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_non_uuid_style_ids_cannot_traverse(active_env):
    for bad in ("..%2f..%2fetc%2fpasswd", "%2e%2e/%2e%2e/etc/passwd", "art_x/../../y"):
        r = active_env.get(f"/v1/artifacts/{bad}")
        assert r.status_code in (404, 400)
        assert "root:" not in r.text


def test_tampered_row_escaping_root_is_404(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    secret = e.root / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    with e.ctx.db.tx() as c:
        c.execute("UPDATE artifacts SET rel_path=? WHERE artifact_id=?", ("../../../../secret.txt", art["artifact_id"]))
    r = e.get(f"/v1/artifacts/{art['artifact_id']}")
    assert r.status_code == 404 and "top secret" not in r.text
    with e.ctx.db.tx() as c:
        c.execute("UPDATE artifacts SET rel_path=? WHERE artifact_id=?", (str(secret), art["artifact_id"]))
    r = e.get(f"/v1/artifacts/{art['artifact_id']}")
    assert r.status_code == 404 and "top secret" not in r.text


def test_symlink_pointing_outside_root_is_404(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    rec = e.ctx.artifacts.get(art["artifact_id"])
    root = e.ctx.artifacts.artifact_root(rec.owner_kind, rec.owner_id)
    secret = e.root / "secret2.txt"
    secret.write_text("outside", encoding="utf-8")
    link = root / "link.csv"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("symlink 권한 없음 (Windows)")
    with e.ctx.db.tx() as c:
        c.execute("UPDATE artifacts SET rel_path='link.csv' WHERE artifact_id=?", (art["artifact_id"],))
    r = e.get(f"/v1/artifacts/{art['artifact_id']}")
    assert r.status_code == 404 and "outside" not in r.text


def test_missing_file_is_404(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    rec = e.ctx.artifacts.get(art["artifact_id"])
    path = e.ctx.artifacts.artifact_root(rec.owner_kind, rec.owner_id) / rec.rel_path
    os.chmod(path, 0o644)
    path.unlink()
    assert e.get(f"/v1/artifacts/{art['artifact_id']}").status_code == 404


def test_register_outside_owner_work_folder_rejected(active_env):
    e = active_env
    outside = e.root / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        e.ctx.artifacts.register(OWNER_JOB, "cal_x", outside, "history")


def test_artifact_is_read_only_and_immutable_ids(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    rec = e.ctx.artifacts.get(art["artifact_id"])
    path = e.ctx.artifacts.artifact_root(rec.owner_kind, rec.owner_id) / rec.rel_path
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    assert (path.stat().st_mode & 0o222) == 0
    # 같은 이름을 다시 만들면 새 ID·새 파일 (기존 내용 불변)
    fresh = path.parent / "history.csv"
    other = e.root / "again.csv"
    other.write_text("a,b\n", encoding="utf-8")
    inside = path.parent.parent / "again.csv"
    inside.write_text("a,b\n", encoding="utf-8")
    new_id = e.ctx.artifacts.register(OWNER_JOB, rec.owner_id, inside, "history", filename="history.csv")
    assert new_id != rec.artifact_id
    assert fresh.read_text(encoding="utf-8") == "eval_index,J\n0,1.0\n1,0.5\n2,0.33\n"


def test_expired_artifact_is_410(tmp_path):
    now = [datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)]
    e = build_env(tmp_path, config_over={"artifacts.retention_days": 1}, clock=lambda: now[0])
    try:
        e.activate_recipe()
        art = art_of(e, finished_job(e))
        assert e.get(f"/v1/artifacts/{art['artifact_id']}").status_code == 200
        now[0] += timedelta(days=1, seconds=1)
        r = e.get(f"/v1/artifacts/{art['artifact_id']}")
        assert r.status_code == 410 and r.json()["error"]["code"] == "ARTIFACT_EXPIRED"
    finally:
        from matobserver.recipes.repository import unlock_tree

        e.client.close()
        unlock_tree(tmp_path / "data" / "recipes")


def test_no_retention_means_never_expires(active_env):
    e = active_env
    art = art_of(e, finished_job(e))
    assert e.ctx.artifacts.is_expired(e.ctx.artifacts.get(art["artifact_id"])) is False


def test_recipe_verification_artifacts_are_served_from_their_own_root(env):
    def verify(recipe, services, strict):
        p = services.workdir("artifacts") / "report.json"
        p.write_text("{}", encoding="utf-8")
        aid = services.add_artifact(p, "verification_bundle")
        return {"status": "passed", "checks": [], "artifacts": [{"artifact_id": aid, "kind": "verification_bundle"}]}

    env.hooks.verify_recipe = verify
    env.add_recipe()
    env.worker().run_once()
    d = env.client.get("/v1/admin/recipes/bending3p-param/versions/1", headers=env.admin).json()
    aid = d["verification"]["artifacts"][0]["artifact_id"]
    r = env.get(f"/v1/artifacts/{aid}")
    assert r.status_code == 200 and r.text == "{}"
    assert str(env.root) not in r.text

