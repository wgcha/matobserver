"""입력 파일 저장소 (`POST /v1/files`, api.md §4.1). 파일은 `paths.data_dir/files/{file_id}.{ext}` 에 두고 외부에는 `file_id` 만 쓴다."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, BinaryIO, Final

from ..core import errors as E
from ..core.config import Config
from ..core.errors import MatObserverError
from ..core.ids import new_file_id
from .clock import Clock, iso, utcnow
from .store import JobStore

PURPOSES: Final = ("geometry_stp",)  # 확장 가능 enum (api.md §4.1)
STEP_EXTENSIONS: Final = (".stp", ".step")
STEP_MAGIC: Final = "ISO-10303-21;"


class FileTooLarge(MatObserverError):
    code = E.FILE_TOO_LARGE


class FileTypeUnsupported(MatObserverError):
    code = E.FILE_TYPE_UNSUPPORTED


class FileService:
    def __init__(self, store: JobStore, config: Config, clock: Clock = utcnow) -> None:
        self.store = store
        self.config = config
        self.clock = clock

    @property
    def root(self) -> Path:
        return self.config.path("paths.data_dir") / "files"

    def get(self, file_id: str) -> dict[str, Any] | None:
        return self.store.get_file(file_id)

    def path(self, rec: dict[str, Any]) -> Path:
        """저장된 실제 경로 (서버 내부에서만 쓴다). 루트 밖이면 FileNotFoundError."""
        root = self.root.resolve()
        p = (root / rec["rel_path"]).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise FileNotFoundError(rec["file_id"])
        return p

    def save_upload(self, stream: BinaryIO, filename: str, purpose: str, created_by: str | None) -> dict[str, Any]:
        """검사(확장자·첫 줄·크기) 후 저장. 실패는 FileTypeUnsupported(400)/FileTooLarge(413)."""
        if purpose not in PURPOSES:
            raise FileTypeUnsupported(f"purpose 는 {list(PURPOSES)} 중 하나여야 합니다: {purpose!r}")
        base = os.path.basename(filename.replace("\\", "/")) or "upload"
        ext = os.path.splitext(base)[1].lower()
        if ext not in STEP_EXTENSIONS:
            raise FileTypeUnsupported(f"허용 확장자: {', '.join(STEP_EXTENSIONS)}")
        max_bytes = int(self.config.get("files.max_bytes"))
        file_id = new_file_id()
        self.root.mkdir(parents=True, exist_ok=True)
        rel = f"{file_id}{ext}"
        dest = self.root / rel
        h = hashlib.sha256()
        n = 0
        head = b""
        try:
            with open(dest, "wb") as out:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    n += len(chunk)
                    if n > max_bytes:
                        raise FileTooLarge(f"파일이 너무 큽니다 (최대 {max_bytes} bytes)")
                    if len(head) < 256:
                        head += chunk[: 256 - len(head)]
                    h.update(chunk)
                    out.write(chunk)
            first = head.decode("utf-8", errors="ignore").lstrip("﻿ \t\r\n").split("\n", 1)[0].strip()
            if not first.startswith(STEP_MAGIC):
                raise FileTypeUnsupported("STEP 파일이 아닙니다 (첫 줄이 ISO-10303-21; 이어야 함).")
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        rec = {"file_id": file_id, "purpose": purpose, "filename": base, "bytes": n, "sha256": h.hexdigest(), "rel_path": rel,
               "created_at": iso(self.clock()), "created_by": created_by}
        self.store.add_file(rec)
        return rec
