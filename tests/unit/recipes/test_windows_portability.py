"""Windows 이식성 회귀 (Linux 에서 재현 가능한 형태): 백슬래시 멤버 이름, CRLF 체크아웃에서도 맞는 시험 도구, 해시의 줄바꿈 독립 규칙."""

from __future__ import annotations

import os
import shutil
import types
import zipfile

import pytest

import be_support
from matobserver.recipes.bundle import BundleError, FileEntry, content_hash, extract_bundle, raw_member_names, scan_tree

from be_support import RECIPE_DIR, recipe_zip


class _WindowsOs(types.SimpleNamespace):
    """`zipfile` 이 보는 `os` 를 Windows 처럼 (sep='\\\\') 보이게 한다 -> ZipInfo 가 `\\\\` 를 `/` 로 바꾼다."""

    def __getattr__(self, name):
        return getattr(os, name)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zi = zipfile.ZipInfo("placeholder")
            zi.filename = zi.orig_filename = name  # 생성 뒤 대입: Windows 의 ZipInfo(name) 은 `\\` 를 `/` 로 바꾸기 때문
            zf.writestr(zi, data)
    return buf.getvalue()


def test_backslash_member_rejected_even_when_zipfile_normalizes_separators(tmp_path, monkeypatch):
    data = _zip_bytes({"manifest.yaml": b"x: 1", "a\\b.k": b"boom"})   # os.sep 가 '/' 인 상태에서 만든 원본 이름
    monkeypatch.setattr(zipfile, "os", _WindowsOs(sep="\\", path=os.path))
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
        assert [i.filename for i in zf.infolist()] == ["manifest.yaml", "a/b.k"]  # Windows 의 zipfile 은 이미 '/' 로 바꿨다
        assert b"a\\b.k" in raw_member_names(zf)                                   # 원본은 중앙 디렉터리에서 읽는다
    with pytest.raises(BundleError) as ei:
        extract_bundle(data, tmp_path / "out", max_unpacked_bytes=10_000, max_files=10)
    assert ei.value.code == "RECIPE_PATH_UNSAFE" and not (tmp_path / "out" / "a" / "b.k").exists()


def test_normal_bundle_still_extracts(tmp_path):
    data = _zip_bytes({"manifest.yaml": b"x: 1", "deck/a.k": b"*KEYWORD\n"})
    entries = extract_bundle(data, tmp_path / "out", max_unpacked_bytes=10_000, max_files=10)
    assert [e.path for e in entries] == ["deck/a.k", "manifest.yaml"]


def test_content_hash_depends_on_posix_paths_and_bytes_only():
    a = [FileEntry("deck/a.k", 3, "aa"), FileEntry("manifest.yaml", 1, "bb")]
    assert content_hash(a) == content_hash(list(reversed(a)))                 # 입력 순서 무관 (경로 사전순)
    b = [FileEntry("deck\\a.k", 3, "aa"), FileEntry("manifest.yaml", 1, "bb")]
    assert content_hash(a) != content_hash(b)                                  # 역슬래시 표기는 다른 경로 (허용도 안 됨)
    assert all("\\" not in e.path for e in scan_tree(RECIPE_DIR)[0])           # 스캔 결과는 항상 POSIX 상대 경로


def test_recipe_zip_edits_work_on_crlf_checkout(tmp_path, monkeypatch):
    """CRLF 로 체크아웃된 픽스처에서도 `version: 1\\n` 같은 줄 치환이 적용된다 (CI 에서 version=2 가 v1 로 남아 200≠202 가 되던 원인)."""
    crlf = tmp_path / "recipe"
    shutil.copytree(RECIPE_DIR, crlf)
    for p in crlf.rglob("*"):
        if p.is_file() and p.suffix in {".yaml", ".k"}:
            p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setattr(be_support, "RECIPE_DIR", crlf)
    with zipfile.ZipFile(__import__("io").BytesIO(recipe_zip(version=2))) as zf:
        assert b"version: 2" in zf.read("manifest.yaml")
    with zipfile.ZipFile(__import__("io").BytesIO(recipe_zip(edit_manifest=lambda t: t.replace("fixture: 3-point", "fixture X: 3-point")))) as zf:
        assert b"fixture X: 3-point" in zf.read("manifest.yaml")
