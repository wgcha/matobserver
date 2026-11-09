"""SQLite 접속·스키마 (JobStore 저장소, api.md §5).

API 프로세스와 워커 프로세스가 같은 파일을 연다(WAL). 연결은 작업마다 짧게 열고 닫는다(프로세스·스레드 안전).
잡/큐/이력/파일/artifact/멱등 키/레시피 표가 한 DB에 있다(레시피 상태 전이와 검증 큐 등록을 한 트랜잭션으로 묶기 위해).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..core.errors import MatObserverError

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    name TEXT,
    status TEXT NOT NULL,
    phase TEXT,
    iter INTEGER NOT NULL DEFAULT 0,
    best_j REAL,
    progress_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    created_by TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_json TEXT,
    material_id TEXT,
    schema_version TEXT NOT NULL,
    request_hash TEXT,
    request_json TEXT,
    spec_json TEXT,
    result_json TEXT,
    result_schema_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS job_tests (
    job_id TEXT NOT NULL,
    test_key TEXT NOT NULL,
    test_type TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    recipe_ref TEXT NOT NULL,
    PRIMARY KEY (job_id, test_key)
);
CREATE TABLE IF NOT EXISTS history (
    job_id TEXT NOT NULL,
    eval_index INTEGER NOT NULL,
    stage TEXT NOT NULL,
    item_json TEXT NOT NULL,
    PRIMARY KEY (job_id, eval_index)
);
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    state TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_state ON queue(state, seq);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    filename TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    owner_kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    slot TEXT,
    test_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifacts_owner ON artifacts(owner_kind, owner_id);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    job_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    verification_id TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    kind TEXT NOT NULL,
    test_type TEXT NOT NULL,
    title TEXT NOT NULL,
    standards_json TEXT NOT NULL DEFAULT '[]',
    compliance_available INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL,
    files_json TEXT NOT NULL,
    store_rel TEXT NOT NULL,
    strict INTEGER NOT NULL DEFAULT 0,
    static_warnings_json TEXT NOT NULL DEFAULT '[]',
    warnings_count INTEGER NOT NULL DEFAULT 0,
    verification_json TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    retired_at TEXT,
    retire_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_recipes_ref ON recipes(recipe_id, version);
"""


# --------------------------------------------------------------------------------------
# 스키마 버전 (deploy.md §2.3): SQLite `PRAGMA user_version`. 앞으로만, 단계별 트랜잭션.
#   1 = 1.0~1.2 의 표 (user_version 0 + 표 있음은 1로 본다)
#   2 = 1.3: 워커 하트비트 표
# --------------------------------------------------------------------------------------
HEARTBEAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL NOT NULL,
    pid INTEGER,
    job_id TEXT
);
"""
MIGRATIONS: dict[int, str] = {2: HEARTBEAT_SCHEMA}
SCHEMA_VERSION = 2


class SchemaOutdated(MatObserverError):
    """DB 가 코드보다 옛 버전 -> `matobserver db migrate` 필요."""


class SchemaTooNew(MatObserverError):
    """DB 가 코드보다 새 버전 (다운그레이드 불가)."""


@dataclass(frozen=True)
class MigrateResult:
    from_version: int
    to_version: int
    backup: Path | None


def _has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone() is not None


def stored_version(conn: sqlite3.Connection) -> int:
    """파일의 스키마 버전. 표가 없으면 0(새 DB), user_version 0 + 표 있음은 1."""
    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if ver == 0 and _has_tables(conn):
        return 1
    return ver


def migrate(path: str | Path, *, backup: bool = True) -> MigrateResult:
    """DB 를 코드 버전까지 전진시킨다. 새 DB 는 최신 스키마로 만든다. 더 새 버전은 `SchemaTooNew`. 실패 시 롤백(예외)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        ver = stored_version(conn)
        if ver > SCHEMA_VERSION:
            raise SchemaTooNew(f"DB 스키마 버전 {ver} 이 코드({SCHEMA_VERSION})보다 새롭습니다")
        if ver == SCHEMA_VERSION:
            return MigrateResult(ver, ver, None)
        conn.execute("PRAGMA journal_mode=WAL")
        if ver == 0:
            conn.executescript(SCHEMA + HEARTBEAT_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return MigrateResult(0, SCHEMA_VERSION, None)
        bak: Path | None = None
        if backup:
            bak = path.with_name(f"{path.name}.bak-v{ver}")
            dst = sqlite3.connect(str(bak))
            try:
                conn.backup(dst)
            finally:
                dst.close()
        for target in range(ver + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in [x for x in MIGRATIONS[target].split(";") if x.strip()]:
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={target}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return MigrateResult(ver, SCHEMA_VERSION, bak)
    finally:
        conn.close()


class Database:
    """SQLite 파일 래퍼. `connect()`는 자동 커밋 연결, `tx()`는 `BEGIN IMMEDIATE` 트랜잭션.

    새 DB 는 최신 스키마로 만든다. 옛 버전은 `SchemaOutdated`(-> `matobserver db migrate`), 새 버전은 `SchemaTooNew`.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            ver = stored_version(conn)
        if ver == 0:
            migrate(self.path, backup=False)
        elif ver > SCHEMA_VERSION:
            raise SchemaTooNew(f"DB 스키마 버전 {ver} 이 코드({SCHEMA_VERSION})보다 새롭습니다")
        elif ver < SCHEMA_VERSION:
            raise SchemaOutdated(f"DB 스키마 버전 {ver} < {SCHEMA_VERSION}: `matobserver db migrate` 를 실행하세요")

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션(다른 쓰기와 직렬화). 예외 시 롤백."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()
