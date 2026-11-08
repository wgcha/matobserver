"""추출기 카탈로그 구현 (testspec.md §4) — binout(RCFORC·NODOUT·GLSTAT) → SI 곡선.

* 단위: binout 값은 덱 단위(mm·N·s) → SI(m·N·s)로 환산(module-boundaries.md §3).
* 부호 `sign`, 대칭 배율 `scale: symmetry`(→ 매니페스트 `symmetry.force_scale`) 또는 수치를 여기서 적용.
* x·y가 다른 블록이면 y(보통 힘)의 시간축에 x를 선형 보간해 짝짓는다(두 시간축의 공통 구간만).
* 블록·변수·ID가 없으면 그 응답은 빈 곡선(부분 결과 허용, plugins.md §4.2 `extract`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from ...core import units
from ...core.types import CurveData, Extracted, ExtractorRef, SimulationCase
from ...deck.assemble import resolve_node
from .binout import ResultSource, series

SUPPORTED_EXTRACTORS = frozenset({"rcforc_resultant", "nodout_disp", "nodout_rel_disp", "time", "field_max", "glstat_ratio"})

_COMPONENTS = ("x", "y", "z")


@dataclass(frozen=True)
class ExtractSettings:
    rcforc_side_codes: Mapping[str, int] = field(default_factory=lambda: {"slave": 0, "master": 1})


class ExtractionError(KeyError):
    pass


def _time(src: ResultSource, block: str) -> np.ndarray:
    return np.asarray(src.read(block, "time"), dtype=float).reshape(-1)


def rcforc_resultant(src: ResultSource, args: Mapping[str, Any], case: SimulationCase,
                     settings: ExtractSettings) -> tuple[np.ndarray, np.ndarray]:
    cid = int(args["contact_id"])
    side = str(args.get("side", "master"))
    ids = np.asarray(src.read("rcforc", "ids"), dtype=int).reshape(-1)
    try:
        sides = np.asarray(src.read("rcforc", "side"), dtype=int).reshape(-1)
    except KeyError:
        sides = None
    cols = np.flatnonzero(ids == cid)
    if sides is not None:
        code = settings.rcforc_side_codes.get(side)
        if code is None:
            raise ExtractionError(f"unknown rcforc side {side!r}")
        cols = cols[sides[cols] == int(code)]
    elif len(cols) >= 2:  # side 정보가 없으면 (slave, master) 순서로 본다
        cols = cols[[0 if side == "slave" else 1]]
    if len(cols) == 0:
        raise ExtractionError(f"rcforc: contact id {cid} side {side} not found")
    col = int(cols[0])
    t = _time(src, "rcforc")
    comp = str(args.get("component", "z"))
    if comp == "magnitude":
        f = np.sqrt(sum(series(src, "rcforc", f"{c}_force", len(ids))[:, col] ** 2 for c in _COMPONENTS))
    elif comp in _COMPONENTS:
        f = series(src, "rcforc", f"{comp}_force", len(ids))[:, col]
    else:
        raise ExtractionError(f"rcforc: unknown component {comp!r}")
    scale = args.get("scale")
    if scale is None:
        k = 1.0
    elif scale == "symmetry":
        k = float((case.manifest.get("symmetry") or {}).get("force_scale", 1.0))
    else:
        k = float(scale)
    f_si = units.to_si("force", f)
    return t, float(args.get("sign", 1)) * k * f_si


def _nodout_disp(src: ResultSource, node_id: int, comp: str) -> np.ndarray:
    ids = np.asarray(src.read("nodout", "ids"), dtype=int).reshape(-1)
    hit = np.flatnonzero(ids == node_id)
    if len(hit) == 0:
        raise ExtractionError(f"nodout: node {node_id} not found")
    if comp == "magnitude":
        return np.sqrt(sum(series(src, "nodout", f"{c}_displacement", len(ids))[:, hit[0]] ** 2 for c in _COMPONENTS))
    if comp not in _COMPONENTS:
        raise ExtractionError(f"nodout: unknown component {comp!r}")
    return series(src, "nodout", f"{comp}_displacement", len(ids))[:, hit[0]]


def nodout_disp(src: ResultSource, args: Mapping[str, Any], case: SimulationCase) -> tuple[np.ndarray, np.ndarray]:
    node = resolve_node(args["node"], case)
    u = _nodout_disp(src, node, str(args.get("component", "z")))
    return _time(src, "nodout"), float(args.get("sign", 1)) * units.mm_to_m(u)


def nodout_rel_disp(src: ResultSource, args: Mapping[str, Any], case: SimulationCase) -> tuple[np.ndarray, np.ndarray]:
    comp = str(args.get("component", "z"))
    node = resolve_node(args["node"], case)
    refs = [resolve_node(r, case) for r in args.get("ref_nodes", ())]
    if not refs:
        raise ExtractionError("nodout_rel_disp needs ref_nodes")
    u = _nodout_disp(src, node, comp)
    u_ref = np.mean([_nodout_disp(src, r, comp) for r in refs], axis=0)
    return _time(src, "nodout"), float(args.get("sign", 1)) * units.mm_to_m(u - u_ref)


def glstat_ratio(src: ResultSource, args: Mapping[str, Any] | None = None) -> float:
    num = str((args or {}).get("numerator", "kinetic"))
    den = str((args or {}).get("denominator", "internal"))
    a = np.asarray(src.read("glstat", f"{num}_energy"), dtype=float).reshape(-1)
    b = np.asarray(src.read("glstat", f"{den}_energy"), dtype=float).reshape(-1)
    if len(b) == 0:
        raise ExtractionError("glstat: empty")
    floor = 1e-6 * float(np.max(np.abs(b))) if np.max(np.abs(b)) > 0 else 0.0
    mask = np.abs(b) > floor
    if not mask.any():
        return 0.0
    return float(np.max(np.abs(a[mask]) / np.abs(b[mask])))


def _series_for(ref: ExtractorRef, src: ResultSource, case: SimulationCase,
                settings: ExtractSettings) -> tuple[np.ndarray, np.ndarray]:
    if ref.name == "rcforc_resultant":
        return rcforc_resultant(src, ref.args, case, settings)
    if ref.name == "nodout_disp":
        return nodout_disp(src, ref.args, case)
    if ref.name == "nodout_rel_disp":
        return nodout_rel_disp(src, ref.args, case)
    if ref.name == "time":
        block = str(ref.args.get("block", "rcforc"))
        t = _time(src, block)
        return t, t.copy()
    raise ExtractionError(f"extractor {ref.name!r} does not give a time series")


def pair_on_time(tx: np.ndarray, x: np.ndarray, ty: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """y 시간축(공통 구간)에 x를 선형 보간."""
    tx, x, ty, y = (np.asarray(a, dtype=float).reshape(-1) for a in (tx, x, ty, y))
    if len(tx) == 0 or len(ty) == 0:
        return np.array([]), np.array([])
    if len(tx) == len(ty) and np.array_equal(tx, ty):
        return x.copy(), y.copy()
    lo, hi = max(tx[0], ty[0]), min(tx[-1], ty[-1])
    m = (ty >= lo) & (ty <= hi)
    return np.interp(ty[m], tx, x), y[m]


FieldReader = Callable[[Path, SimulationCase], Mapping[str, Mapping[str, float]]]


def d3plot_field_max(rundir: Path, case: SimulationCase) -> Mapping[str, Mapping[str, float]]:
    """d3plot의 고체 요소 유효소성변형률 최대값(슬롯별, 전 시점). d3plot·lasso가 없으면 빈 매핑."""
    if not any(Path(rundir).glob("d3plot")):
        return {}
    try:
        from lasso.dyna import ArrayType, D3plot

        d3 = D3plot(str(Path(rundir) / "d3plot"))
        eps = np.asarray(d3.arrays[ArrayType.element_solid_effective_plastic_strain], dtype=float)
        part_idx = np.asarray(d3.arrays[ArrayType.element_solid_part_indexes], dtype=int)
        part_ids = np.asarray(d3.arrays[ArrayType.part_ids], dtype=int)
    except Exception:
        return {}
    eps_elem = eps.reshape(eps.shape[0], eps.shape[1], -1).max(axis=(0, 2))
    elem_pid = part_ids[part_idx]
    out: dict[str, dict[str, float]] = {}
    for b in case.slots:
        m = np.isin(elem_pid, list(b.part_ids))
        if m.any():
            out[b.slot] = {"eff_plastic_strain": float(eps_elem[m].max())}
    return out


def extract_case(src: ResultSource | None, case: SimulationCase, rundir: Path, mode_used: str,
                 settings: ExtractSettings, field_reader: FieldReader = d3plot_field_max) -> Extracted:
    curves: dict[str, CurveData] = {}
    scalars: dict[str, float] = {}
    for r in case.responses:
        xq = r.axis or "displacement"
        if r.kind == "curve":
            x = y = np.array([])
            if src is not None:
                try:
                    tx, xv = _series_for(r.extractor["x"], src, case, settings)
                    ty, yv = _series_for(r.extractor["y"], src, case, settings)
                    x, y = pair_on_time(tx, xv, ty, yv)
                except (KeyError, ValueError, IndexError):
                    x = y = np.array([])
            curves[r.response_key] = CurveData(x=x, y=y, x_quantity=xq, y_quantity=r.test_channel)
        else:
            ref = r.extractor.get("value")
            if src is None or ref is None:
                continue
            try:
                if ref.name == "glstat_ratio":
                    scalars[r.response_key] = glstat_ratio(src, ref.args)
                else:
                    _, v = _series_for(ref, src, case, settings)
                    if len(v):
                        scalars[r.response_key] = float(np.max(v))
            except (KeyError, ValueError, IndexError):
                pass
    energy: dict[str, float] = {}
    if mode_used == "explicit" and src is not None:
        try:
            energy["ke_ie_ratio_max"] = glstat_ratio(src, {"numerator": "kinetic", "denominator": "internal"})
        except (KeyError, ValueError, IndexError):
            pass
    field_max = dict(field_reader(rundir, case))
    return Extracted(curves=curves, scalars=scalars, field_max=field_max, energy=energy)
