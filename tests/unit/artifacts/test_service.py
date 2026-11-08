"""ArtifactService 단위 시험 (api.md §6, module-boundaries.md §4.5)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from matobserver.artifacts.service import (OWNER_JOB, OWNER_RECIPE_VERIFICATION, ArtifactExpired, ArtifactNotFound, ArtifactService,
                                           KIND_MEDIA_TYPES, file_sha256)
from matobserver.jobs.db import Database

from be_support import make_config


@pytest.fixture
def svc(tmp_path):
    cfg = make_config(tmp_path)
    return ArtifactService(Database(cfg.path("jobstore.sqlite_path")), cfg)


def make_file(svc, owner="cal_a", name="x.csv", content="a,b\n", sub="artifacts", kind=OWNER_JOB):
    d = svc.work_root(kind, owner) / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


def test_roots_are_per_owner_kind(svc):
    wd = svc.config.path("paths.work_dir")
    assert svc.work_root(OWNER_JOB, "cal_1") == wd / "cal_1"
    assert svc.work_root(OWNER_RECIPE_VERIFICATION, "ver_1") == wd / "_recipes" / "ver_1"
    assert svc.artifact_root(OWNER_JOB, "cal_1") == wd / "cal_1" / "artifacts"
    with pytest.raises(ValueError):
        svc.work_root("other", "x")


def test_register_in_artifact_root_records_metadata(svc):
    p = make_file(svc, name="history.csv", content="eval,J\n1,0.5\n")
    aid = svc.register(OWNER_JOB, "cal_a", p, "history", slot="body", test_key="t1")
    rec = svc.get(aid)
    assert aid.startswith("art_") and rec.owner_kind == OWNER_JOB and rec.owner_id == "cal_a"
    assert (rec.rel_path, rec.filename, rec.kind, rec.slot, rec.test_key) == ("history.csv", "history.csv", "history", "body", "t1")
    assert rec.bytes == p.stat().st_size and rec.sha256 == file_sha256(p) and rec.media_type == "text/csv"
    ref = rec.ref()
    assert "rel_path" not in ref and "owner_id" not in ref and ref["artifact_id"] == aid  # 경로·소유자 비노출


def test_register_from_elsewhere_in_work_folder_copies_into_artifacts(svc):
    p = make_file(svc, name="mesh.k", sub="run_3/out")
    aid = svc.register(OWNER_JOB, "cal_a", p, "mesh")
    rec = svc.get(aid)
    assert rec.rel_path == "mesh.k" and (svc.artifact_root(OWNER_JOB, "cal_a") / "mesh.k").read_text(encoding="utf-8") == "a,b\n"
    assert p.exists()  # 원본은 그대로


def test_same_filename_gets_unique_destination_and_new_id(svc):
    a = svc.register(OWNER_JOB, "cal_a", make_file(svc, name="f.csv", sub="t1"), "history")
    b = svc.register(OWNER_JOB, "cal_a", make_file(svc, name="f.csv", content="other\n", sub="t2"), "history")
    ra, rb = svc.get(a), svc.get(b)
    assert a != b and ra.rel_path == "f.csv" and rb.rel_path == "f-1.csv" and ra.sha256 != rb.sha256


def test_register_outside_work_folder_and_missing_file(svc, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    with pytest.raises(ValueError):
        svc.register(OWNER_JOB, "cal_a", outside, "history")
    sibling = make_file(svc, owner="cal_b")
    with pytest.raises(ValueError):  # 다른 소유자의 작업 폴더
        svc.register(OWNER_JOB, "cal_a", sibling, "history")
    with pytest.raises(FileNotFoundError):
        svc.register(OWNER_JOB, "cal_a", svc.work_root(OWNER_JOB, "cal_a") / "nope.csv", "history")
    with pytest.raises(ValueError):
        svc.register("bogus", "cal_a", outside, "history")


def test_register_via_symlink_escape_is_blocked(svc, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    d = svc.work_root(OWNER_JOB, "cal_a") / "artifacts"
    d.mkdir(parents=True)
    os.symlink(secret, d / "link.txt")
    with pytest.raises(ValueError):
        svc.register(OWNER_JOB, "cal_a", d / "link.txt", "history")


def test_registered_file_is_read_only(svc):
    p = make_file(svc)
    svc.register(OWNER_JOB, "cal_a", p, "history")
    assert p.stat().st_mode & 0o222 == 0


def test_media_type_by_kind_override_and_guess(svc):
    p = make_file(svc, name="a.bin", content="x")
    assert svc.get(svc.register(OWNER_JOB, "cal_a", p, "card_lsdyna")).media_type == KIND_MEDIA_TYPES["card_lsdyna"]
    q = make_file(svc, name="b.json", content="{}")
    assert svc.get(svc.register(OWNER_JOB, "cal_a", q, "unknown_kind")).media_type == "application/json"  # 확장자로 추정
    r = make_file(svc, name="c.weird", content="x")
    assert svc.get(svc.register(OWNER_JOB, "cal_a", r, "unknown_kind")).media_type == "application/octet-stream"
    s = make_file(svc, name="d.csv", content="x")
    assert svc.get(svc.register(OWNER_JOB, "cal_a", s, "history", media_type="text/plain", filename="내보내기.csv")).filename == "내보내기.csv"


def test_list_for_owner_is_scoped_and_ordered(svc):
    ids = [svc.register(OWNER_JOB, "cal_a", make_file(svc, name=f"{i}.csv"), "history") for i in range(3)]
    svc.register(OWNER_JOB, "cal_b", make_file(svc, owner="cal_b"), "history")
    svc.register(OWNER_RECIPE_VERIFICATION, "ver_a", make_file(svc, owner="ver_a", kind=OWNER_RECIPE_VERIFICATION), "verification_bundle")
    assert [r.artifact_id for r in svc.list_for_owner(OWNER_JOB, "cal_a")] == ids
    assert len(svc.list_for_owner(OWNER_RECIPE_VERIFICATION, "ver_a")) == 1 and svc.list_for_owner(OWNER_JOB, "none") == []


def test_resolve_for_download_returns_real_path_inside_root(svc):
    aid = svc.register(OWNER_JOB, "cal_a", make_file(svc), "history")
    rec, path = svc.resolve_for_download(aid)
    assert path.is_file() and path.is_relative_to(svc.artifact_root(OWNER_JOB, "cal_a").resolve()) and rec.artifact_id == aid


@pytest.mark.parametrize("rel", ["../../escape.txt", "/etc/hostname", "sub/../../x"])
def test_resolve_blocks_tampered_rel_paths(svc, rel):
    aid = svc.register(OWNER_JOB, "cal_a", make_file(svc), "history")
    with svc.db.tx() as c:
        c.execute("UPDATE artifacts SET rel_path=? WHERE artifact_id=?", (rel, aid))
    with pytest.raises(ArtifactNotFound):
        svc.resolve_for_download(aid)


def test_resolve_unknown_and_missing(svc):
    with pytest.raises(ArtifactNotFound):
        svc.resolve_for_download("art_nope")
    aid = svc.register(OWNER_JOB, "cal_a", make_file(svc), "history")
    (svc.artifact_root(OWNER_JOB, "cal_a") / "x.csv").chmod(0o644)
    (svc.artifact_root(OWNER_JOB, "cal_a") / "x.csv").unlink()
    with pytest.raises(ArtifactNotFound):
        svc.resolve_for_download(aid)


def test_cannot_serve_artifact_through_another_owner_root(svc):
    """같은 상대 경로여도 소유자 루트로만 해석된다: cal_b 에 같은 이름 파일이 있어도 cal_a 의 레코드는 cal_a 루트를 본다."""
    a = svc.register(OWNER_JOB, "cal_a", make_file(svc, owner="cal_a", content="AAA"), "history")
    svc.register(OWNER_JOB, "cal_b", make_file(svc, owner="cal_b", content="BBB"), "history")
    _, path = svc.resolve_for_download(a)
    assert path.read_text(encoding="utf-8") == "AAA"


def test_retention_boundary_and_none(tmp_path):
    now = [datetime(2026, 11, 8, tzinfo=timezone.utc)]
    cfg = make_config(tmp_path, **{"artifacts.retention_days": 2})
    s = ArtifactService(Database(cfg.path("jobstore.sqlite_path")), cfg, clock=lambda: now[0])
    aid = s.register(OWNER_JOB, "cal_a", make_file(s), "history")
    now[0] += timedelta(days=2)
    s.resolve_for_download(aid)  # 정확히 2일이면 아직 유효
    now[0] += timedelta(seconds=1)
    with pytest.raises(ArtifactExpired):
        s.resolve_for_download(aid)
    cfg2 = make_config(tmp_path, **{"artifacts.retention_days": None})
    s2 = ArtifactService(s.db, cfg2, clock=lambda: now[0] + timedelta(days=9999))
    s2.resolve_for_download(aid)


def test_job_services_workdir_cannot_escape(tmp_path):
    from matobserver.jobs.services import JobServices, NeverCancel
    from matobserver.jobs.store import JobStore
    from matobserver.core.registry import Registry
    from be_support import PLUGINS_DIR

    cfg = make_config(tmp_path)
    db = Database(cfg.path("jobstore.sqlite_path"))
    art = ArtifactService(db, cfg)
    js = JobServices(owner_kind="job", owner_id="cal_x", registry=Registry.from_paths([PLUGINS_DIR]), config=cfg, store=JobStore(db),
                     artifacts=art, cancel=NeverCancel())
    assert js.workdir("runs", "r1").is_dir() and js.workdir() == art.work_root("job", "cal_x")
    with pytest.raises(ValueError):
        js.workdir("..", "cal_other")
    p = js.workdir("artifacts") / "r.csv"
    p.write_text("x")
    aid = js.add_artifact(p, "history", test_key="t1")
    assert art.get(aid).test_key == "t1"
    # 검증(recipe_verification) 서비스는 이력·진행률을 기록하지 않는다
    v = JobServices(owner_kind="recipe_verification", owner_id="ver_x", registry=js.registry, config=cfg, store=JobStore(db), artifacts=art,
                    cancel=NeverCancel())
    v.record_eval({"eval_index": 0, "stage": "elastic"})
    v.progress("elastic")
    assert v.workdir() == art.work_root("recipe_verification", "ver_x")
