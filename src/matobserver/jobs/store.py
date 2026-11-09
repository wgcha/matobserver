"""JobStore: 잡·큐·이력·입력 파일·멱등 키 (SQLite, api.md §5).

상태 머신(api.md §5): queued -> running -> succeeded | failed | cancelled, queued -> cancelled.
워커 획득은 한 트랜잭션에서 원자적으로 한다(`claim_next`): 이미 실행 중인 항목(잡 또는 레시피 검증)이 있으면 획득하지 않는다
-> 워커 프로세스가 둘이어도 동시에 실행되는 항목은 최대 1건이다(B-5).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from ..core import errors as E
from ..core.errors import MatObserverError
from .clock import Clock, iso, parse_iso, utcnow
from .db import Database

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "queued", "running", "succeeded", "failed", "cancelled"
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
ALL_STATUSES = frozenset({QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED})

KIND_JOB = "job"
KIND_VERIFICATION = "recipe_verification"

#: iter(완료된 해석 평가 수)에 세는 history 단계. diagnostics·roundtrip·explicit_check 는 예산 밖이라 세지 않는다(api.md §4.9·§4.11).
BUDGET_STAGES = frozenset({"elastic", "plastic", "joint"})


class JobNotFound(MatObserverError):
    code = E.NOT_FOUND


class JobAlreadyFinished(MatObserverError):
    code = E.JOB_ALREADY_FINISHED


class IdempotencyKeyReused(MatObserverError):
    code = E.IDEMPOTENCY_KEY_REUSED


@dataclass(frozen=True)
class QueueItem:
    seq: int
    kind: str  # KIND_JOB | KIND_VERIFICATION
    ref_id: str  # job_id 또는 verification_id


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def job_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["progress"] = _loads(d.pop("progress_json"))
    d["warnings"] = _loads(d.pop("warnings_json")) or []
    d["error"] = _loads(d.pop("error_json"))
    d["cancel_requested"] = bool(d["cancel_requested"])
    return d


class JobStore:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self.db = db
        self.clock = clock

    def now(self) -> str:
        return iso(self.clock())

    # ------------------------------------------------------------------ 워커 하트비트 (deploy.md §2.1)
    def beat_worker(self, pid: int, job_id: str | None = None, *, ts: float | None = None) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO worker_heartbeat (id, ts, pid, job_id) VALUES (1,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, pid=excluded.pid, job_id=excluded.job_id",
                      (time.time() if ts is None else ts, pid, job_id))

    def last_heartbeat(self) -> dict[str, Any] | None:
        with self.db.connect() as c:
            row = c.execute("SELECT ts, pid, job_id FROM worker_heartbeat WHERE id=1").fetchone()
        return None if row is None else {"ts": row["ts"], "pid": row["pid"], "job_id": row["job_id"]}

    # ------------------------------------------------------------------ 생성
    def create_job(
        self,
        *,
        job_id: str,
        name: str | None,
        created_by: str | None,
        schema_version: str,
        request_hash: str,
        request: Mapping[str, Any],
        spec: Mapping[str, Any],
        warnings: Sequence[Mapping[str, Any]],
        material_id: str | None,
        tests: Sequence[Mapping[str, str]],
        idempotency_key: str | None = None,
        idempotency_hash: str | None = None,
    ) -> dict[str, Any]:
        """queued 잡을 등록하고 큐에 넣는다(한 트랜잭션). `tests`: `[{test_key, test_type, recipe_ref}]`."""
        now = self.now()
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO jobs (job_id, name, status, created_at, created_by, warnings_json, material_id,"
                " schema_version, request_hash, request_json, spec_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, name, QUEUED, now, created_by, _dumps(list(warnings)), material_id, schema_version,
                 request_hash, _dumps(request), _dumps(spec)),
            )
            for t in tests:
                c.execute(
                    "INSERT INTO job_tests (job_id, test_key, test_type, recipe_id, recipe_ref) VALUES (?,?,?,?,?)",
                    (job_id, t["test_key"], t["test_type"], t["recipe_ref"].split("@")[0], t["recipe_ref"]),
                )
            c.execute(
                "INSERT INTO queue (kind, ref_id, state, enqueued_at) VALUES (?,?,?,?)", (KIND_JOB, job_id, "queued", now)
            )
            if idempotency_key is not None:
                c.execute(
                    "INSERT OR REPLACE INTO idempotency (key, request_hash, job_id, created_at) VALUES (?,?,?,?)",
                    (idempotency_key, idempotency_hash or request_hash, job_id, now),
                )
        return self.get_job(job_id)  # type: ignore[return-value]

    def find_idempotent(self, key: str, body_hash: str, ttl_s: float) -> dict[str, Any] | None:
        """같은 키+같은 본문 해시 -> 기존 잡. 다른 본문 해시 -> IdempotencyKeyReused. 보존 기간이 지난 키는 없는 것으로 본다."""
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if self.clock() - parse_iso(row["created_at"]) > timedelta(seconds=ttl_s):
            with self.db.connect() as c:
                c.execute("DELETE FROM idempotency WHERE key=?", (key,))
            return None
        if row["request_hash"] != body_hash:
            raise IdempotencyKeyReused(f"Idempotency-Key {key!r} was used with a different request body")
        return self.get_job(row["job_id"])

    # ------------------------------------------------------------------ 조회
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return None if row is None else job_row_to_dict(row)

    def job_tests(self, job_id: str) -> list[dict[str, str]]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM job_tests WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_jobs(
        self,
        *,
        statuses: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        material_id: str | None = None,
        test_type: str | None = None,
        recipe_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append("j.status IN (%s)" % ",".join("?" * len(statuses)))
            args.extend(statuses)
        if created_from:
            where.append("j.created_at >= ?")
            args.append(created_from)
        if created_to:
            where.append("j.created_at <= ?")
            args.append(created_to)
        if material_id:
            where.append("j.material_id = ?")
            args.append(material_id)
        if test_type:
            where.append("EXISTS (SELECT 1 FROM job_tests t WHERE t.job_id=j.job_id AND t.test_type=?)")
            args.append(test_type)
        if recipe_id:
            where.append("EXISTS (SELECT 1 FROM job_tests t WHERE t.job_id=j.job_id AND t.recipe_id=?)")
            args.append(recipe_id)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        with self.db.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM jobs j{cond}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT j.* FROM jobs j{cond} ORDER BY j.seq DESC LIMIT ? OFFSET ?", [*args, limit, offset]
            ).fetchall()
        return [job_row_to_dict(r) for r in rows], total

    # ------------------------------------------------------------------ 취소 (api.md §4.12)
    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """queued -> 즉시 cancelled. running -> cancel_requested=1 (워커가 종료 후 cancelled). 종결 상태 -> JobAlreadyFinished."""
        now = self.now()
        with self.db.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            status = row["status"]
            if status in TERMINAL:
                raise JobAlreadyFinished(f"job {job_id} is already {status}")
            if status == QUEUED:
                c.execute(
                    "UPDATE jobs SET status=?, cancel_requested=1, finished_at=? WHERE job_id=?", (CANCELLED, now, job_id)
                )
                c.execute(
                    "UPDATE queue SET state='cancelled', finished_at=? WHERE kind=? AND ref_id=? AND state='queued'",
                    (now, KIND_JOB, job_id),
                )
            else:
                c.execute("UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (job_id,))
        return self.get_job(job_id)  # type: ignore[return-value]

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.db.connect() as c:
            row = c.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    # ------------------------------------------------------------------ 큐 / 워커 획득 (api.md §5)
    def enqueue(self, kind: str, ref_id: str, conn: sqlite3.Connection | None = None) -> None:
        now = self.now()
        sql = "INSERT INTO queue (kind, ref_id, state, enqueued_at) VALUES (?,?,?,?)"
        if conn is not None:
            conn.execute(sql, (kind, ref_id, "queued", now))
        else:
            with self.db.tx() as c:
                c.execute(sql, (kind, ref_id, "queued", now))

    def claim_next(self) -> QueueItem | None:
        """가장 오래된 queued 항목을 원자적으로 running 으로 바꾼다. 이미 running 항목이 있으면 None (워커 1개·1건 규칙)."""
        now = self.now()
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM queue WHERE state='running' LIMIT 1").fetchone():
                return None
            row = c.execute("SELECT * FROM queue WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            c.execute("UPDATE queue SET state='running', started_at=? WHERE seq=?", (now, row["seq"]))
            if row["kind"] == KIND_JOB:
                c.execute(
                    "UPDATE jobs SET status=?, started_at=?, phase='preprocess' WHERE job_id=? AND status=?",
                    (RUNNING, now, row["ref_id"], QUEUED),
                )
            return QueueItem(seq=row["seq"], kind=row["kind"], ref_id=row["ref_id"])

    def release_queue(self, kind: str, ref_id: str) -> None:
        """항목의 큐 행을 done 으로 (running 해제)."""
        with self.db.tx() as c:
            c.execute(
                "UPDATE queue SET state='done', finished_at=? WHERE kind=? AND ref_id=? AND state='running'",
                (self.now(), kind, ref_id),
            )

    def recover_lost(self) -> dict[str, list[str]]:
        """워커 (재)시작 시: running 으로 남은 항목을 정리한다. 잡 -> failed(WORKER_LOST). 검증 -> 호출자가 레시피를 rejected 로 바꾼다."""
        now = self.now()
        lost: dict[str, list[str]] = {KIND_JOB: [], KIND_VERIFICATION: []}
        with self.db.tx() as c:
            for row in c.execute("SELECT * FROM queue WHERE state='running' ORDER BY seq").fetchall():
                lost[row["kind"]].append(row["ref_id"])
                c.execute("UPDATE queue SET state='done', finished_at=? WHERE seq=?", (now, row["seq"]))
            for job_id in lost[KIND_JOB]:
                c.execute(
                    "UPDATE jobs SET status=?, finished_at=?, error_json=? WHERE job_id=? AND status=?",
                    (FAILED, now, _dumps({"code": E.WORKER_LOST, "message": "워커가 비정상 종료되어 잡이 중단되었습니다."}),
                     job_id, RUNNING),
                )
            # 잡 상태는 running 인데 큐 행이 없는 불일치도 정리한다.
            for row in c.execute("SELECT job_id FROM jobs WHERE status=?", (RUNNING,)).fetchall():
                c.execute(
                    "UPDATE jobs SET status=?, finished_at=?, error_json=? WHERE job_id=?",
                    (FAILED, now, _dumps({"code": E.WORKER_LOST, "message": "워커가 비정상 종료되어 잡이 중단되었습니다."}),
                     row["job_id"]),
                )
                lost[KIND_JOB].append(row["job_id"])
        return lost

    # ------------------------------------------------------------------ 종결
    def finish_job(
        self,
        job_id: str,
        status: str,
        *,
        error: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        warnings: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        if status not in TERMINAL:
            raise ValueError(f"not a terminal status: {status}")
        now = self.now()
        with self.db.tx() as c:
            sets = ["status=?", "finished_at=?", "error_json=?"]
            args: list[Any] = [status, now, None if error is None else _dumps(error)]
            if result is not None:
                sets += ["result_json=?", "result_schema_version=?"]
                args += [_dumps(result), str(result.get("schema_version", ""))]
            if warnings is not None:
                sets.append("warnings_json=?")
                args.append(_dumps(list(warnings)))
            args.append(job_id)
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)
            c.execute(
                "UPDATE queue SET state='done', finished_at=? WHERE kind=? AND ref_id=? AND state='running'",
                (now, KIND_JOB, job_id),
            )

    def get_result(self, job_id: str) -> dict[str, Any] | None:
        with self.db.connect() as c:
            row = c.execute("SELECT result_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return None if row is None else _loads(row["result_json"])

    # ------------------------------------------------------------------ 진행·이력 (EngineServices)
    def set_progress(self, job_id: str, stage: str, fields: Mapping[str, Any]) -> None:
        """`progress(stage, **fields)`. `iter`/`best_J` 필드는 최상위 값을 덮어쓴다. phase = stage."""
        with self.db.tx() as c:
            row = c.execute("SELECT progress_json, iter, best_j FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            prog = _loads(row["progress_json"]) or {}
            extra = dict(fields)
            it = extra.pop("iter", None)
            best = extra.pop("best_J", None)
            prog.update(extra)
            prog["stage"] = stage
            c.execute(
                "UPDATE jobs SET progress_json=?, phase=?, iter=?, best_j=? WHERE job_id=?",
                (_dumps(prog), stage, row["iter"] if it is None else int(it), row["best_j"] if best is None else best, job_id),
            )

    def record_eval(self, job_id: str, item: Mapping[str, Any]) -> None:
        """history 행 추가(api.md §4.11 HistoryItem). iter/best_J 를 갱신한다: 예산 단계(elastic·plastic) 평가 수, ok 평가의 최소 J."""
        if "eval_index" not in item or "stage" not in item:
            raise ValueError("history item needs eval_index and stage")
        with self.db.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO history (job_id, eval_index, stage, item_json) VALUES (?,?,?,?)",
                (job_id, int(item["eval_index"]), str(item["stage"]), _dumps(dict(item))),
            )
            rows = c.execute("SELECT stage, item_json FROM history WHERE job_id=?", (job_id,)).fetchall()
            n = 0
            best: float | None = None
            for r in rows:
                if r["stage"] not in BUDGET_STAGES:
                    continue
                n += 1
                it = _loads(r["item_json"])
                j = it.get("J")
                if j is not None and it.get("status", "ok") == "ok" and (best is None or j < best):
                    best = float(j)
            c.execute("UPDATE jobs SET iter=?, best_j=? WHERE job_id=?", (n, best, job_id))

    def history_page(
        self, job_id: str, *, stage: str | None, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = "job_id=?", [job_id]
        if stage:
            where += " AND stage=?"
            args.append(stage)
        with self.db.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM history WHERE {where}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT item_json FROM history WHERE {where} ORDER BY eval_index LIMIT ? OFFSET ?", [*args, limit, offset]
            ).fetchall()
        return [_loads(r["item_json"]) for r in rows], total

    def history_all(self, job_id: str) -> list[dict[str, Any]]:
        out, _ = self.history_page(job_id, stage=None, limit=1_000_000, offset=0)
        return out

    # ------------------------------------------------------------------ 입력 파일 (E2)
    def add_file(self, rec: Mapping[str, Any]) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO files (file_id, purpose, filename, bytes, sha256, rel_path, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rec["file_id"], rec["purpose"], rec["filename"], rec["bytes"], rec["sha256"], rec["rel_path"],
                 rec["created_at"], rec.get("created_by")),
            )

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        return None if row is None else dict(row)
