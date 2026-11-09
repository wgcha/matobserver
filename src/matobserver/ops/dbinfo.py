"""JobStore DB 읽기 전용 조회 (status·doctor 용). DB 가 없거나 표가 없어도 예외 대신 빈 값."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def queue_info(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"queued": 0, "running_job_id": None}
    if not path.is_file():
        return out
    try:
        c = _ro(path)
        try:
            out["queued"] = int(c.execute("SELECT COUNT(*) FROM queue WHERE state='queued'").fetchone()[0])
            r = c.execute("SELECT job_id FROM jobs WHERE status='running' ORDER BY seq LIMIT 1").fetchone()
            out["running_job_id"] = r["job_id"] if r else None
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def active_recipes(path: Path) -> int | None:
    if not path.is_file():
        return 0
    try:
        c = _ro(path)
        try:
            return int(c.execute("SELECT COUNT(*) FROM recipes WHERE status='active'").fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return None


def heartbeat_age_s(path: Path, now: float | None = None) -> float | None:
    if not path.is_file():
        return None
    try:
        c = _ro(path)
        try:
            r = c.execute("SELECT ts FROM worker_heartbeat WHERE id=1").fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    return None if r is None else max(0.0, (time.time() if now is None else now) - float(r["ts"]))


def inspect(path: Path) -> dict[str, Any]:
    """`{version, integrity}` (읽기 전용). 열기 실패는 sqlite3.Error 로 올라간다."""
    from ..jobs.db import stored_version

    c = _ro(path)
    try:
        integ = c.execute("PRAGMA integrity_check").fetchone()[0]
        return {"version": stored_version(c), "integrity": integ}
    finally:
        c.close()
