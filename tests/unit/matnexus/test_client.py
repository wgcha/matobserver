"""B-6: MatNexus 클라이언트 — 기록된 응답(픽스처)으로 run·치수·curve 매핑, 5xx/타임아웃 -> 502 (api.md §2·§4.7)."""

from __future__ import annotations

import httpx
import numpy as np
import pytest

from matobserver.matnexus.client import MatNexusClient, RunNotFound, RunUnreadable, UpstreamUnavailable

from be_support import MN_BASE, RUN_ID, RUN_ID_BAD, RUN_ID_MISSING, SPECIMEN_ID, FakeMatNexus, calibration_body, load_json, make_config, mn_body


def client_for(mn: FakeMatNexus, **kw) -> MatNexusClient:
    return MatNexusClient(MN_BASE, "tok", 2.0, 5000, transport=httpx.MockTransport(mn.handler), **kw)


# ---------------------------------------------------------------- 매핑
def test_run_record_mapping_from_recorded_responses():
    mn = FakeMatNexus()
    rec = client_for(mn).fetch_run_record(RUN_ID)
    expected = load_json("matnexus", "curve_displacement_force.json")["points"]
    assert rec.run_id == RUN_ID and rec.test_type_key == "flexural"
    assert rec.instrument == "UTM-01" and rec.specimen_id == SPECIMEN_ID and rec.material_id == "3c3c3c3c-0000-4000-8000-000000000001"
    assert rec.source_sha256 == "ab" * 32
    assert set(rec.channels) == {"time", "displacement", "force"}
    np.testing.assert_allclose(rec.channels["displacement"], [p[0] for p in expected])
    np.testing.assert_allclose(rec.channels["force"], [p[1] for p in expected])
    assert len(rec.channels["time"]) == len(expected)
    assert rec.conditions == {"temperature": 296.15, "test_speed": 1.5e-4}


def test_specimen_dimensions_prefer_measured_over_nominal():
    rec = client_for(FakeMatNexus()).fetch_run_record(RUN_ID)
    assert rec.specimen_dimensions == {"width": 0.0101, "thickness": 0.004, "span": 0.064, "total_length": 0.080}
    assert "load_span" not in rec.specimen_dimensions  # measured·nominal 모두 null 이면 없다


def test_requests_use_documented_endpoints_and_auth():
    mn = FakeMatNexus()
    seen = []

    def spy(request: httpx.Request):
        seen.append(request)
        return mn.handler(request)

    c = MatNexusClient(MN_BASE + "/", "secret-pat", 2.0, 1234, transport=httpx.MockTransport(spy))
    c.fetch_run_record(RUN_ID)
    assert all(r.method == "GET" for r in seen)
    assert all(r.headers["authorization"] == "Bearer secret-pat" for r in seen)
    paths = [r.url.path for r in seen]
    assert paths[0] == f"/api/test-runs/{RUN_ID}" and paths[-1] == f"/api/specimens/{SPECIMEN_ID}/dimensions"
    curves = [r for r in seen if r.url.path.endswith("/curve")]
    assert {(r.url.params["x"], r.url.params["y"]) for r in curves} == {("time", "force"), ("displacement", "force")}
    assert all(r.url.params["max_points"] == "1234" for r in curves)


def test_status_imported_is_readable_and_others_are_not():
    mn = FakeMatNexus()
    mn.runs[RUN_ID]["status"] = "imported"
    assert client_for(mn).fetch_run_record(RUN_ID).run_id == RUN_ID
    for bad in ("failed", "parsing", "uploaded", None):
        mn.runs[RUN_ID]["status"] = bad
        with pytest.raises(RunUnreadable):
            client_for(mn).fetch_run_record(RUN_ID)


def test_unparsed_run_reports_parse_error():
    with pytest.raises(RunUnreadable, match="unexpected header"):
        client_for(FakeMatNexus()).fetch_run_record(RUN_ID_BAD)


def test_run_not_found():
    with pytest.raises(RunNotFound):
        client_for(FakeMatNexus()).fetch_run_record(RUN_ID_MISSING)


def test_missing_channel_curve_422_is_skipped():
    mn = FakeMatNexus()
    mn.runs[RUN_ID]["channels"] = ["time", "displacement", "deflection", "force"]  # deflection 곡선은 서버가 422
    rec = client_for(mn).fetch_run_record(RUN_ID)
    assert "deflection" not in rec.channels and "displacement" in rec.channels


def test_channel_with_different_force_samples_is_dropped_and_recorded():
    mn = FakeMatNexus()
    other = load_json("matnexus", "curve_displacement_force.json")
    other["points"] = [[x, y * 1.001] for x, y in other["points"]]  # LTTB 축약 어긋남을 흉내
    mn.curves[("displacement", "force")] = other
    rec = client_for(mn).fetch_run_record(RUN_ID)  # 첫 쌍(time, force)의 힘 배열이 기준
    assert "displacement" not in rec.channels and {"time", "force"} <= set(rec.channels)
    assert rec.conditions["_dropped_channels"] == ["displacement"]


def test_force_only_run():
    mn = FakeMatNexus()
    mn.runs[RUN_ID]["channels"] = ["force"]
    mn.curves[("force", "force")] = {"x": "force", "y": "force", "row_count": 3, "returned": 3, "points": [[1, 1], [2, 2], [3, 3]]}
    rec = client_for(mn).fetch_run_record(RUN_ID)
    assert list(rec.channels) == ["force"] and rec.channels["force"].tolist() == [1, 2, 3]


def test_run_without_specimen_has_no_dimensions():
    mn = FakeMatNexus()
    mn.runs[RUN_ID]["specimen_id"] = None
    assert client_for(mn).fetch_run_record(RUN_ID).specimen_dimensions == {}


def test_specimen_404_gives_empty_dimensions():
    mn = FakeMatNexus()
    mn.dimensions.clear()
    assert client_for(mn).fetch_run_record(RUN_ID).specimen_dimensions == {}


def test_no_specimen_dimension_without_values():
    mn = FakeMatNexus()
    mn.dimensions[SPECIMEN_ID] = load_json("matnexus", "dimensions_no_span.json")
    assert "span" not in client_for(mn).fetch_run_record(RUN_ID).specimen_dimensions


def test_fetched_hash_is_deterministic_and_content_sensitive():
    mn = FakeMatNexus()
    a, b = client_for(mn).fetch_run_record(RUN_ID), client_for(mn).fetch_run_record(RUN_ID)
    assert a.fetched_hash == b.fetched_hash and len(a.fetched_hash) >= 32
    mn.curves[("displacement", "force")] = {**mn.curves[("displacement", "force")],
                                            "points": [[x, y + 1.0] for x, y in mn.curves[("displacement", "force")]["points"]]}
    assert client_for(mn).fetch_run_record(RUN_ID).fetched_hash != a.fetched_hash


# ---------------------------------------------------------------- 실패 분류
@pytest.mark.parametrize("mode", ["5xx", "timeout", "401"])
def test_upstream_failures_raise_upstream_unavailable(mode):
    mn = FakeMatNexus()
    mn.mode = mode
    with pytest.raises(UpstreamUnavailable):
        client_for(mn).fetch_run_record(RUN_ID)


def test_connection_error_raises_upstream_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamUnavailable) as ei:
        MatNexusClient(MN_BASE, None, 1.0, 5000, transport=httpx.MockTransport(handler)).fetch_run_record(RUN_ID)
    assert "refused" not in str(ei.value)  # 내부 문구 비노출(예외 이름만)


def test_non_json_body_raises_upstream_unavailable():
    c = MatNexusClient(MN_BASE, None, 1.0, 5000, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(UpstreamUnavailable):
        c.get_run(RUN_ID)


def test_unexpected_status_raises_upstream_unavailable():
    c = MatNexusClient(MN_BASE, None, 1.0, 5000, transport=httpx.MockTransport(lambda r: httpx.Response(418)))
    with pytest.raises(UpstreamUnavailable):
        c.get_run(RUN_ID)


def test_missing_base_url_raises_upstream_unavailable():
    with pytest.raises(UpstreamUnavailable):
        MatNexusClient(None, None).get_run(RUN_ID)


def test_failure_in_curve_after_run_ok_is_upstream_unavailable():
    mn = FakeMatNexus()
    base = mn.handler

    def flaky(request):
        if request.url.path.endswith("/curve"):
            return httpx.Response(503)
        return base(request)

    with pytest.raises(UpstreamUnavailable):
        MatNexusClient(MN_BASE, None, 1.0, 5000, transport=httpx.MockTransport(flaky)).fetch_run_record(RUN_ID)


# ---------------------------------------------------------------- 설정
def test_from_config_resolves_env_token(monkeypatch, tmp_path):
    monkeypatch.setenv("MATNEXUS_TOKEN", "from-env")
    cfg = make_config(tmp_path, **{"matnexus.token": "env:MATNEXUS_TOKEN", "matnexus.curve_max_points": 777})
    c = MatNexusClient.from_config(cfg)
    assert (c.token, c.base_url, c.curve_max_points, c.timeout_s) == ("from-env", MN_BASE, 777, 2.0)


def test_from_config_tolerates_missing_env_token(monkeypatch, tmp_path):
    monkeypatch.delenv("MATNEXUS_TOKEN", raising=False)
    cfg = make_config(tmp_path, **{"matnexus.token": "env:MATNEXUS_TOKEN"})
    assert MatNexusClient.from_config(cfg).token is None


# ---------------------------------------------------------------- API 수준: 5xx/타임아웃 -> 502
@pytest.mark.parametrize("mode", ["5xx", "timeout", "401"])
def test_api_returns_502_when_matnexus_unreachable(active_env, mode):
    e = active_env
    e.mn.mode = mode
    r = e.post_job(mn_body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert "mn-test-token" not in r.text and "matnexus.test" not in r.text
    assert e.get("/v1/calibrations").json()["total"] == 0  # 잡이 만들어지지 않는다


def test_api_inline_runs_never_call_matnexus(active_env):
    e = active_env
    e.mn.mode = "5xx"
    assert e.post_job(calibration_body()).status_code == 201
    assert e.mn.calls == []


def test_api_maps_not_found_and_unreadable_to_400(active_env):
    assert active_env.post_job(mn_body(test_run_ids=[RUN_ID_MISSING])).json()["error"]["details"][0]["code"] == "RUN_NOT_FOUND"
    assert active_env.post_job(mn_body(test_run_ids=[RUN_ID_BAD])).json()["error"]["details"][0]["code"] == "RUN_UNREADABLE"


def test_api_fetches_each_run_once_per_request(active_env):
    e = active_env
    body = mn_body(test_run_ids=[RUN_ID, RUN_ID])
    assert e.post_job(body).status_code == 201
    assert sum(1 for m, p in e.mn.calls if p == f"/api/test-runs/{RUN_ID}") == 1


def test_api_missing_span_from_matnexus_is_specimen_dimension_missing(active_env):
    e = active_env
    e.mn.dimensions[SPECIMEN_ID] = load_json("matnexus", "dimensions_no_span.json")
    r = e.post_job(mn_body())
    assert r.status_code == 400 and "SPECIMEN_DIMENSION_MISSING" in [d["code"] for d in r.json()["error"]["details"]]
    # 요청의 recipe_params 로 span 을 주면 통과한다 (우선순위: recipe_params > 시편 치수 > 기본값)
    assert e.post_job(mn_body(recipe_params={"span": 0.064})).status_code == 201
