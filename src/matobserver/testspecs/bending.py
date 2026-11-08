"""3점굽힘 TestSpec `bending` (testspec.md §10, plugins.md §4.5).

TestSpec은 응답 정의와 데이터 매핑만 맡는다. 정렬·재샘플링·목적함수는 엔진 공통 기능이다(엔진 모듈을 import하지 않음).

옵션(레지스트리 defaults ← 엔진이 설정 `testspec.bending.*`와 `solver.implicit.t_end_s`·`solver.explicit.loading_velocity_m_s`를
같은 이름의 키로 주입):
`prefer_deflection`(false), `min_span_ratio`(4), `beam_theory_span_ratio`(10), `overtravel_ratio`(0.05),
`min_eps_p0`(0.01), `rate_from_time_channel`(true), `t_end_s`(1.0), `loading_velocity_m_s`(0.5),
초기값 맞춤 구간 `toe_fit_lo`(0.10)·`toe_fit_hi`(0.40)·`elastic_dev_tol`(0.05) — 엔진이 `alignment.*`도 주입.

`initial_hints(data, ...)`는 엔진이 정렬(1–4단계)을 마친 곡선을 `TestData`로 넘기면 그 `meta["k0"]`, `meta["x_elastic_end"]`,
`meta["X_u"]`를 쓰고, 없으면(원 곡선) 같은 맞춤 규칙으로 직접 계산한다.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal, Mapping, Sequence

import numpy as np

from matobserver.core import errors as E
from matobserver.core.types import (
    ExtractorRef,
    GeometrySpec,
    Issue,
    MeshResult,
    ParamSpace,
    RateInfo,
    ReferencePointRequest,
    ResponseDef,
    ResponseWindow,
    RunRecord,
    TestData,
)

_ALLOWED = ("box", "layered_box", "stp")


def _err(code: str, msg: str, field: str | None = None, **ctx: Any) -> Issue:
    return Issue(code=code, message=msg, severity="error", field=field, context=ctx)


def _warn(code: str, msg: str, field: str | None = None, **ctx: Any) -> Issue:
    return Issue(code=code, message=msg, severity="warning", field=field, context=ctx)


def _fnum(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _extractor(spec: Mapping[str, Any]) -> ExtractorRef:
    spec = dict(spec)
    name = spec.pop("name")
    return ExtractorRef(name=str(name), args=spec)


def _toe_fit(x: np.ndarray, y: np.ndarray, lo: float, hi: float) -> tuple[float, float] | None:
    if len(x) < 2:
        return None
    p = int(np.argmax(y))
    fp = float(y[p])
    if fp <= 0:
        return None
    m = (y >= lo * fp) & (y <= hi * fp) & (np.arange(len(y)) <= p)
    if int(np.sum(m)) < 2:
        return None
    k, b = np.polyfit(x[m], y[m], 1)
    if not (k > 0 and math.isfinite(k)):
        return None
    return float(k), float(b)


class BendingTestSpec:
    name: ClassVar[str] = "bending"
    version: ClassVar[str] = "1.0.0"
    matnexus_test_type_keys: ClassVar[tuple[str, ...]] = ("flexural",)
    load_mode: ClassVar[str] = "three_point_bending"  # result.md §4.7 equivalence.load_mode (엔진은 getattr로 읽는다)

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        o = dict(options or {})
        self.options = o
        self.prefer_deflection = bool(o.get("prefer_deflection", False))
        self.min_span_ratio = float(o.get("min_span_ratio", 4.0))
        self.beam_ratio = float(o.get("beam_theory_span_ratio", 10.0))
        self.overtravel = float(o.get("overtravel_ratio", 0.05))
        self.min_eps_p0 = float(o.get("min_eps_p0", 0.01))
        self.rate_from_time = bool(o.get("rate_from_time_channel", True))
        self.t_end_s = float(o.get("t_end_s", 1.0))
        self.v_explicit = float(o.get("loading_velocity_m_s", 0.5))
        self.toe_lo = float(o.get("toe_fit_lo", 0.10))
        self.toe_hi = float(o.get("toe_fit_hi", 0.40))
        self.dev_tol = float(o.get("elastic_dev_tol", 0.05))

    # ---- 선언 ----
    def allowed_geometries(self) -> tuple[str, ...]:
        return _ALLOWED

    def required_channels(self) -> tuple[tuple[str, ...], ...]:
        return (("force",), ("displacement", "deflection"))

    def required_dimensions(self) -> tuple[str, ...]:
        return ("width", "thickness", "span")

    def param_space(self, test_input: Mapping[str, Any], manifest: Mapping[str, Any]) -> ParamSpace:
        return ParamSpace(params=())  # 1차 bending: 비재료 식별 변수 없음 (R9 경로는 엔진에 존재)

    def param_targets(self) -> Mapping[str, str]:
        return {}

    def analysis_mode(self, test_input: Mapping[str, Any], data: Sequence[TestData]) -> Literal["quasi_static", "dynamic"]:
        return "quasi_static"

    def reference_points(self, manifest: Mapping[str, Any]) -> tuple[ReferencePointRequest, ...]:
        return (ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"}),)

    # ---- 검증 ----
    def _span(self, test_input: Mapping[str, Any], run: RunRecord | None, manifest: Mapping[str, Any]) -> float | None:
        rp = test_input.get("recipe_params") or test_input.get("recipe_params_si") or {}
        if _fnum(rp.get("span")) is not None:
            return _fnum(rp.get("span"))
        if run is not None and _fnum(run.specimen_dimensions.get("span")) is not None:
            return _fnum(run.specimen_dimensions.get("span"))
        for p in manifest.get("parameters") or []:
            if p.get("name") == "span" and _fnum(p.get("default")) is not None:
                return _fnum(p.get("default"))
        return None

    def validate(self, test_input: Mapping[str, Any], runs: Sequence[RunRecord], manifest: Mapping[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        geom = test_input.get("geometry") or {}
        kind = geom.get("kind")
        allowed = list(_ALLOWED)
        spec_kinds = (manifest.get("specimen") or {}).get("geometry_kinds")
        if spec_kinds:
            allowed = [k for k in allowed if k in spec_kinds]
        if kind is not None and kind not in allowed:
            issues.append(_err(E.GEOMETRY_KIND_NOT_ALLOWED, f"형상 {kind!r}는 굽힘 시험에서 허용되지 않음({allowed})",
                               "geometry.kind"))
        gdims = geom.get("dimensions") or {}
        req_ch = self.required_channels()
        for i, run in enumerate(runs):
            f = f"runs[{i}]"
            for group in req_ch:
                if not any(c in run.channels and len(np.asarray(run.channels[c])) > 0 for c in group):
                    issues.append(_err(E.CHANNEL_MISSING, f"필수 채널 없음: {' 또는 '.join(group)}", f, run_id=run.run_id))
            dims = dict(run.specimen_dimensions)
            dims.update({k: v for k, v in gdims.items() if v is not None})
            span = self._span(test_input, run, manifest)
            if span is not None:
                dims.setdefault("span", span)
            if kind != "stp":
                for d in self.required_dimensions():
                    if _fnum(dims.get(d)) is None:
                        issues.append(_err(E.SPECIMEN_DIMENSION_MISSING, f"시편 치수 없음: {d}", f, run_id=run.run_id))
            elif _fnum(dims.get("span")) is None:
                issues.append(_err(E.SPECIMEN_DIMENSION_MISSING, "시편 치수 없음: span", f, run_id=run.run_id))
            if _fnum(run.specimen_dimensions.get("load_span")) is not None:
                issues.append(_err(E.TEST_VARIANT_UNSUPPORTED, "load_span 값이 있음(4점 굽힘은 1차 미지원)", f,
                                   run_id=run.run_id))
            L = _fnum(dims.get("span"))
            h = _fnum(dims.get("thickness"))
            tl = _fnum(dims.get("total_length"))
            if L is not None:
                if L <= 0 or (tl is not None and L >= tl):
                    issues.append(_err(E.GEOMETRY_DIMENSION_INVALID, f"span {L} m 이 0 이하이거나 total_length 이상",
                                       f, run_id=run.run_id))
                elif h is not None and h > 0:
                    ratio = L / h
                    if ratio < self.min_span_ratio:
                        issues.append(_err(E.GEOMETRY_DIMENSION_INVALID,
                                           f"span/thickness = {ratio:.3g} < {self.min_span_ratio}", f, run_id=run.run_id))
                    elif ratio < self.beam_ratio:
                        issues.append(_warn(E.BEAM_THEORY_APPROXIMATE,
                                            f"span/thickness = {ratio:.3g} < {self.beam_ratio}: 보 이론 초기값 정확도 저하",
                                            f, run_id=run.run_id))
        return issues

    # ---- 매핑 ----
    def map_run(self, run: RunRecord, displacement_channel: str, compliance_m_per_n: float | None) -> TestData:
        ch = run.channels
        has_disp = "displacement" in ch and len(np.asarray(ch["displacement"])) > 0
        has_defl = "deflection" in ch and len(np.asarray(ch["deflection"])) > 0
        sel = displacement_channel or "auto"
        if sel == "auto":
            if self.prefer_deflection and has_defl:
                sel = "deflection"
            elif has_disp:
                sel = "displacement"
            elif has_defl:
                sel = "deflection"
            else:
                raise ValueError(f"{E.CHANNEL_MISSING}: run {run.run_id} has neither displacement nor deflection")
        if sel not in ("displacement", "deflection"):
            raise ValueError(f"displacement_channel must be auto|displacement|deflection, got {displacement_channel!r}")
        if sel == "deflection" and not has_defl or sel == "displacement" and not has_disp:
            raise ValueError(f"{E.DISPLACEMENT_CHANNEL_UNAVAILABLE}: channel {sel!r} missing in run {run.run_id}")
        x = np.asarray(ch[sel], dtype=float)  # 원 채널 그대로 (컴플라이언스 보정 금지, T6)
        y = np.asarray(ch["force"], dtype=float)
        source = "crosshead" if sel == "displacement" else "deflectometer"
        meta: dict[str, Any] = {
            "conditions": dict(run.conditions),
            "instrument": run.instrument,
            "specimen_id": run.specimen_id,
            "channel": sel,
        }
        if "time" in ch and len(np.asarray(ch["time"])) == len(x):
            meta["time"] = np.asarray(ch["time"], dtype=float)
        if source == "deflectometer":
            meta["compliance_skipped"] = "deflectometer"
        return TestData(
            run_id=run.run_id,
            x_quantity=sel,
            x=x,
            y=y,
            displacement_source=source,
            compliance_m_per_n=(compliance_m_per_n if source == "crosshead" else None),
            scalars={},
            dimensions={k: float(v) for k, v in run.specimen_dimensions.items() if _fnum(v) is not None},
            meta=meta,
        )

    # ---- 응답 ----
    def responses(self, manifest: Mapping[str, Any], x_quantity: str) -> tuple[ResponseDef, ...]:
        out: list[ResponseDef] = []
        for r in manifest.get("responses") or []:
            kind = r.get("kind", "curve")
            ext = dict(r.get("extractor") or {})
            axis = r.get("axis", "displacement") if kind == "curve" else None
            if kind == "curve" and x_quantity != axis:
                alt = (r.get("alt_axes") or {}).get(x_quantity)
                if alt is None:
                    raise ValueError(f"{E.DISPLACEMENT_CHANNEL_UNAVAILABLE}: response {r.get('response_key')!r} "
                                     f"has no alt_axes.{x_quantity}")
                ext = {**ext, **alt}
                axis = x_quantity
            w = r.get("window") or {}
            window = ResponseWindow(
                x_min=_fnum(w.get("x_min")), x_max=_fnum(w.get("x_max")),
                split=w.get("split", "auto"), region_weights=w.get("region_weights"),
            )
            out.append(ResponseDef(
                response_key=str(r.get("response_key", "force_disp")),
                kind=kind,
                test_channel=str(r.get("test_channel", "force")),
                axis=axis,
                extractor={k: _extractor(v) for k, v in ext.items()},
                weight=float(r.get("weight", 1.0)),
                window=window,
            ))
        if not out:  # 매니페스트에 응답이 없으면 §10.3 기본
            x_ext = ({"name": "nodout_disp", "node": {"ref": "indenter_ref"}, "component": "z", "sign": -1}
                     if x_quantity == "displacement" else
                     {"name": "nodout_rel_disp", "node": {"ref": "midspan_bottom"}, "ref_nodes": [{"ref": "support_top"}],
                      "component": "z", "sign": -1})
            out.append(ResponseDef(
                response_key="force_disp", kind="curve", test_channel="force", axis=x_quantity,
                extractor={"y": _extractor({"name": "rcforc_resultant", "contact_id": 1, "component": "z", "sign": -1,
                                            "scale": "symmetry"}),
                           "x": _extractor(x_ext)},
                weight=1.0, window=ResponseWindow(None, None, "auto", None)))
        return tuple(out)

    # ---- 초기값 ----
    def _geom_bh(self, geometry: GeometrySpec, data: Sequence[TestData]) -> tuple[float | None, float | None]:
        dims = dict(geometry.dimensions)
        b = _fnum(dims.get("width"))
        h = _fnum(dims.get("thickness"))
        if b is None and data:
            b = _fnum(np.mean([d.dimensions.get("width", np.nan) for d in data]))
        if h is None and data:
            h = _fnum(np.mean([d.dimensions.get("thickness", np.nan) for d in data]))
        return b, h

    def _span_from(self, params_si: Mapping[str, float], data: Sequence[TestData]) -> float | None:
        L = _fnum(params_si.get("span"))
        if L is None and data:
            L = _fnum(np.mean([d.dimensions.get("span", np.nan) for d in data]))
        return L

    def _curve_props(self, d: TestData) -> tuple[float, float, float] | None:
        """(k0, F(x_e), X_u) — 엔진 정렬 결과(meta)를 우선, 없으면 직접 맞춤."""
        meta = d.meta or {}
        if all(k in meta for k in ("k0", "x_elastic_end", "X_u")):
            k0, xe, xu = float(meta["k0"]), float(meta["x_elastic_end"]), float(meta["X_u"])
            fxe = float(np.interp(xe, d.x, d.y))
            return k0, fxe, xu
        x = np.asarray(d.x, dtype=float)
        y = np.asarray(d.y, dtype=float)
        if d.compliance_m_per_n:
            x = x - float(d.compliance_m_per_n) * y
        fit = _toe_fit(x, y, self.toe_lo, self.toe_hi)
        if fit is None:
            return None
        k0, b = fit
        xs = x + b / k0
        fp = float(np.max(y))
        p = int(np.argmax(y))
        x_hi = xs[np.argmax(y >= self.toe_hi * fp)]
        dev = (xs > x_hi) & (y < (1.0 - self.dev_tol) * k0 * xs) & (np.arange(len(y)) <= p)
        xe = float(xs[np.argmax(dev)]) if np.any(dev) else float(xs[p])
        fxe = float(np.interp(xe, xs, y))
        return k0, fxe, float(xs[p])

    def initial_hints(self, data: Sequence[TestData], geometry: GeometrySpec,
                      params_si: Mapping[str, float]) -> Mapping[str, float]:
        if geometry.kind == "stp":
            return {}
        b, h = self._geom_bh(geometry, data)
        L = self._span_from(params_si, data)
        if not (b and h and L) or b <= 0 or h <= 0 or L <= 0:
            return {}
        props = [p for p in (self._curve_props(d) for d in data) if p is not None]
        if not props:
            return {}
        E0 = float(np.mean([k0 * L**3 / (4.0 * b * h**3) for k0, _, _ in props]))
        sy0 = float(np.mean([3.0 * fxe * L / (2.0 * b * h**2) for _, fxe, _ in props]))
        xu = float(np.max([p[2] for p in props]))
        eps_p = max(6.0 * h * xu / L**2 - sy0 / E0, self.min_eps_p0)
        return {"E0": E0, "sigma_y0": sy0, "eps_p_max0": float(eps_p)}

    # ---- 기준 변형률속도 (§10.6) ----
    def reference_strain_rate(self, data: TestData, geometry: GeometrySpec, params_si: Mapping[str, float]) -> RateInfo:
        formula = "6*h*V/L^2"
        dims = dict(geometry.dimensions)
        h = _fnum(dims.get("thickness"))
        if h is None:
            h = _fnum(dims.get("load_height"))
        if h is None:
            h = _fnum(data.dimensions.get("thickness"))
        L = _fnum(params_si.get("span"))
        if L is None:
            L = _fnum(data.dimensions.get("span"))
        cond = (data.meta or {}).get("conditions") or {}
        V = _fnum(cond.get("test_speed"))
        source: str = "condition.test_speed"
        if V is None or V <= 0:
            V = None
            t = (data.meta or {}).get("time")
            if self.rate_from_time and t is not None and len(t) == len(data.x) and len(t) >= 3:
                t = np.asarray(t, dtype=float)
                x = np.asarray(data.x, dtype=float)
                y = np.asarray(data.y, dtype=float)
                p = int(np.argmax(y))
                seg = slice(0, max(p, 2) + 1)
                dt = np.diff(t[seg])
                dx = np.diff(x[seg])
                ok = dt > 0
                if np.any(ok):
                    V = float(np.median(dx[ok] / dt[ok]))
                    source = "time_channel"
                    if not (V > 0):
                        V = None
        inputs = {k: v for k, v in (("h", h), ("V", V), ("L", L)) if v is not None}
        if V is None or h is None or L is None or L <= 0:
            return RateInfo(value_1_s=None, formula=formula, inputs=inputs, velocity_source="none")
        return RateInfo(value_1_s=6.0 * h * V / L**2, formula=formula, inputs=inputs, velocity_source=source)  # type: ignore[arg-type]

    # ---- 엔진 파라미터 (§10.4) ----
    def engine_params(self, window: Mapping[str, float], mode: Literal["implicit", "explicit"],
                      mesh: MeshResult) -> Mapping[str, float]:
        xu = _fnum(window.get("X_u", window.get("x_max")))
        if xu is None or xu <= 0:
            raise ValueError("engine_params: window needs X_u (or x_max) > 0")
        dend = xu * (1.0 + self.overtravel)
        if mode == "implicit":
            tend = self.t_end_s
        else:
            tend = dend / self.v_explicit
        return {"mo_dend": dend, "mo_tend": tend, "mo_htop": float(mesh.top_z_at_load_m), "mo_hbot": float(mesh.bottom_z_m)}
