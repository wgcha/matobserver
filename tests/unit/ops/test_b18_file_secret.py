"""B-18: `file:` 비밀값 참조 로드, 누락 시 doctor D01 FAIL (deploy.md §2.4)."""

from __future__ import annotations

import pytest

from matobserver.core.config import Config, load_config, resolve_secret
from matobserver.core.errors import ConfigError
from matobserver.ops import doctor as D

from ops_support import write_config


def test_file_reference_reads_first_line_trimmed(tmp_path):
    f = tmp_path / "t.token"
    f.write_text("  secret-value \r\nsecond line\n", encoding="utf-8")
    assert resolve_secret(f"file:{f}") == "secret-value"
    cfg = Config({"matnexus.token": f"file:{f}"})
    assert cfg.secret("matnexus.token") == "secret-value"
    assert cfg.get("matnexus.token") == f"file:{f}"  # 원본 문자열은 그대로 보인다


def test_file_reference_handles_bom_and_empty_file(tmp_path):
    f = tmp_path / "bom.token"
    f.write_bytes(b"\xef\xbb\xbftok\n")
    assert resolve_secret(f"file:{f}") == "tok"
    e = tmp_path / "empty.token"
    e.write_text("")
    assert resolve_secret(f"file:{e}") == ""


def test_missing_file_is_config_error_without_leaking_content(tmp_path):
    with pytest.raises(ConfigError) as ei:
        resolve_secret(f"file:{tmp_path / 'nope.token'}", key="matnexus.token")
    assert "matnexus.token" in str(ei.value)
    with pytest.raises(ConfigError):
        resolve_secret("file:")


def test_env_reference_still_works():
    assert resolve_secret("env:FOO", {"FOO": "bar"}) == "bar"
    assert resolve_secret("plain") == "plain"


def test_doctor_d01_fails_for_missing_secret_file(tmp_path):
    p = write_config(tmp_path, **{"matnexus.token": f"file:{tmp_path / 'missing.token'}"})
    r = D.d01(load_config(p, env={}), [], p)
    assert r.status == D.FAIL and "secret" in r.detail.lower()


def test_doctor_d01_ok_with_existing_secret_file(tmp_path):
    f = tmp_path / "ok.token"
    f.write_text("v\n")
    p = write_config(tmp_path, **{"matnexus.token": f"file:{f}"})
    assert D.d01(load_config(p, env={}), [], p).status in (D.OK, D.INFO)
