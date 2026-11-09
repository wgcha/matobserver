"""번들 해제 안전, content_hash 규칙 (recipe.md §2·§5)."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile

import pytest

from matobserver.recipes.bundle import BundleError, check_rel_path, content_hash, extract_bundle, scan_tree

from be_support import RECIPE_DIR, folder_zip


def make_zip(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    """멤버 이름은 입력 그대로 담긴다. Windows 의 `ZipInfo(name)` 은 `\\` 를 `/` 로 바꾸므로, 생성 뒤에 filename 을 직접 대입해
    OS 무관하게 원본 이름(`a\\b.k`)이 지역·중앙 헤더에 들어가게 한다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, d in files.items():
            zi = zipfile.ZipInfo("placeholder")
            zi.filename = zi.orig_filename = n
            zf.writestr(zi, d)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, "/etc/passwd")
    return buf.getvalue()


def extract(data, tmp_path, **kw):
    dest = tmp_path / "out"
    dest.mkdir()
    return extract_bundle(data, dest, max_unpacked_bytes=kw.get("max_bytes", 10**9), max_files=kw.get("max_files", 100)), dest


def independent_hash(root):
    """recipe.md §5 의 공식을 구현과 독립적으로 다시 계산한다."""
    parts = []
    for dirpath, _d, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            parts.append((rel, hashlib.sha256(open(p, "rb").read()).hexdigest()))
    blob = b"".join(f"{r}\0{h}\n".encode() for r, h in sorted(parts))
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def test_content_hash_matches_formula():
    entries, _ = scan_tree(RECIPE_DIR)
    assert content_hash(entries) == independent_hash(RECIPE_DIR)


def folder_files():
    return {p.relative_to(RECIPE_DIR).as_posix(): p.read_bytes() for p in RECIPE_DIR.rglob("*") if p.is_file()}


def unpack_hash(files, tmp_path, name):
    out = tmp_path / name
    out.mkdir()
    return content_hash(extract_bundle(make_zip(files), out, max_unpacked_bytes=10**9, max_files=100))


def test_hash_independent_of_zip_order_compression_and_timestamps(tmp_path):
    files = folder_files()
    a = unpack_hash(dict(sorted(files.items())), tmp_path, "a")
    b = unpack_hash(dict(sorted(files.items(), reverse=True)), tmp_path, "b")
    assert a == b == independent_hash(RECIPE_DIR)


def test_hash_changes_with_any_byte_or_path(tmp_path):
    files = folder_files()
    h0 = unpack_hash(files, tmp_path, "base")
    appended = {**files, "deck/contact.k": files["deck/contact.k"] + b" "}
    renamed = dict(files)
    renamed["deck/renamed.k"] = renamed.pop("deck/contact.k")
    assert len({h0, unpack_hash(appended, tmp_path, "v1"), unpack_hash(renamed, tmp_path, "v2")}) == 3


def test_readme_is_part_of_the_hash(tmp_path):
    files = folder_files()
    assert unpack_hash(files, tmp_path, "1") != unpack_hash({**files, "README.md": "설명".encode()}, tmp_path, "2")


def test_crlf_bytes_are_hashed_as_is(tmp_path):
    assert unpack_hash({"manifest.yaml": b"a: 1\n"}, tmp_path, "1") != unpack_hash({"manifest.yaml": b"a: 1\r\n"}, tmp_path, "2")  # 원본 바이트 기준


@pytest.mark.parametrize("name,ok", [
    ("deck/main.k", True), ("a.txt", True), ("a/b/c.k", True),
    ("../x", False), ("a/../../x", False), ("/etc/passwd", False), ("C:/x", False), ("a\\b", False), ("", False),
])
def test_check_rel_path(name, ok):
    assert (check_rel_path(name) is None) is ok


@pytest.mark.parametrize("evil", ["../evil.k", "deck/../../evil.k", "/abs.k", "a\\b.k"])
def test_unsafe_zip_member_paths_rejected(tmp_path, evil):
    data = make_zip({"manifest.yaml": b"x: 1", evil: b"boom"})
    with pytest.raises(BundleError) as ei:
        extract(data, tmp_path)
    assert ei.value.code == "RECIPE_PATH_UNSAFE"
    assert not (tmp_path / "evil.k").exists()


def test_symlink_member_rejected(tmp_path):
    with pytest.raises(BundleError) as ei:
        extract(make_zip({"manifest.yaml": b"x: 1"}, symlink="deck/link.k"), tmp_path)
    assert ei.value.code == "RECIPE_PATH_UNSAFE"


def test_not_a_zip_and_corrupt_zip(tmp_path):
    for i, data in enumerate((b"not a zip", b"", b"PK\x03\x04garbage")):
        out = tmp_path / f"o{i}"
        out.mkdir()
        with pytest.raises(BundleError) as ei:
            extract_bundle(data, out, max_unpacked_bytes=10**6, max_files=10)
        assert ei.value.code == "RECIPE_BUNDLE_INVALID"


def test_manifest_must_be_at_top_level(tmp_path):
    data = make_zip({"wrapper/manifest.yaml": b"x: 1"})
    with pytest.raises(BundleError) as ei:
        extract(data, tmp_path)
    assert ei.value.code == "RECIPE_BUNDLE_INVALID" and ei.value.field == "manifest.yaml"


def test_limits_on_files_and_unpacked_size(tmp_path):
    many = make_zip({"manifest.yaml": b"x", **{f"f{i}.k": b"1" for i in range(5)}})
    with pytest.raises(BundleError, match="파일 수"):
        extract(many, tmp_path, max_files=3)
    big = make_zip({"manifest.yaml": b"x", "big.k": b"0" * 5000})
    (tmp_path / "o2").mkdir()
    with pytest.raises(BundleError, match="총크기"):
        extract_bundle(big, tmp_path / "o2", max_unpacked_bytes=1000, max_files=10)


def test_duplicate_member_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.yaml", "a")
        with pytest.warns(UserWarning):
            zf.writestr("manifest.yaml", "b")
    with pytest.raises(BundleError):
        extract(buf.getvalue(), tmp_path)


def test_scan_tree_flags_symlinks(tmp_path):
    (tmp_path / "a.k").write_text("x")
    try:
        os.symlink(tmp_path / "a.k", tmp_path / "link.k")
    except OSError:
        pytest.skip("symlink 권한 없음 (Windows)")
    entries, bad = scan_tree(tmp_path)
    assert [e.path for e in entries] == ["a.k"] and bad == [("link.k", "심볼릭 링크")]


def test_helper_zip_roundtrip_equals_folder_hash(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    entries = extract_bundle(folder_zip(RECIPE_DIR), out, max_unpacked_bytes=10**9, max_files=100)
    assert content_hash(entries) == independent_hash(RECIPE_DIR)
