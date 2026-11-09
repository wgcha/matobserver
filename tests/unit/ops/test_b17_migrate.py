"""B-17: db migrate — user_version 전진, 백업, 실패 롤백, 새 DB 거부 (deploy.md §2.3)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from matobserver.jobs import db as jdb


def uv(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def make_v1(path):
    """1.0~1.2 시절 DB: 표는 있고 user_version=0, 하트비트 표 없음."""
    c = sqlite3.connect(path)
    c.executescript(jdb.SCHEMA)
    c.execute("INSERT INTO jobs (job_id, status, created_at, schema_version) VALUES ('cal_old','succeeded','2026-01-01T00:00:00Z','1.0')")
    c.commit()
    c.close()


def test_new_db_gets_latest_schema_without_backup(tmp_path):
    p = tmp_path / "db" / "j.sqlite"
    r = jdb.migrate(p)
    assert (r.from_version, r.to_version, r.backup) == (0, jdb.SCHEMA_VERSION, None)
    assert uv(p) == jdb.SCHEMA_VERSION and "worker_heartbeat" in tables(p)
    r2 = jdb.migrate(p)  # 이미 최신 -> 변경 없음
    assert r2.from_version == r2.to_version == jdb.SCHEMA_VERSION and r2.backup is None


def test_v1_db_is_advanced_with_backup_and_data_kept(tmp_path):
    p = tmp_path / "j.sqlite"
    make_v1(p)
    assert jdb.stored_version(sqlite3.connect(p)) == 1
    r = jdb.migrate(p)
    assert (r.from_version, r.to_version) == (1, jdb.SCHEMA_VERSION)
    assert r.backup is not None and r.backup.is_file() and r.backup.name == "j.sqlite.bak-v1"
    assert "worker_heartbeat" not in tables(r.backup) and uv(r.backup) == 0  # 백업은 이전 상태
    assert uv(p) == jdb.SCHEMA_VERSION and "worker_heartbeat" in tables(p)
    c = sqlite3.connect(p)
    assert c.execute("SELECT job_id FROM jobs").fetchall() == [("cal_old",)]  # 데이터 보존
    c.close()


def test_no_backup_option(tmp_path):
    p = tmp_path / "j.sqlite"
    make_v1(p)
    assert jdb.migrate(p, backup=False).backup is None and not list(tmp_path.glob("*.bak-*"))


def test_failed_step_rolls_back(tmp_path, monkeypatch):
    p = tmp_path / "j.sqlite"
    make_v1(p)
    monkeypatch.setattr(jdb, "MIGRATIONS", {2: "CREATE TABLE half_done (x INTEGER); INSERT INTO no_such_table VALUES (1)"})
    with pytest.raises(sqlite3.Error):
        jdb.migrate(p)
    assert "half_done" not in tables(p) and jdb.stored_version(sqlite3.connect(p)) == 1
    monkeypatch.undo()
    assert jdb.migrate(p).to_version == jdb.SCHEMA_VERSION  # 롤백 후 재시도 가능


def test_newer_db_is_refused(tmp_path):
    p = tmp_path / "j.sqlite"
    jdb.migrate(p)
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version={jdb.SCHEMA_VERSION + 5}")
    c.close()
    with pytest.raises(jdb.SchemaTooNew):
        jdb.migrate(p)
    with pytest.raises(jdb.SchemaTooNew):
        jdb.Database(p)


def test_database_requires_migrate_for_outdated(tmp_path):
    p = tmp_path / "j.sqlite"
    make_v1(p)
    with pytest.raises(jdb.SchemaOutdated):
        jdb.Database(p)
    jdb.migrate(p)
    jdb.Database(p)  # 이제 열린다


def test_cli_db_migrate_exit_codes(ops):
    db = ops.cfg().path("jobstore.sqlite_path")
    db.parent.mkdir(parents=True, exist_ok=True)
    make_v1(db)
    code, out, _ = ops.cli("db", "migrate", "--json")
    rep = json.loads(out)
    assert code == 0 and rep["from_version"] == 1 and rep["to_version"] == jdb.SCHEMA_VERSION and rep["changed"] and rep["backup"].endswith(".bak-v1")
    code, out, _ = ops.cli("db", "migrate", "--json")
    assert code == 0 and json.loads(out)["changed"] is False
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version=99")
    c.close()
    code, _, err = ops.cli("db", "migrate")
    assert code == 2 and "새롭습니다" in err


def test_cli_db_migrate_failure_is_1(ops, monkeypatch):
    db = ops.cfg().path("jobstore.sqlite_path")
    db.parent.mkdir(parents=True, exist_ok=True)
    make_v1(db)
    monkeypatch.setattr(jdb, "MIGRATIONS", {2: "INSERT INTO no_such_table VALUES (1)"})
    code, _, err = ops.cli("db", "migrate")
    assert code == 1 and "롤백" in err
