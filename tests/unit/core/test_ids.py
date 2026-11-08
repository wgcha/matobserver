from __future__ import annotations

import re

import pytest

from matobserver.core import ids


def test_ulid_format_and_alphabet():
    u = ids.new_ulid()
    assert len(u) == 26 and ids.is_ulid(u)
    assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", u)  # Crockford: I L O U 없음


def test_ulid_known_timestamp_encoding():
    # 시각 0 -> 앞 10자 '0000000000'
    assert ids.new_ulid(0)[:10] == "0000000000"
    assert ids.ulid_timestamp_ms(ids.new_ulid(1_762_600_000_000)) == 1_762_600_000_000


def test_ulid_sortable_and_monotonic_within_ms():
    a = [ids.new_ulid(1_000) for _ in range(50)]
    assert a == sorted(a) and len(set(a)) == 50
    assert ids.new_ulid(1_000) < ids.new_ulid(1_001)


def test_ulid_unique():
    assert len({ids.new_ulid() for _ in range(2000)}) == 2000


def test_prefixed_ids():
    assert ids.new_job_id().startswith("cal_") and ids.is_valid_id("job", ids.new_job_id())
    assert ids.new_file_id().startswith("fil_")
    assert ids.new_artifact_id().startswith("art_")
    assert ids.new_verification_id().startswith("ver_")
    assert len(ids.new_job_id()) == 4 + 26
    assert not ids.is_valid_id("job", ids.new_file_id())
    assert not ids.is_valid_id("job", "cal_short")
    with pytest.raises(ValueError):
        ids.new_id("nope")


def test_api_doc_example_is_valid():
    assert ids.is_valid_id("job", "cal_01JC8Z6Q4M2V7R3T9X0A1B2C3D")


def test_invalid_ulid():
    assert not ids.is_ulid("01JC8Z6Q4M2V7R3T9X0A1B2C3")   # 25자
    assert not ids.is_ulid("01JC8Z6Q4M2V7R3T9X0A1B2C3I")  # I 금지
    with pytest.raises(ValueError):
        ids.ulid_timestamp_ms("bad")
    with pytest.raises(ValueError):
        ids.new_ulid(-1)
