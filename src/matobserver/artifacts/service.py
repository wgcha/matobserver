"""artifact 등록·조회·다운로드 경로 해석 (api.md §6, module-boundaries.md §4.5).

* 저장: 소유자별 작업 폴더에만. 잡 `paths.work_dir/{job_id}/artifacts/`, 레시피 검증 `paths.work_dir/_recipes/{verification_id}/artifacts/`.
* 노출: `GET /v1/artifacts/{artifact_id}` 한 곳. 응답에는 경로를 넣지 않는다 (ArtifactRef 에 파일 이름·크기·해시만).
* 안전: 다운로드 시 실제 경로(심볼릭 링크 해석 후)가 소유자 artifact 루트 안인지 검사한다. 밖이면 NotFound.
* 불변: 등록된 파일 내용은 바뀌지 않는다 (읽기 전용으로 바꾼다). 재생성은 새 ID.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from ..core import errors as E
from ..core.config import Config
from ..core.errors import MatObserverError
from ..core.ids import new_artifact_id
from ..jobs.clock import Clock, iso, parse_iso, utcnow
from ..jobs.db import Database

OWNER_JOB: Final = "job"
OWNER_RECIPE_VERIFICATION: Final = "recipe_verification"
OWNER_KINDS: Final = frozenset({OWNER_JOB, OWNER_RECIPE_VERIFICATION})

#: result.md §6 kind -> media_type
KIND_MEDIA_TYPES: Final[dict[str, str]] = {
    "result_json": "application/json",
    "card_lsdyna": "text/plain",
    "card_blocks_json": "application/json",
    "curve_compare": "text/csv",
    "curve_raw": "text/csv",
    "history": "text/csv",
    "convergence_plot_data": "application/json",
    "deck_best": "application/zip",
    "mesh": "text/plain",
    "roundtrip": "application/zip",
    "explicit_check": "application/zip",
    "explicit_check_curves": "text/csv",
    "identifiability_json": "application/json",
    "verification_bundle": "application/zip",
}


class ArtifactNotFound(MatObserverError):
    code = E.NOT_FOUND


class ArtifactExpired(MatObserverError):
    code = E.ARTIFACT_EXPIRED


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    owner_kind: str
    owner_id: str
    rel_path: str
    filename: str
    sha256: str
    bytes: int
    media_type: str
    kind: str
    slot: str | None
    test_key: str | None
    created_at: str

    def ref(self) -> dict[str, Any]:
        """result.md §6 ArtifactRef (경로 없음)."""
        return {
            "artifact_id": self.artifact_id, "kind": self.kind, "media_type": self.media_type, "filename": self.filename,
            "bytes": self.bytes, "sha256": self.sha256, "slot": self.slot, "test_key": self.test_key,
            "created_at": self.created_at,
        }


def _row(r: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(**{k: r[k] for k in ArtifactRecord.__dataclass_fields__})


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ArtifactService:
    def __init__(self, db: Database, config: Config, clock: Clock = utcnow) -> None:
        self.db = db
        self.config = config
        self.clock = clock

    # ------------------------------------------------------------------ 경로
    def work_root(self, owner_kind: str, owner_id: str) -> Path:
        """소유자 작업 폴더 (module-boundaries.md §4.5)."""
        base = self.config.path("paths.work_dir")
        if owner_kind == OWNER_JOB:
            return base / owner_id
        if owner_kind == OWNER_RECIPE_VERIFICATION:
            return base / "_recipes" / owner_id
        raise ValueError(f"unknown owner_kind {owner_kind!r}")

    def artifact_root(self, owner_kind: str, owner_id: str) -> Path:
        return self.work_root(owner_kind, owner_id) / "artifacts"

    # ------------------------------------------------------------------ 등록
    def register(
        self,
        owner_kind: str,
        owner_id: str,
        path: str | os.PathLike[str],
        kind: str,
        *,
        slot: str | None = None,
        test_key: str | None = None,
        media_type: str | None = None,
        filename: str | None = None,
    ) -> str:
        """파일을 artifact 로 등록하고 `artifact_id`를 돌려준다.

        파일이 소유자 `artifacts/` 안에 있으면 그대로, 소유자 작업 폴더의 다른 곳에 있으면 `artifacts/`로 복사한다.
        소유자 작업 폴더 밖이면 ValueError (경로 탈출 방지).
        """
        if owner_kind not in OWNER_KINDS:
            raise ValueError(f"unknown owner_kind {owner_kind!r}")
        work = self.work_root(owner_kind, owner_id)
        root = self.artifact_root(owner_kind, owner_id)
        root.mkdir(parents=True, exist_ok=True)
        src = Path(path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"artifact source is not a file: {Path(path).name}")
        work_r, root_r = work.resolve(), root.resolve()
        if src.is_relative_to(root_r):
            dest = src
        elif src.is_relative_to(work_r):
            dest = self._unique_dest(root_r, src.name)
            shutil.copyfile(src, dest)
        else:
            raise ValueError("artifact source must be inside the owner's work folder")
        try:
            os.chmod(dest, 0o444)  # 불변: 읽기 전용 (best effort)
        except OSError:
            pass
        rel = dest.relative_to(root_r).as_posix()
        art_id = new_artifact_id()
        name = filename or dest.name
        mt = media_type or KIND_MEDIA_TYPES.get(kind) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO artifacts (artifact_id, owner_kind, owner_id, rel_path, filename, sha256, bytes, media_type,"
                " kind, slot, test_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (art_id, owner_kind, owner_id, rel, name, file_sha256(dest), dest.stat().st_size, mt, kind, slot, test_key,
                 iso(self.clock())),
            )
        return art_id

    @staticmethod
    def _unique_dest(root: Path, name: str) -> Path:
        dest = root / name
        i = 1
        while dest.exists():
            stem, suf = os.path.splitext(name)
            dest = root / f"{stem}-{i}{suf}"
            i += 1
        return dest

    # ------------------------------------------------------------------ 조회
    def get(self, artifact_id: str) -> ArtifactRecord | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return None if r is None else _row(r)

    def list_for_owner(self, owner_kind: str, owner_id: str) -> list[ArtifactRecord]:
        with self.db.connect() as c:
            rows = c.execute(
                "SELECT * FROM artifacts WHERE owner_kind=? AND owner_id=? ORDER BY rowid", (owner_kind, owner_id)
            ).fetchall()
        return [_row(r) for r in rows]

    def is_expired(self, rec: ArtifactRecord) -> bool:
        days = self.config.get("artifacts.retention_days")
        if days is None:
            return False
        return self.clock() - parse_iso(rec.created_at) > timedelta(days=float(days))

    def resolve_for_download(self, artifact_id: str) -> tuple[ArtifactRecord, Path]:
        """다운로드할 실제 경로. 없음/루트 밖 -> ArtifactNotFound (404), 보존 기간 만료 -> ArtifactExpired (410)."""
        rec = self.get(artifact_id)
        if rec is None:
            raise ArtifactNotFound(artifact_id)
        if self.is_expired(rec):
            raise ArtifactExpired(artifact_id)
        rel = Path(rec.rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ArtifactNotFound(artifact_id)
        root = self.artifact_root(rec.owner_kind, rec.owner_id).resolve()
        full = (root / rel).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            raise ArtifactNotFound(artifact_id)
        return rec, full
