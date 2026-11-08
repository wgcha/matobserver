"""InlineRun 정규화 (api.md §4.5)."""

from __future__ import annotations

import pytest

from matobserver.matnexus.normalize import InlineRunError, normalize_inline_run

from be_support import inline_run


def test_normalizes_to_run_record():
    r = normalize_inline_run(inline_run("abc"))
    assert r.run_id == "inline:abc" and r.test_type_key == "flexural" and r.instrument == "UTM-01" and r.specimen_id == "S-01"
    assert set(r.channels) == {"time", "displacement", "force"} and r.material_id is None
    assert r.specimen_dimensions["span"] == 0.064 and r.conditions["temperature"] == 296.15


def test_hash_depends_on_data_only():
    a, b = normalize_inline_run(inline_run("x")), normalize_inline_run(inline_run("y"))
    assert a.fetched_hash == b.fetched_hash and a.run_id != b.run_id
    run = inline_run("x")
    run["channels"]["force"]["values"][3] += 0.5
    assert normalize_inline_run(run).fetched_hash != a.fetched_hash


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["channels"]["force"].update(unit="kN"), "CHANNEL_UNIT_INVALID"),
    (lambda r: r["channels"]["displacement"].update(unit="mm"), "CHANNEL_UNIT_INVALID"),
    (lambda r: r["channels"]["time"].update(unit="min"), "CHANNEL_UNIT_INVALID"),
    (lambda r: r["channels"]["force"].update(values=r["channels"]["force"]["values"][:-1]), "INLINE_RUN_INVALID"),
    (lambda r: r["channels"]["force"].update(values=[]), "INLINE_RUN_INVALID"),
    (lambda r: r["channels"]["force"]["values"].__setitem__(2, float("nan")), "INLINE_RUN_INVALID"),
    (lambda r: r["channels"]["force"]["values"].__setitem__(2, "x"), "INLINE_RUN_INVALID"),
    (lambda r: r["channels"].update(bogus={"unit": "N", "values": [1.0]}), "INLINE_RUN_INVALID"),
    (lambda r: r["specimen_dimensions"].update(width=float("inf")), "INLINE_RUN_INVALID"),
])
def test_invalid_inline_runs(mutate, code):
    run = inline_run()
    mutate(run)
    with pytest.raises(InlineRunError) as ei:
        normalize_inline_run(run)
    assert ei.value.code == code and ei.value.field


def test_api_duplicate_run_key_with_different_data_across_tests(active_env):
    e = active_env
    from be_support import calibration_body

    b = calibration_body()
    t1 = {k: b.pop(k) for k in ("test_type", "recipe", "geometry", "inline_runs")}
    t2 = {**t1, "inline_runs": [inline_run()]}
    t2["inline_runs"][0]["channels"]["force"]["values"][5] += 1.0
    b["tests"] = [{**t1, "test_key": "a"}, {**t2, "test_key": "b"}]
    r = e.post_job(b)
    assert r.status_code == 400 and "SCHEMA_INVALID" in [d["code"] for d in r.json()["error"]["details"]]
    t2["inline_runs"] = [inline_run()]  # 같은 데이터면 허용
    b["tests"][1] = {**t2, "test_key": "b"}
    assert e.post_job(b).status_code == 201
