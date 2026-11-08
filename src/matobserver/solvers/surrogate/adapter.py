"""`surrogate_beam` SolverAdapter — 계약 테스트 기준 구현·대리모델 (plugins.md §2·§4.2, module-boundaries.md B-2).

LS-DYNA 없이 3점굽힘 F-δ를 보 이론 + 섬유 단면 탄소성 모델(`beam.py`)로 계산한다. 덱 대신 `surrogate_case.json`
(결정적 JSON)을 작업 폴더에 쓰고 `run`이 그것을 읽어 `surrogate_result.json`을 쓴다(무상태 어댑터, P-2).

* 단면: `case.specimen_mesh.mesh_path`(.k)에서 파트별 바운딩 박스를 읽어 슬롯(SlotBinding.part_ids)별 직사각형 셀로 만든다.
  `symmetry`가 half/quarter면 y=0 평면으로 대칭 복원. 메시를 읽지 못하면 MeshResult.bbox_m·top/bottom으로 단일 슬롯 단면.
* 스팬: `case.params_si["span"]`(없으면 매니페스트 `jig.params.span`이 가리키는 파라미터).
* 하중 경로: `engine_params_si[mode]`의 `mo_dend`·`mo_tend`, 출력 간격 `mo_dtout`(없으면 `output.dt_out_s`).
* 추출: `rcforc_resultant`(원시 = −F/force_scale, 인자 sign·scale 적용), `nodout_disp`·`nodout_rel_disp`(원시 = −δ, sign 적용),
  `time`. 강체 압자·지그 컴플라이언스 없음 → 압자 변위 = 중앙 처짐.
* 실패 모사(계약 테스트 S4·S7·S9용): 옵션 또는 매니페스트 `surrogate: {force_state: error|diverged, fail_implicit: true,
  simulated_runtime_s: <s>, mass_scaling: true}`. `simulated_runtime_s > timeout_s`면 `TIMEOUT`(부분 곡선).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping

import numpy as np

from matobserver.core.errors import IncompatibleMaterial, IncompatibleOverride
from matobserver.core.types import (
    CancelToken,
    CurveData,
    ElasticBlock,
    Extracted,
    NeutralMaterial,
    PreparedRun,
    RunOverrides,
    RunState,
    RunStatus,
    SimulationCase,
    SolverCapabilities,
    TableRow,
    neutral_material_violations,
)
from matobserver.core.units import UNIT_SYSTEM

from .beam import Section, Uniaxial, three_point_bending
from .kfile import part_bboxes

CASE_FILE = "surrogate_case.json"
RESULT_FILE = "surrogate_result.json"
UNIAXIAL_FILE = "surrogate_uniaxial.json"
EXTRACTORS = frozenset({"rcforc_resultant", "nodout_disp", "nodout_rel_disp", "time", "field_max", "glstat_ratio"})
MAX_POINTS = 20000


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")


def _plain(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _plain(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


class SurrogateBeamAdapter:
    name: ClassVar[str] = "surrogate_beam"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options: dict[str, Any] = dict(options or {})
        self.n_fibers = int(self.options.get("fibers", 240))
        self.explicit_ke_ratio = float(self.options.get("explicit_ke_ratio", 0.002))

    # ---- 선언 ----
    def capabilities(self) -> SolverCapabilities:
        return SolverCapabilities(
            analysis_modes=frozenset({"implicit", "explicit"}),
            extractors=EXTRACTORS,
            material_keywords=frozenset({"MAT_001", "MAT_024"}),
            extension_keys=frozenset(),
            unit_system=UNIT_SYSTEM,
            supports_overrides=True,
        )

    def solver_version(self) -> str | None:
        return f"surrogate_beam {self.version}"

    # ---- 준비 ----
    def _sim_opts(self, case: SimulationCase) -> dict[str, Any]:
        o = dict(self.options)
        o.update(dict((case.manifest or {}).get("surrogate") or {}))
        return o

    def _section_boxes(self, case: SimulationCase) -> list[list[Any]]:
        mesh = case.specimen_mesh
        sym = str(mesh.symmetry or "none")
        pid_slot: dict[int, str] = {}
        for b in case.slots:
            for pid in b.part_ids:
                pid_slot[int(pid)] = b.slot
        boxes: list[list[Any]] = []
        try:
            bb = part_bboxes(Path(mesh.mesh_path))
        except Exception:
            bb = {}
        for pid in sorted(bb):
            if pid not in pid_slot:
                continue
            _, _, y0, y1, z0, z1 = bb[pid]
            boxes.append([pid_slot[pid], y0, y1, z0, z1])
        if not boxes:  # 메시 없음: bbox 단일 단면
            (_, y0, z0), (_, y1, z1) = mesh.bbox_m
            z0, z1 = float(mesh.bottom_z_m), float(mesh.top_z_at_load_m)
            slot = case.slots[0].slot if case.slots else "body"
            boxes.append([slot, float(y0), float(y1), z0, z1])
        if sym in ("half", "quarter"):
            ymin = min(b[1] for b in boxes)
            if ymin >= -1e-9:
                mirrored = [[b[0], -b[2], -b[1], b[3], b[4]] for b in boxes]
                boxes = mirrored + boxes
        return [[b[0], round(float(b[1]), 12), round(float(b[2]), 12), round(float(b[3]), 12), round(float(b[4]), 12)]
                for b in boxes]

    def _span(self, case: SimulationCase) -> float:
        if "span" in case.params_si:
            return float(case.params_si["span"])
        jig = (case.manifest or {}).get("jig") or {}
        pname = (jig.get("params") or {}).get("span")
        if pname and pname in case.params_si:
            return float(case.params_si[pname])
        raise ValueError("surrogate_beam: case.params_si has no 'span'")

    def prepare(self, case: SimulationCase, materials: Mapping[str, NeutralMaterial], workdir: Path,
                mode: Literal["implicit", "explicit"] | None = None,
                overrides: RunOverrides | None = None) -> PreparedRun:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        sim = self._sim_opts(case)
        for slot, m in materials.items():
            bad = set(m.extensions) - set(self.capabilities().extension_keys)
            if bad:
                raise IncompatibleMaterial(f"surrogate_beam: unsupported material extensions {sorted(bad)} (slot {slot})")
        if overrides is not None:
            mode = "explicit"
            if overrides.forbid_mass_scaling and bool(sim.get("mass_scaling", False)):
                raise IncompatibleOverride("surrogate_beam: recipe uses mass scaling (DT2MS != 0)")
        if mode is None:
            mode = case.analysis_primary
        eng = {k: dict(v) for k, v in case.engine_params_si.items()}
        if overrides is not None and overrides.engine_params_si:
            eng.setdefault("explicit", {}).update({k: float(v) for k, v in overrides.engine_params_si.items()})
        doc = {
            "format": "matobserver.surrogate_beam/1",
            "case_id": case.case_id,
            "test_key": case.test_key,
            "recipe_ref": case.recipe_ref,
            "mode": mode,
            "fallback": case.analysis_fallback if overrides is None else "none",
            "span_m": self._span(case),
            "symmetry_force_scale": float(((case.manifest or {}).get("symmetry") or {}).get("force_scale", 1.0)),
            "section_boxes": self._section_boxes(case),
            "slots": [b.slot for b in case.slots],
            "materials": {k: v.to_card_blocks() for k, v in sorted(materials.items())},
            "engine_params_si": eng,
            "dt_out_s": float(case.output.dt_out_s),
            "responses": [_plain(r) for r in case.responses],
            "overrides": _plain(overrides) if overrides is not None else None,
            "sim": {k: _plain(v) for k, v in sorted(sim.items()) if k in
                    ("force_state", "fail_implicit", "simulated_runtime_s", "mass_scaling")},
        }
        main = workdir / CASE_FILE
        _dump(main, doc)
        return PreparedRun(workdir=workdir, case=case, main_file=main, mode=mode)

    # ---- 실행 ----
    def _solve(self, doc: Mapping[str, Any], mode: str) -> dict[str, Any]:
        eng = doc["engine_params_si"].get(mode) or doc["engine_params_si"].get("implicit") or {}
        dend = float(eng["mo_dend"])
        tend = float(eng["mo_tend"])
        dtout = float(eng.get("mo_dtout", doc["dt_out_s"]))
        n = int(min(MAX_POINTS, max(2, math.floor(tend / dtout + 1e-9) + 1)))
        t = np.linspace(0.0, (n - 1) * dtout, n) if (n - 1) * dtout <= tend * (1 + 1e-9) else np.linspace(0.0, tend, n)
        if t[-1] < tend * (1 - 1e-9):
            t = np.append(t, tend)
        delta = dend * t / tend
        mats_cb = doc["materials"]
        boxes = [tuple(b) for b in doc["section_boxes"]]
        sec = Section.from_boxes(boxes, self.n_fibers)
        mats = []
        for s in sec.slots:
            cb = mats_cb[s]
            table = cb["table"]
            mats.append(Uniaxial.from_card(cb["elastic"]["youngs_modulus"],
                                           None if table is None else [(r["plastic_strain"], r["true_stress"]) for r in table]))
        res = three_point_bending(sec, mats, float(doc["span_m"]), delta)
        return {"t": t.tolist(), "delta": delta.tolist(), "force": res.force.tolist(),
                "eps_p_max": res.eps_p_max, "ke_ie": (self.explicit_ke_ratio if mode == "explicit" else 0.0)}

    def run(self, prepared: PreparedRun, ncpu: int, timeout_s: float, cancel: CancelToken) -> RunStatus:
        t0 = time.perf_counter()
        doc = json.loads(Path(prepared.main_file).read_text(encoding="utf-8"))
        sim = doc.get("sim") or {}
        out_path = Path(prepared.workdir) / RESULT_FILE
        mode = doc["mode"]

        def finish(state: RunState, msg: str, payload: dict[str, Any] | None, mode_used: str, fb: bool) -> RunStatus:
            body = {"state": state.value, "mode_used": mode_used, "message": msg}
            if payload is not None:
                body.update(payload)
            _dump(out_path, body)
            return RunStatus(state=state, mode_used=mode_used, fallback_used=fb,
                             elapsed_s=time.perf_counter() - t0, termination_message=msg,
                             solver_version=self.solver_version(), license_waits=0, warnings=())

        if cancel is not None and cancel.is_set():
            return finish(RunState.CANCELLED, "cancelled", None, mode, False)
        for slot, cb in doc["materials"].items():
            # 불변식 검사용 재구성
            nm = NeutralMaterial(elastic=ElasticBlock(**cb["elastic"]),
                                 table=None if cb["table"] is None else tuple(TableRow(**r) for r in cb["table"]))
            if neutral_material_violations(nm):
                return finish(RunState.ERROR, "E r r o r    t e r m i n a t i o n (invalid material)", None, mode, False)
        forced = sim.get("force_state")
        if forced in ("error", "diverged"):
            st = RunState.ERROR if forced == "error" else RunState.DIVERGED
            return finish(st, f"E r r o r    t e r m i n a t i o n ({forced})", None, mode, False)
        fallback_used = False
        if mode == "implicit" and sim.get("fail_implicit"):
            if doc.get("fallback") == "explicit":
                mode = "explicit"
                fallback_used = True
            else:
                return finish(RunState.ERROR, "E r r o r    t e r m i n a t i o n (implicit)", None, "implicit", False)
        try:
            payload = self._solve(doc, mode)
        except Exception as e:  # 수치 실패 → ERROR
            return finish(RunState.ERROR, f"E r r o r    t e r m i n a t i o n ({type(e).__name__}: {e})", None, mode,
                          fallback_used)
        runtime = float(sim.get("simulated_runtime_s", 0.0) or 0.0)
        if runtime > timeout_s:
            frac = max(0.0, timeout_s / runtime)
            k = max(1, int(len(payload["t"]) * frac))
            for key in ("t", "delta", "force"):
                payload[key] = payload[key][:k]
            return finish(RunState.TIMEOUT, "timeout", payload, mode, fallback_used)
        return finish(RunState.NORMAL, "N o r m a l    t e r m i n a t i o n", payload, mode, fallback_used)

    # ---- 추출 ----
    def extract(self, prepared: PreparedRun, status: RunStatus) -> Extracted:
        p = Path(prepared.workdir) / RESULT_FILE
        empty = Extracted(curves={}, scalars={}, field_max={}, energy={})
        if not p.is_file():
            return empty
        body = json.loads(p.read_text(encoding="utf-8"))
        if "force" not in body:
            return Extracted(curves={r.response_key: CurveData(np.array([]), np.array([]), r.axis or "displacement", "force")
                                     for r in prepared.case.responses if r.kind == "curve"},
                             scalars={}, field_max={}, energy={})
        doc = json.loads(Path(prepared.main_file).read_text(encoding="utf-8"))
        t = np.asarray(body["t"], dtype=float)
        delta = np.asarray(body["delta"], dtype=float)
        F = np.asarray(body["force"], dtype=float)
        fs = float(doc.get("symmetry_force_scale", 1.0)) or 1.0
        raw_force = -F / fs  # 대칭 모델 원시 반력(−z)
        raw_disp = -delta  # 압자 z 변위(−z 진행)
        curves: dict[str, CurveData] = {}
        for r in prepared.case.responses:
            if r.kind != "curve":
                continue
            ey, ex = r.extractor["y"], r.extractor["x"]
            curves[r.response_key] = CurveData(x=self._apply(ex, raw_disp, raw_force, t, fs),
                                               y=self._apply(ey, raw_disp, raw_force, t, fs),
                                               x_quantity=r.axis or "displacement", y_quantity="force")
        field_max = {s: {"eff_plastic_strain": float(v)} for s, v in (body.get("eps_p_max") or {}).items()}
        energy = {"ke_ie_ratio_max": float(body.get("ke_ie", 0.0))} if body.get("mode_used") == "explicit" else {}
        return Extracted(curves=curves, scalars={}, field_max=field_max, energy=energy)

    @staticmethod
    def _apply(ext, raw_disp: np.ndarray, raw_force: np.ndarray, t: np.ndarray, fs: float) -> np.ndarray:
        name = ext.name
        args = dict(ext.args)
        sign = float(args.get("sign", 1.0))
        if name == "rcforc_resultant":
            scale = args.get("scale")
            k = fs if scale == "symmetry" else (float(scale) if isinstance(scale, (int, float)) else 1.0)
            return sign * k * raw_force
        if name in ("nodout_disp", "nodout_rel_disp"):
            return sign * raw_disp
        if name == "time":
            return t.copy()
        raise ValueError(f"surrogate_beam: unsupported extractor {name!r}")

    # ---- 카드 (1.1) ----
    def render_card(self, neutral: NeutralMaterial, slot: Any, options: Mapping[str, Any]) -> str:
        """card_format=None: 대리모델은 카드 산출물이 없다 (plugins.md §4.2, S13)."""
        raise NotImplementedError("surrogate_beam has no card format (card_format=None)")

    # ---- 정리 ----
    def cleanup(self, prepared: PreparedRun, keep: Literal["all", "results", "minimal"]) -> None:
        # 덱(case json)·결과(result json)만 쓰므로 지울 임시 파일이 없다 (작업 폴더 밖은 건드리지 않음).
        return None

    # ---- 1요소 단축 (왕복 검사, S11) ----
    def single_element_uniaxial(self, material: NeutralMaterial, strain_max: float, workdir: Path,
                                timeout_s: float) -> CurveData:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        if material.table is None:
            return CurveData(np.array([]), np.array([]), "plastic_strain", "true_stress")
        uni = Uniaxial.from_card(material.elastic.youngs_modulus,
                                 [(r.plastic_strain, r.true_stress) for r in material.table])
        nodes = np.array([r.plastic_strain for r in material.table])
        smax = float(strain_max) if strain_max and strain_max > 0 else float(nodes[-1])
        ep = np.union1d(nodes[nodes <= smax], np.linspace(0.0, smax, 101))
        sig = uni.stress_at_plastic_strain(ep)
        _dump(workdir / UNIAXIAL_FILE, {"material": material.to_card_blocks(), "strain_max": smax,
                                        "plastic_strain": ep.tolist(), "true_stress": sig.tolist()})
        return CurveData(x=ep, y=sig, x_quantity="plastic_strain", y_quantity="true_stress")
