"""레시피 번들(zip) 안전 해제, 폴더 스캔, content_hash (recipe.md §2·§5).

* 경로 안전: 절대 경로·`..`·역슬래시·심볼릭 링크 금지 (`RECIPE_PATH_UNSAFE`).
* 상한: 해제 총크기 `recipes.max_unpacked_bytes`, 파일 수 `recipes.max_files` — 해제 중 초과하면 중단 (`RECIPE_BUNDLE_INVALID`).
* `content_hash = "sha256:" + hex(sha256(L))`, L = 상대 경로(POSIX) 사전순 `"{path}\\0{sha256(bytes)}\\n"` 이어 붙임.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ..core import errors as E


class BundleError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@dataclass(frozen=True)
class FileEntry:
    path: str  # 상대 POSIX 경로
    bytes: int
    sha256: str  # hex

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def check_rel_path(name: str) -> str | None:
    """안전하지 않으면 사유 문자열, 안전하면 None."""
    if not name or "\x00" in name:
        return "빈 경로 또는 NUL 문자"
    if "\\" in name:
        return "역슬래시 경로"
    if name.startswith("/") or _DRIVE_RE.match(name):
        return "절대 경로"
    parts = PurePosixPath(name).parts
    if any(p == ".." for p in parts):
        return "상위 경로(..)"
    return None


def raw_member_names(zf: zipfile.ZipFile) -> list[bytes]:
    """중앙 디렉터리에 적힌 원본 멤버 이름(바이트). Windows 의 `zipfile` 은 ZipInfo 생성 때 `\\` 를 `/` 로 바꿔 버려
    백슬래시 검사가 OS 마다 달라지므로, 검사는 이 원본으로 한다."""
    fp = zf.fp
    out: list[bytes] = []
    if fp is None:
        return out
    pos = fp.tell()
    try:
        fp.seek(zf.start_dir)
        for _ in range(len(zf.infolist())):
            hdr = fp.read(46)
            if len(hdr) < 46 or hdr[:4] != b"PK\x01\x02":
                break
            n, m, k = struct.unpack("<HHH", hdr[28:34])
            out.append(fp.read(n))
            fp.seek(m + k, 1)
    finally:
        fp.seek(pos)
    return out


def content_hash(entries: list[FileEntry]) -> str:
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x.path):
        h.update(f"{e.path}\0{e.sha256}\n".encode("utf-8"))
    return "sha256:" + h.hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def scan_tree(root: Path) -> tuple[list[FileEntry], list[tuple[str, str]]]:
    """폴더의 일반 파일 목록과 안전 위반 `[(경로, 사유)]`. 심볼릭 링크는 따라가지 않고 위반으로 기록한다."""
    entries: list[FileEntry] = []
    bad: list[tuple[str, str]] = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in list(dirnames):
            full = Path(dirpath) / d
            if full.is_symlink():
                bad.append((full.relative_to(root).as_posix(), "심볼릭 링크"))
                dirnames.remove(d)
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                bad.append((rel, "심볼릭 링크"))
                continue
            if not full.is_file():
                bad.append((rel, "일반 파일이 아님"))
                continue
            digest, size = hash_file(full)
            entries.append(FileEntry(rel, size, digest))
    entries.sort(key=lambda e: e.path)
    return entries, bad


def extract_bundle(
    data: bytes | BinaryIO, dest: Path, *, max_unpacked_bytes: int, max_files: int
) -> list[FileEntry]:
    """zip -> `dest`(비어 있는 새 폴더). 위반은 BundleError. 반환: 파일 목록(경로 사전순)."""
    fobj: BinaryIO = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    try:
        zf = zipfile.ZipFile(fobj)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise BundleError(E.RECIPE_BUNDLE_INVALID, "번들이 올바른 zip 파일이 아닙니다.") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
        if len(infos) > max_files:
            raise BundleError(E.RECIPE_BUNDLE_INVALID, f"파일 수 상한 초과 (최대 {max_files}개)")
        if sum(i.file_size for i in infos) > max_unpacked_bytes:
            raise BundleError(E.RECIPE_BUNDLE_INVALID, f"해제 총크기 상한 초과 (최대 {max_unpacked_bytes} bytes)")
        for raw in raw_member_names(zf):
            if b"\\" in raw:
                raise BundleError(E.RECIPE_PATH_UNSAFE, "안전하지 않은 경로: 역슬래시 경로", field=raw.decode("utf-8", "replace"))
        seen: set[str] = set()
        for i in infos:
            name = i.filename
            why = check_rel_path(name)
            if why:
                raise BundleError(E.RECIPE_PATH_UNSAFE, f"안전하지 않은 경로: {why}", field=name)
            mode = (i.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise BundleError(E.RECIPE_PATH_UNSAFE, "심볼릭 링크는 허용되지 않습니다.", field=name)
            if i.flag_bits & 0x1:
                raise BundleError(E.RECIPE_BUNDLE_INVALID, "암호화된 zip 은 지원하지 않습니다.", field=name)
            if name in seen:
                raise BundleError(E.RECIPE_BUNDLE_INVALID, "같은 경로가 두 번 들어 있습니다.", field=name)
            seen.add(name)
        if "manifest.yaml" not in seen:
            raise BundleError(E.RECIPE_BUNDLE_INVALID, "번들 최상위에 manifest.yaml 이 없습니다.", field="manifest.yaml")
        dest = Path(dest)
        dest_r = dest.resolve()
        total = 0
        for i in infos:
            target = (dest / i.filename).resolve()
            if not target.is_relative_to(dest_r):  # 방어: 위에서 걸렀지만 한 번 더
                raise BundleError(E.RECIPE_PATH_UNSAFE, "안전하지 않은 경로", field=i.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(i) as src, open(target, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        total += len(chunk)
                        if total > max_unpacked_bytes:
                            raise BundleError(E.RECIPE_BUNDLE_INVALID, "해제 총크기 상한 초과")
                        out.write(chunk)
            except (zipfile.BadZipFile, RuntimeError, zlib.error) as e:
                raise BundleError(E.RECIPE_BUNDLE_INVALID, "zip 항목이 손상되었습니다.", field=i.filename) from e
    entries, bad = scan_tree(dest)
    if bad:  # 이론상 없음 (위에서 걸렀다)
        raise BundleError(E.RECIPE_PATH_UNSAFE, f"안전하지 않은 항목: {bad[0][1]}", field=bad[0][0])
    return entries
