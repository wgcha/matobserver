"""(e) 해시 정규화 결정성."""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path

import numpy as np
import pytest

from matobserver.core.hashing import (
    canonical_json,
    canonical_json_bytes,
    sha256_digest,
    sha256_hex,
    sha256_json,
)


def test_key_order_does_not_matter():
    a = {"b": 1, "a": {"y": [1, 2], "x": None}}
    b = {"a": {"x": None, "y": [1, 2]}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_json(a) == sha256_json(b)


def test_no_whitespace_and_sorted_keys():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_digest_format_and_known_vector():
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_digest(b"") == "sha256:" + sha256_hex(b"")
    assert sha256_json({}) == sha256_digest(b"{}")


def test_deterministic_across_calls_and_hash_seeds():
    obj = {"set": {3, 1, 2}, "tuple": (1, 2), "path": Path("a/b"), "f": 0.1 + 0.2}
    assert len({sha256_json(obj) for _ in range(20)}) == 1


def test_integral_float_equals_int_and_negative_zero():
    assert sha256_json({"a": 1.0}) == sha256_json({"a": 1})
    assert sha256_json({"a": -0.0}) == sha256_json({"a": 0})
    assert sha256_json({"a": 1.5}) != sha256_json({"a": 1})
    assert canonical_json({"a": 2.1e11}) == '{"a":210000000000}'
    assert canonical_json({"a": 1e300}) == '{"a":1e+300}'  # 2^53 이상은 float 표기 유지


def test_bool_is_not_int():
    assert sha256_json({"a": True}) != sha256_json({"a": 1})


def test_types_normalized():
    class Color(Enum):
        RED = "red"

    @dataclasses.dataclass
    class D:
        x: float
        y: tuple

    obj = {"arr": np.array([1.0, 2.5]), "np_int": np.int64(3), "np_f": np.float64(0.5), "np_b": np.bool_(True),
           "enum": Color.RED, "dc": D(1.0, (1, 2)), "p": Path("x") / "y", "fs": frozenset({"b", "a"})}
    out = json.loads(canonical_json(obj))
    assert out == {"arr": [1, 2.5], "np_int": 3, "np_f": 0.5, "np_b": True, "enum": "red",
                   "dc": {"x": 1, "y": [1, 2]}, "p": "x/y", "fs": ["a", "b"]}


def test_list_order_matters():
    assert sha256_json([1, 2]) != sha256_json([2, 1])


def test_utf8_not_escaped():
    assert canonical_json({"k": "굽힘"}) == '{"k":"굽힘"}'
    assert canonical_json_bytes({"k": "굽힘"}) == '{"k":"굽힘"}'.encode("utf-8")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), np.float64("nan")])
def test_non_finite_rejected(bad):
    with pytest.raises(ValueError):
        canonical_json({"a": bad})


def test_non_string_keys_and_unknown_types_rejected():
    with pytest.raises(TypeError):
        canonical_json({1: "a"})
    with pytest.raises(TypeError):
        canonical_json({"a": object()})
