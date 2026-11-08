"""레시피 저장소: 등록(번들 -> 정적 검사 -> 불변 저장 -> 검증 큐), 상태 전이, 조회, 폐기 (recipe.md §2·§5, api.md §8).

* 저장 위치: `recipes.store_dir/<recipe_id>/<version>/<verification_id>/` (읽기 전용, 덮어쓰기 금지).
* 상태: verifying -> active | rejected, active -> retired. 같은 `recipe_id@version` 에 업로드 시도가 여럿일 수 있다(rejected 이력).
* 대표 시도 우선순위: active > retired > verifying > rejected(최신).
* 검증은 일반 잡과 같은 큐에서 순차 실행한다 (`JobStore.enqueue`).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core import errors as E
from ..core.config import Config
from ..core.errors import MatObserverError
from ..core.ids import new_ulid, new_verification_id
from ..core.registry import Registry
from ..core.types import ResolvedRecipe
from ..jobs.clock import Clock, iso, utcnow
from ..jobs.db import Database
from ..jobs.store import KIND_VERIFICATION, JobStore
from .bundle import BundleError, content_hash, extract_bundle
from .static_check import StaticIssue, check_folder

VERIFYING, ACTIVE, RETIRED, REJECTED = "verifying", "active", "retired", "rejected"
LIVE_STATUSES = (VERIFYING, ACTIVE, RETIRED)  # 충돌·멱등 판정 대상 (rejected 제외)

_PRIORITY_SQL = "CASE status WHEN 'active' THEN 0 WHEN 'retired' THEN 1 WHEN 'verifying' THEN 2 ELSE 3 END"


class StaticCheckFailed(MatObserverError):
    """정적 검사 실패 (400 VALIDATION_FAILED + RECIPE_* 사유)."""

    code = E.VALIDATION_FAILED

    def __init__(self, issues: Sequence[StaticIssue]) -> None:
        super().__init__(f"recipe static check failed: {len(issues)} issue(s)")
        self.issues = list(issues)


class RecipeVersionConflict(MatObserverError):
    code = E.RECIPE_VERSION_CONFLICT


class RecipeNotFound(MatObserverError):
    code = E.NOT_FOUND


class RecipeNotRetirable(MatObserverError):
    code = E.RECIPE_NOT_RETIRABLE


@dataclass
class RegisterOutcome:
    created: bool  # True 면 새로 등록(202), False 면 같은 해시 재등록(200)
    row: dict[str, Any]
    warnings: list[StaticIssue] = field(default_factory=list)


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["standards"] = json.loads(d.pop("standards_json"))
    d["manifest"] = json.loads(d.pop("manifest_json"))
    d["files"] = json.loads(d.pop("files_json"))
    d["static_warnings"] = json.loads(d.pop("static_warnings_json"))
    vj = d.pop("verification_json")
    d["verification"] = None if vj is None else json.loads(vj)
    d["recipe_ref"] = f"{d['recipe_id']}@{d['version']}"
    d["strict"] = bool(d["strict"])
    d["compliance_available"] = bool(d["compliance_available"])
    return d


def _chmod_readonly(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                os.chmod(Path(dirpath) / fn, 0o444)
            except OSError:
                pass
    for dirpath, _dirnames, _fn in os.walk(root, topdown=False):
        try:
            os.chmod(dirpath, 0o555)
        except OSError:
            pass


def unlock_tree(root: Path) -> None:
    """읽기 전용으로 잠근 폴더를 지울 수 있게 푼다 (스테이징 정리·시험 정리용)."""
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            os.chmod(dirpath, 0o755)
        except OSError:
            pass
        for fn in filenames:
            try:
                os.chmod(Path(dirpath) / fn, 0o644)
            except OSError:
                pass


class RecipeRepository:
    def __init__(self, db: Database, store: JobStore, config: Config, clock: Clock = utcnow) -> None:
        self.db = db
        self.store = store
        self.config = config
        self.clock = clock

    # ------------------------------------------------------------------ 경로
    @property
    def store_root(self) -> Path:
        return self.config.path("recipes.store_dir")

    def attempt_dir(self, row: Mapping[str, Any]) -> Path:
        return (self.store_root / row["store_rel"]).resolve()

    def resolved(self, row: Mapping[str, Any]) -> ResolvedRecipe:
        return ResolvedRecipe(recipe_ref=row["recipe_ref"], content_hash=row["content_hash"],
                              store_dir=self.attempt_dir(row), manifest=row["manifest"])

    # ------------------------------------------------------------------ 등록 (A1)
    def register_bundle(
        self, data: bytes, *, strict: bool, created_by: str | None, registry: Registry | None
    ) -> RegisterOutcome:
        root = self.store_root
        staging_root = root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / new_ulid()
        staging.mkdir()
        moved = False
        try:
            try:
                entries = extract_bundle(
                    data, staging,
                    max_unpacked_bytes=int(self.config.get("recipes.max_unpacked_bytes")),
                    max_files=int(self.config.get("recipes.max_files")),
                )
            except BundleError as e:
                raise StaticCheckFailed([StaticIssue(e.code, e.message, field=e.field)]) from e
            res = check_folder(staging, registry, entries=entries)
            manifest = res.manifest
            schema_bad = manifest is None or any(i.code in (E.RECIPE_MANIFEST_INVALID, E.RECIPE_BUNDLE_INVALID) for i in res.errors)
            if schema_bad:
                raise StaticCheckFailed(res.errors)
            chash = content_hash(entries)
            recipe_id, version = manifest["recipe_id"], int(manifest["version"])
            existing = self.attempts(recipe_id, version)
            live = [r for r in existing if r["status"] in LIVE_STATUSES]
            same = [r for r in live if r["content_hash"] == chash]
            if same:
                return RegisterOutcome(created=False, row=sorted(same, key=_prio)[0])
            if live:
                raise RecipeVersionConflict(
                    f"{recipe_id}@{version} already registered with a different content_hash ({live[0]['status']})"
                )
            blocking = res.errors + (res.warnings if strict else [])
            if blocking:
                raise StaticCheckFailed(blocking)
            verification_id = new_verification_id()
            store_rel = f"{recipe_id}/{version}/{verification_id}"
            dest = root / store_rel
            now = iso(self.clock())
            with self.db.tx() as c:
                # 직렬화된 트랜잭션 안에서 한 번 더 충돌 확인 (동시 업로드 경쟁)
                rows = c.execute(
                    "SELECT status, content_hash FROM recipes WHERE recipe_id=? AND version=?", (recipe_id, version)
                ).fetchall()
                if any(r["status"] in LIVE_STATUSES for r in rows):
                    raise RecipeVersionConflict(f"{recipe_id}@{version} already registered")
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, dest)
                moved = True
                _chmod_readonly(dest)
                comp = manifest.get("compliance") or {}
                try:
                    self._insert_row(c, recipe_id, version, verification_id, chash, manifest, entries, store_rel, strict, res, now,
                                     created_by, comp)
                except BaseException:
                    unlock_tree(dest)
                    shutil.rmtree(dest, ignore_errors=True)
                    raise
            return RegisterOutcome(created=True, row=self.get_by_verification(verification_id), warnings=res.warnings)  # type: ignore[arg-type]
        finally:
            if not moved and staging.exists():
                unlock_tree(staging)
                shutil.rmtree(staging, ignore_errors=True)

    def _insert_row(self, c: sqlite3.Connection, recipe_id: str, version: int, verification_id: str, chash: str,
                    manifest: Mapping[str, Any], entries: Sequence[Any], store_rel: str, strict: bool, res: Any, now: str,
                    created_by: str | None, comp: Mapping[str, Any]) -> None:
        c.execute(
            "INSERT INTO recipes (recipe_id, version, verification_id, content_hash, status, kind, test_type, title,"
            " standards_json, compliance_available, manifest_json, files_json, store_rel, strict, static_warnings_json,"
            " warnings_count, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (recipe_id, version, verification_id, chash, VERIFYING, manifest["kind"], manifest["test_type"],
             manifest["title"], json.dumps(manifest.get("standards") or []),
             1 if comp.get("value_m_per_n") is not None else 0, json.dumps(manifest, ensure_ascii=False, default=str),
             json.dumps([e.to_json() for e in entries]), store_rel, 1 if strict else 0,
             json.dumps([w.to_detail() for w in res.warnings], ensure_ascii=False), len(res.warnings), now, created_by),
        )
        self.store.enqueue(KIND_VERIFICATION, verification_id, conn=c)

    # ------------------------------------------------------------------ 조회
    def get_by_verification(self, verification_id: str) -> dict[str, Any] | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM recipes WHERE verification_id=?", (verification_id,)).fetchone()
        return None if r is None else _row(r)

    def attempts(self, recipe_id: str, version: int) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM recipes WHERE recipe_id=? AND version=? ORDER BY seq", (recipe_id, version)).fetchall()
        return [_row(r) for r in rows]

    def get_best(self, recipe_id: str, version: int) -> dict[str, Any] | None:
        with self.db.connect() as c:
            r = c.execute(
                f"SELECT * FROM recipes WHERE recipe_id=? AND version=? ORDER BY {_PRIORITY_SQL}, seq DESC LIMIT 1",
                (recipe_id, version),
            ).fetchone()
        return None if r is None else _row(r)

    def _representatives(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            rows = c.execute(f"SELECT * FROM recipes ORDER BY recipe_id, version, {_PRIORITY_SQL}, seq DESC").fetchall()
        seen: set[tuple[str, int]] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            k = (r["recipe_id"], r["version"])
            if k in seen:
                continue
            seen.add(k)
            out.append(_row(r))
        return out

    def list_admin(
        self, *, statuses: Sequence[str] | None, test_type: str | None, recipe_id: str | None, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        items = self._representatives()
        if statuses:
            items = [r for r in items if r["status"] in statuses]
        if test_type:
            items = [r for r in items if r["test_type"] == test_type]
        if recipe_id:
            items = [r for r in items if r["recipe_id"] == recipe_id]
        items.sort(key=lambda r: r["seq"], reverse=True)
        return items[offset:offset + limit], len(items)

    def list_active(self, *, test_type: str | None, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        items = [r for r in self._representatives() if r["status"] == ACTIVE]
        if test_type:
            items = [r for r in items if r["test_type"] == test_type]
        items.sort(key=lambda r: (r["recipe_id"], r["version"]))
        return items[offset:offset + limit], len(items)

    # ------------------------------------------------------------------ 폐기 (A4)
    def retire(self, recipe_id: str, version: int, reason: str) -> dict[str, Any]:
        with self.db.tx() as c:
            r = c.execute(
                f"SELECT * FROM recipes WHERE recipe_id=? AND version=? ORDER BY {_PRIORITY_SQL}, seq DESC LIMIT 1",
                (recipe_id, version),
            ).fetchone()
            if r is None:
                raise RecipeNotFound(f"{recipe_id}@{version}")
            if r["status"] == RETIRED:
                return _row(r)
            if r["status"] != ACTIVE:
                raise RecipeNotRetirable(f"{recipe_id}@{version} is {r['status']}; only active recipes can be retired")
            c.execute(
                "UPDATE recipes SET status=?, retired_at=?, retire_reason=? WHERE seq=?",
                (RETIRED, iso(self.clock()), reason, r["seq"]),
            )
        return self.get_best(recipe_id, version)  # type: ignore[return-value]

    # ------------------------------------------------------------------ 검증 결과 (워커)
    def finish_verification(self, verification_id: str, report: Mapping[str, Any]) -> dict[str, Any]:
        """검증 보고서(recipe.md §7.3)를 저장하고 상태를 정한다: passed -> active, passed_with_warnings -> active(strict면 rejected),
        failed·error -> rejected."""
        status = str(report.get("status", "error"))
        with self.db.tx() as c:
            r = c.execute("SELECT * FROM recipes WHERE verification_id=?", (verification_id,)).fetchone()
            if r is None:
                raise RecipeNotFound(verification_id)
            if r["status"] != VERIFYING:
                return _row(r)
            if status == "passed" or (status == "passed_with_warnings" and not r["strict"]):
                new = ACTIVE
            else:
                new = REJECTED
            warn = sum(1 for chk in report.get("checks", []) if chk.get("result") == "warn")
            c.execute(
                "UPDATE recipes SET status=?, verification_json=?, warnings_count=? WHERE seq=?",
                (new, json.dumps(report, ensure_ascii=False, default=str), len(json.loads(r["static_warnings_json"])) + warn, r["seq"]),
            )
        return self.get_by_verification(verification_id)  # type: ignore[return-value]


def _prio(r: Mapping[str, Any]) -> int:
    return {ACTIVE: 0, RETIRED: 1, VERIFYING: 2}.get(r["status"], 3)
