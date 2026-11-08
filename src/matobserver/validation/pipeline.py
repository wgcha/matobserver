"""잡 생성 검증 파이프라인 (api.md §4.7).

순서: ① 스키마 ② 레지스트리 이름 ③ 레시피 상태·시험 종류 ④ 슬롯 대응 ⑤ 시험 데이터 조회·매핑(TestSpec) ⑥ 곡선 품질 ⑦ 형상(STP 포함)
⑧ 파라미터 범위 ⑨ 조합 호환(재료–솔버–시험) ⑩ 식별성 사전 점검. 오류는 한꺼번에 모아 `details[]` 로 돌려준다(단계 순 정렬).

* ⑤ 에 오류가 하나라도 있으면 ⑥ 이후는 건너뛴다 (api.md §4.7). ②–④ 의 오류는 해당 시험의 ⑤ 만 건너뛴다.
* ⑥ 중 점 수(`CURVE_TOO_FEW_POINTS`)는 여기서, 곡선 정렬 1–5단계(`CURVE_*`)와 ⑩ 식별성은 엔진 `preflight` 에 맡긴다.
  `preflight` 는 ①–⑨ 가 모두 통과한 뒤에만 호출한다 (불완전한 입력으로 엔진을 부르지 않는다).
* MatNexus 에 닿지 못하면(`UpstreamUnavailable`) 검증을 중단하고 502 로 보고한다.
* 플러그인이 낸 `Issue` 는 error -> `details[]`, warning -> `warnings[]` 로 옮긴다 (코드·field·context 유지).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import ValidationError

from ..api.schemas import (
    SCHEMA_MAJOR,
    SCHEMA_MINOR,
    SCHEMA_VERSION,
    SHORTHAND_FIELDS,
    CalibrationRequest,
)
from ..core import errors as E
from ..core.config import Config
from ..core.errors import MatObserverError, UnknownPluginError
from ..core.hashing import sha256_json
from ..core.registry import Registry
from ..core.types import (
    CalibrationSpec,
    Issue,
    NeutralMaterial,
    ResolvedRecipe,
    RunRecord,
    neutral_material_violations,
)
from ..jobs.files import FileService
from ..matnexus.client import MatNexusClient, RunNotFound, RunUnreadable, UpstreamUnavailable
from ..matnexus.normalize import InlineRunError, normalize_inline_run
from ..recipes.cards import CardBlocksError, neutral_from_card_blocks
from ..recipes.repository import ACTIVE, RETIRED, RecipeRepository
from ..recipes.static_check import ELFORMS
from .errors import ValidationFailed, make_detail, sort_details
from .hooks import EngineHooks
from .spec import build_spec, geometry_spec_from_dict, run_to_json

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class ValidationContext:
    config: Config
    registry: Registry
    recipes: RecipeRepository
    files: FileService
    matnexus: MatNexusClient | None = None
    hooks: EngineHooks = field(default_factory=EngineHooks)


@dataclass
class ValidatedRequest:
    job_id: str
    request: dict[str, Any]  # 정규화(기본값 채움) 요청 본문 — request.json 으로 저장
    request_hash: str
    snapshot: dict[str, Any]  # 잡 입력 스냅샷 (JobStore.spec_json)
    spec: CalibrationSpec
    runs: dict[str, RunRecord]  # run_id -> RunRecord (inputs/runs/*.json 으로 저장)
    stp_files: dict[str, Path]  # file_id -> 원본 경로 (inputs/stp/ 로 복사)
    warnings: list[dict[str, Any]]
    material_id: str | None
    tests: list[dict[str, str]]  # [{test_key, test_type, recipe_ref}]


class PreflightServices:
    """검증 단계에서 엔진 `preflight` 에 주는 EngineServices. artifact·이력은 만들 수 없다."""

    owner_kind = "job"

    class _NoCancel:
        def is_set(self) -> bool:
            return False

    def __init__(self, registry: Registry, config: Config, owner_id: str, tmp_root: Path) -> None:
        self.registry, self.config, self.owner_id = registry, config, owner_id
        self.cancel = self._NoCancel()
        self._tmp_root = tmp_root
        self._dir: Path | None = None

    def workdir(self, *parts: str) -> Path:
        if self._dir is None:
            self._tmp_root.mkdir(parents=True, exist_ok=True)
            self._dir = Path(tempfile.mkdtemp(prefix="preflight-", dir=self._tmp_root))
        p = self._dir.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_artifact(self, path: Path, kind: str, *, slot: str | None = None, test_key: str | None = None) -> str:
        raise RuntimeError("preflight 중에는 artifact 를 만들 수 없습니다.")

    def record_eval(self, item: Mapping[str, Any]) -> None:
        return None

    def progress(self, stage: str, **fields: Any) -> None:
        return None

    def cleanup(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON 에 허용되지 않는 값: {name}")


def parse_json_body(raw: bytes) -> Any:
    """요청 본문 JSON 파싱. NaN/Infinity 거부. 실패는 ValidationFailed(SCHEMA_INVALID)."""
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValidationFailed([make_detail(E.SCHEMA_INVALID, f"요청 본문이 올바른 JSON 이 아닙니다: {e}")]) from e


def _loc(loc: Sequence[Any]) -> str:
    out = ""
    for p in loc:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def _issue_detail(issue: Issue, default_test_key: str | None = None) -> dict[str, Any]:
    ctx = dict(issue.context or {})
    return make_detail(issue.code, issue.message, field=issue.field,
                       test_key=ctx.get("test_key", default_test_key), run_id=ctx.get("run_id"),
                       slot=ctx.get("slot"), param=ctx.get("param"))


def _issue_warning(issue: Issue, default_test_key: str | None = None) -> dict[str, Any]:
    ctx = dict(issue.context or {})
    w: dict[str, Any] = {"code": issue.code, "message": issue.message}
    tk = ctx.get("test_key", default_test_key)
    if tk is not None:
        w["test_key"] = tk
    for k in ("slot", "run_id", "param"):
        if ctx.get(k) is not None:
            w[k] = ctx[k]
    return w


def _plugin_ref(ref: Any, default_name: str) -> tuple[str, dict[str, Any]]:
    if ref is None:
        return default_name, {}
    if isinstance(ref, str):
        return ref, {}
    return ref["name"], dict(ref.get("options") or {})


class _Validator:
    def __init__(self, ctx: ValidationContext, job_id: str) -> None:
        self.ctx, self.cfg, self.reg = ctx, ctx.config, ctx.registry
        self.job_id = job_id
        self.details: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.step_failed: set[int] = set()
        self.req: dict[str, Any] = {}
        # 해석된 중간 결과
        self.specs: dict[str, Any] = {}  # test_key -> TestSpec 인스턴스
        self.recipe_rows: dict[str, dict[str, Any]] = {}  # test_key -> 레시피 행
        self.tests_runs: dict[str, list[RunRecord]] = {}
        self.models: dict[str, Any] = {}  # slot -> MaterialModel 인스턴스
        self.model_names: dict[str, str] = {}
        self.model_options: dict[str, dict[str, Any]] = {}
        self.references: dict[str, NeutralMaterial | None] = {}
        self.ref_blocks: dict[str, dict[str, Any] | None] = {}
        self.names: dict[str, tuple[str, dict[str, Any]]] = {}
        self.geometry: dict[str, dict[str, Any]] = {}  # test_key -> 스냅샷 geometry dict
        self.run_cache: dict[str, RunRecord] = {}
        self.explicit: dict[str, Any] = {"enabled": False}

    # ---------------------------------------------------------------- 도우미
    def err(self, code: str, message: str, field: str | None = None, **ctx: Any) -> None:
        self.details.append(make_detail(code, message, field=field, **ctx))

    def warn(self, code: str, message: str, **ctx: Any) -> None:
        w = {"code": code, "message": message}
        w.update({k: v for k, v in ctx.items() if v is not None})
        self.warnings.append(w)

    def has_error_in(self, steps: Iterable[int]) -> bool:
        from .errors import step_of

        s = set(steps)
        return any(step_of(d["code"]) in s for d in self.details)

    def add_issues(self, issues: Iterable[Issue], test_key: str | None = None) -> None:
        for i in issues:
            if i.severity == "error":
                self.details.append(_issue_detail(i, test_key))
            else:
                self.warnings.append(_issue_warning(i, test_key))

    # ================================================================ ① 스키마
    def step1(self, body: Any) -> bool:
        if not isinstance(body, dict):
            self.err(E.SCHEMA_INVALID, "요청 본문은 JSON 객체여야 합니다.")
            return False
        sv = body.get("schema_version")
        if not isinstance(sv, str) or not re.match(r"^\d+\.\d+$", sv):
            self.err(E.SCHEMA_INVALID, "schema_version 은 \"MAJOR.MINOR\" 문자열이어야 합니다.", "schema_version")
            return False
        major, minor = (int(p) for p in sv.split("."))
        if major != SCHEMA_MAJOR or minor > SCHEMA_MINOR:
            self.err(E.SCHEMA_VERSION_UNSUPPORTED, f"지원하지 않는 schema_version {sv!r} (서버 {SCHEMA_VERSION})", "schema_version")
            return False
        shorthand = [k for k in SHORTHAND_FIELDS if k in body]
        data = dict(body)
        prefix_strip = False
        if "tests" in data:
            if shorthand:
                self.err(E.SCHEMA_INVALID, f"tests 와 단일 시험 약식 필드를 함께 쓸 수 없습니다: {shorthand}", "tests")
                return False
        elif shorthand:
            t = {k: data.pop(k) for k in shorthand}
            t["test_key"] = "t1"
            data["tests"] = [t]
            prefix_strip = True
        else:
            data.setdefault("tests", [])
        try:
            model = CalibrationRequest.model_validate(data)
        except ValidationError as e:
            for er in e.errors():
                f = _loc(er["loc"])
                if prefix_strip and f.startswith("tests[0]"):
                    f = f[len("tests[0]"):].lstrip(".")
                if er["type"] == "extra_forbidden":
                    msg = "정의되지 않은 필드입니다."
                else:
                    msg = er["msg"]
                self.err(E.SCHEMA_INVALID, msg, f or None)
            return False
        self.req = model.model_dump(mode="json", exclude_none=True)
        self.shorthand = prefix_strip
        # 개수 제약
        a = self.cfg
        if not 1 <= len(model.tests) <= a.get("api.max_tests_per_job"):
            self.err(E.SCHEMA_INVALID, f"tests 는 1..{a.get('api.max_tests_per_job')}개여야 합니다.", "tests")
        if not 1 <= len(model.materials.slots) <= a.get("api.max_slots"):
            self.err(E.SCHEMA_INVALID, f"materials.slots 는 1..{a.get('api.max_slots')}개여야 합니다.", "materials.slots")
        seen_keys: set[str] = set()
        for i, t in enumerate(model.tests):
            base = "" if (prefix_strip and i == 0) else f"tests[{i}]."
            key = t.test_key or f"t{i + 1}"
            self.req["tests"][i]["test_key"] = key
            if key in seen_keys:
                self.err(E.SCHEMA_INVALID, f"test_key 중복: {key!r}", f"{base}test_key")
            seen_keys.add(key)
            has_ids, has_inline = t.test_run_ids is not None, t.inline_runs is not None
            if has_ids == has_inline:
                self.err(E.SCHEMA_INVALID, "test_run_ids 와 inline_runs 중 정확히 하나만 있어야 합니다.", f"{base}test_run_ids")
            else:
                n = len(t.test_run_ids if has_ids else t.inline_runs)  # type: ignore[arg-type]
                if not 1 <= n <= a.get("api.max_runs_per_test"):
                    self.err(E.SCHEMA_INVALID, f"run 은 1..{a.get('api.max_runs_per_test')}개여야 합니다.",
                             f"{base}{'test_run_ids' if has_ids else 'inline_runs'}", test_key=key)
            for j, rid in enumerate(t.test_run_ids or []):
                if not _UUID_RE.match(rid):
                    self.err(E.SCHEMA_INVALID, f"MatNexus TestRun ID(UUID) 형식이 아닙니다: {rid!r}", f"{base}test_run_ids[{j}]")
            keys = [r.run_key for r in (t.inline_runs or [])]
            if len(set(keys)) != len(keys):
                self.err(E.SCHEMA_INVALID, "inline_runs 의 run_key 가 중복됩니다.", f"{base}inline_runs", test_key=key)
            g = t.geometry
            if g.kind == "layered_box" and not g.layers:
                self.err(E.SCHEMA_INVALID, "layered_box 는 layers 가 필요합니다.", f"{base}geometry.layers", test_key=key)
            if g.kind == "stp" and not g.file_id:
                self.err(E.SCHEMA_INVALID, "stp 는 file_id 가 필요합니다.", f"{base}geometry.file_id", test_key=key)
        names = [s.slot for s in model.materials.slots]
        if len(set(names)) != len(names):
            self.err(E.SCHEMA_INVALID, "materials.slots 의 slot 이름이 중복됩니다.", "materials.slots")
        if model.friction is not None and model.friction.mode == "fixed" and model.friction.value is None:
            pass  # value 생략 = 레시피 fric 기본값
        if model.explicit_check is not None and model.explicit_check.tests is not None:
            for k in model.explicit_check.tests:
                if k not in seen_keys:
                    self.err(E.SCHEMA_INVALID, f"explicit_check.tests 의 {k!r} 는 tests 에 없습니다.", "explicit_check.tests")
        return not self.has_error_in([1])

    def field_prefix(self, i: int) -> str:
        return "" if (getattr(self, "shorthand", False) and i == 0) else f"tests[{i}]."

    # ================================================================ ② 레지스트리 이름
    def step2(self) -> None:
        cfg, reg = self.cfg, self.reg
        self.plasticity_mode = self.req.get("plasticity_mode") or cfg.get("engine.default_plasticity_mode")
        self.names["optimizer"] = _plugin_ref(self.req.get("optimizer"), cfg.get("engine.stages.plastic.optimizer"))
        self.names["elastic_optimizer"] = _plugin_ref(self.req.get("elastic_optimizer"), cfg.get("engine.stages.elastic.optimizer"))
        self.names["solver"] = _plugin_ref(self.req.get("solver"), cfg.get("engine.default_solver"))
        for fld, key, ptype, code in (("optimizer", "optimizer", "optimizer", E.OPTIMIZER_UNKNOWN),
                                      ("elastic_optimizer", "elastic_optimizer", "optimizer", E.OPTIMIZER_UNKNOWN),
                                      ("solver", "solver", "solver", E.SOLVER_UNKNOWN)):
            name, _ = self.names[key]
            try:
                reg.enabled_entry(ptype, name)
            except UnknownPluginError:
                self.err(code, f"등록되지 않았거나 비활성인 {ptype}: {name!r}", fld)
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            try:
                reg.enabled_entry("test_spec", t["test_type"])
            except UnknownPluginError:
                self.err(E.TEST_TYPE_UNKNOWN, f"등록되지 않았거나 비활성인 TestSpec: {t['test_type']!r}",
                         self.field_prefix(i) + "test_type", test_key=tk)
                continue
            try:
                self.specs[tk] = reg.create("test_spec", t["test_type"], {})
            except Exception as e:  # noqa: BLE001 - 플러그인 초기화 실패는 요청 오류로 보고
                self.err(E.TEST_TYPE_UNKNOWN, f"TestSpec {t['test_type']!r} 를 초기화할 수 없습니다: {e}",
                         self.field_prefix(i) + "test_type", test_key=tk)
        ctx = {"plasticity_mode": self.plasticity_mode}
        for i, s in enumerate(self.req["materials"]["slots"]):
            f = f"materials.slots[{i}].material_model"
            try:
                entry = reg.enabled_entry("material_model", s["material_model"], ctx)
            except UnknownPluginError:
                self.err(E.MATERIAL_MODEL_UNKNOWN, f"등록되지 않았거나 비활성인 재료 모델: {s['material_model']!r}", f, slot=s["slot"])
                continue
            self.model_names[s["slot"]] = entry.name
            self.model_options[s["slot"]] = reg.merged_options("material_model", entry.name, s.get("options"))

    # ================================================================ ③ 레시피
    def step3(self) -> None:
        repo = self.ctx.recipes
        for i, t in enumerate(self.req["tests"]):
            tk, f = t["test_key"], self.field_prefix(i) + "recipe"
            rid, _, ver = t["recipe"].partition("@")
            row = repo.get_best(rid, int(ver))
            if row is None:
                self.err(E.RECIPE_NOT_FOUND, f"등록되지 않은 레시피: {t['recipe']}", f, test_key=tk)
                continue
            if row["status"] == RETIRED:
                self.err(E.RECIPE_RETIRED, f"폐기된 레시피입니다: {t['recipe']}", f, test_key=tk)
                continue
            if row["status"] != ACTIVE:
                self.err(E.RECIPE_NOT_ACTIVE, f"사용 가능한(active) 레시피가 아닙니다 ({row['status']}): {t['recipe']}", f, test_key=tk)
                continue
            self.recipe_rows[tk] = row
            man = row["manifest"]
            if man["test_type"] != t["test_type"]:
                self.err(E.RECIPE_TEST_TYPE_MISMATCH,
                         f"레시피 test_type {man['test_type']!r} 가 요청 {t['test_type']!r} 와 다릅니다.", f, test_key=tk)
            params = {p["name"]: p for p in man["parameters"]}
            for name, val in (t.get("recipe_params") or {}).items():
                pf = self.field_prefix(i) + f"recipe_params.{name}"
                if name not in params:
                    self.err(E.RECIPE_PARAM_UNKNOWN, f"레시피에 없는 파라미터: {name!r}", pf, test_key=tk, param=name)
                    continue
                lo, hi = params[name]["range"]
                if not lo <= val <= hi:
                    self.err(E.RECIPE_PARAM_OUT_OF_RANGE, f"{name}={val} 가 레시피 범위 [{lo}, {hi}] 밖입니다.", pf, test_key=tk, param=name)

    # ================================================================ ④ 슬롯
    def step4(self) -> None:
        cfg = self.cfg
        mat = self.req["materials"]
        slots = mat["slots"]
        names = {s["slot"] for s in slots}
        if not any(s["role"] == "identify" for s in slots):
            self.err(E.SLOT_NO_IDENTIFY, "식별할(identify) 슬롯이 하나도 없습니다.", "materials.slots")
        if mat["mode"] == "A" and len(slots) >= 2:
            self.err(E.MODE_A_MULTI_SLOT, "모드 A 는 슬롯 1개(단일 등가)만 허용합니다.", "materials.slots")
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            man = self.recipe_rows.get(tk, {}).get("manifest")
            used: set[str] = {la["slot"] for la in (t["geometry"].get("layers") or [])} | \
                {b["slot"] for b in (t["geometry"].get("body_slots") or [])}
            if mat["mode"] == "A" and len(used) >= 2:
                self.err(E.MODE_A_MULTI_SLOT, "모드 A 에서 형상 영역이 둘 이상의 슬롯에 매핑됩니다.",
                         self.field_prefix(i) + "geometry", test_key=tk)
            for u in sorted(used - names):
                self.err(E.SLOT_UNKNOWN, f"형상이 가리키는 슬롯 {u!r} 이 materials.slots 에 없습니다.",
                         self.field_prefix(i) + "geometry", test_key=tk, slot=u)
            if man is None:
                continue
            man_slots = {s["slot"] for s in man["material_slots"]}
            for j, s in enumerate(slots):
                if s["slot"] not in man_slots:
                    self.err(E.SLOT_UNKNOWN, f"슬롯 {s['slot']!r} 가 레시피 {t['recipe']} 의 material_slots 에 없습니다.",
                             f"materials.slots[{j}].slot", test_key=tk, slot=s["slot"])
            for m in sorted(man_slots - names):
                self.err(E.SLOT_UNASSIGNED, f"레시피 슬롯 {m!r} 에 대응하는 materials.slots 항목이 없습니다.",
                         "materials.slots", test_key=tk, slot=m)
        density_default = float(cfg.get("material.default_density_kg_m3"))
        lo, hi = cfg.get("material.default_E_range_pa")
        for j, s in enumerate(slots):
            f = f"materials.slots[{j}]"
            ref = s.get("reference")
            el = (ref or {}).get("elastic") or {}
            if s["role"] == "fixed" and (ref is None):
                self.err(E.FIXED_SLOT_REFERENCE_MISSING, "fixed 슬롯은 reference(기준 카드)가 필요합니다.", f"{f}.reference", slot=s["slot"])
                continue
            if el.get("poisson_ratio") is None:
                self.err(E.POISSON_RATIO_REQUIRED, "reference.elastic.poisson_ratio 는 식별하지 않는 입력값이라 필수입니다.",
                         f"{f}.reference.elastic.poisson_ratio", slot=s["slot"])
                continue
            if s["role"] == "fixed" and el.get("youngs_modulus") is None:
                self.err(E.FIXED_SLOT_REFERENCE_MISSING, "fixed 슬롯의 reference.elastic.youngs_modulus 가 필요합니다.",
                         f"{f}.reference.elastic.youngs_modulus", slot=s["slot"])
                continue
            density = el.get("density")
            if density is None:
                density = density_default
                self.warn(E.DENSITY_DEFAULTED, f"슬롯 {s['slot']!r} 의 density 가 없어 기본값 {density_default:g} kg/m^3 를 썼습니다.",
                          slot=s["slot"])
            young = el.get("youngs_modulus")
            if young is None:  # identify: 식별 대상이라 값이 의미 없다 -> 기본 범위의 로그 중앙을 자리표시값으로 쓴다
                young = math.sqrt(float(lo) * float(hi))
            blocks = {"elastic": {"youngs_modulus": float(young), "poisson_ratio": float(el["poisson_ratio"]),
                                  "density": float(density)},
                      "table": (ref or {}).get("table") if s["role"] == "fixed" else None,
                      "rate_table": (ref or {}).get("rate_table") if s["role"] == "fixed" else None}
            try:
                nm = neutral_from_card_blocks(blocks)
            except CardBlocksError as e:
                self.err(E.SCHEMA_INVALID, f"reference 형식 오류: {e}", f"{f}.reference", slot=s["slot"])
                continue
            for v in neutral_material_violations(nm):
                self.err(E.SCHEMA_INVALID, f"reference 가 재료 불변식을 위반합니다: {v}", f"{f}.reference", slot=s["slot"])
            self.references[s["slot"]] = nm
            self.ref_blocks[s["slot"]] = nm.to_card_blocks()
        # 재료 모델 인스턴스 (param_space·capabilities 용)
        for j, s in enumerate(slots):
            name = self.model_names.get(s["slot"])
            if name is None or s["slot"] not in self.references:
                continue
            try:
                model = self.reg.create("material_model", name, self.model_options[s["slot"]])
                model.bind(self.references[s["slot"]])
            except Exception as e:  # noqa: BLE001
                self.err(E.SCHEMA_INVALID, f"재료 모델 {name!r} 옵션/초기화 오류: {e}", f"materials.slots[{j}].options", slot=s["slot"])
                continue
            self.models[s["slot"]] = model
        # 모드 B: identify 슬롯의 소성 변수는 1차 미지원
        if mat["mode"] == "B":
            for j, s in enumerate(slots):
                model = self.models.get(s["slot"])
                if s["role"] != "identify" or model is None:
                    continue
                fixed = {k for k, p in (s.get("params") or {}).items() if p.get("fixed_value") is not None}
                free_plastic = [p.name for p in model.param_space().params if p.stage == "plastic" and p.name not in fixed]
                if free_plastic:
                    self.err(E.MODE_B_PLASTIC_UNSUPPORTED,
                             f"모드 B 의 소성 변수 식별은 1차 미지원입니다: {free_plastic} (fixed_value 로 고정하세요).",
                             f"materials.slots[{j}]", slot=s["slot"])

    # ================================================================ ⑤ 시험 데이터
    def _test_spec_keys(self, matnexus_key: str) -> list[str]:
        out = []
        for e in self.reg.entries("test_spec", enabled_only=True):
            try:
                cls = self.reg.plugin_class("test_spec", e.name)
            except Exception:  # noqa: BLE001
                continue
            if matnexus_key in tuple(getattr(cls, "matnexus_test_type_keys", ())):
                out.append(e.name)
        return out

    def _load_runs(self, i: int, t: Mapping[str, Any]) -> list[RunRecord] | None:
        tk = t["test_key"]
        pre = self.field_prefix(i)
        runs: list[RunRecord] = []
        ok = True
        if t.get("test_run_ids") is not None:
            client = self.ctx.matnexus
            for j, rid in enumerate(t["test_run_ids"]):
                f = f"{pre}test_run_ids[{j}]"
                if rid in self.run_cache:
                    runs.append(self.run_cache[rid])
                    continue
                if client is None:
                    raise UpstreamUnavailable("MatNexus 클라이언트가 설정되지 않았습니다.")
                try:
                    rec = client.fetch_run_record(rid)
                except RunNotFound:
                    self.err(E.RUN_NOT_FOUND, f"MatNexus 에서 시험 run 을 찾을 수 없습니다: {rid}", f, test_key=tk, run_id=rid)
                    ok = False
                    continue
                except RunUnreadable as e:
                    self.err(E.RUN_UNREADABLE, f"MatNexus run 을 읽을 수 없습니다: {e}", f, test_key=tk, run_id=rid)
                    ok = False
                    continue
                self.run_cache[rid] = rec
                runs.append(rec)
        else:
            for j, ir in enumerate(t["inline_runs"]):
                f = f"{pre}inline_runs[{j}]"
                try:
                    rec = normalize_inline_run(ir)
                except InlineRunError as e:
                    self.err(e.code, e.message, f + (f".{e.field}" if e.field else ""), test_key=tk, run_id=f"inline:{ir['run_key']}")
                    ok = False
                    continue
                prev = self.run_cache.get(rec.run_id)
                if prev is not None and prev.fetched_hash != rec.fetched_hash:
                    self.err(E.SCHEMA_INVALID, f"inline run_key {ir['run_key']!r} 가 다른 시험에서 다른 데이터로 쓰였습니다 (잡 안에서 고유해야 함).",
                             f, test_key=tk)
                    ok = False
                    continue
                self.run_cache[rec.run_id] = rec
                runs.append(rec)
        return runs if ok else None

    def step5(self) -> None:
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            spec = self.specs.get(tk)
            row = self.recipe_rows.get(tk)
            if spec is None or row is None:
                continue
            man = row["manifest"]
            pre = self.field_prefix(i)
            runs = self._load_runs(i, t)
            if runs is None:
                continue
            self.tests_runs[tk] = runs
            my_keys = tuple(getattr(self.reg.plugin_class("test_spec", spec.name), "matnexus_test_type_keys", ()))
            for r in runs:
                if r.test_type_key in my_keys:
                    continue
                if not self._test_spec_keys(r.test_type_key):
                    self.err(E.TEST_TYPE_UNMAPPED, f"MatNexus 시험 종류 {r.test_type_key!r} 에 대응하는 TestSpec 이 없습니다.",
                             pre + "test_run_ids", test_key=tk, run_id=r.run_id)
                else:
                    self.err(E.RUN_TEST_TYPE_MISMATCH,
                             f"run 의 시험 종류 {r.test_type_key!r} 가 요청 test_type {t['test_type']!r} 와 맞지 않습니다.",
                             pre + "test_run_ids", test_key=tk, run_id=r.run_id)
            dim_to_param = {p["matnexus_key"]: p for p in man["parameters"] if p.get("matnexus_key")}
            overrides = t.get("recipe_params") or {}
            geo_dims = t["geometry"].get("dimensions") or {}
            req_channels = spec.required_channels()
            req_dims = spec.required_dimensions()
            dc = t.get("displacement_channel", "auto")
            for r in runs:
                for group in req_channels:
                    if not any(c in r.channels for c in group):
                        self.err(E.CHANNEL_MISSING, f"필수 채널 없음: {' 또는 '.join(group)}", pre + "test_run_ids",
                                 test_key=tk, run_id=r.run_id)
                if dc in ("displacement", "deflection") and dc not in r.channels:
                    self.err(E.DISPLACEMENT_CHANNEL_UNAVAILABLE, f"지정한 변위 채널 {dc!r} 이 run 에 없습니다.",
                             pre + "displacement_channel", test_key=tk, run_id=r.run_id)
                eff = {**r.specimen_dimensions, **geo_dims}
                for dim in req_dims:
                    p = dim_to_param.get(dim)
                    has = dim in eff or (p is not None and p["name"] in overrides)
                    if not has:
                        self.err(E.SPECIMEN_DIMENSION_MISSING, f"필수 시편 치수 {dim!r} 가 없습니다 (MatNexus 시편 치수·geometry.dimensions·recipe_params).",
                                 pre + "test_run_ids", test_key=tk, run_id=r.run_id)
                # MatNexus 치수가 레시피 파라미터 범위 밖
                for dim, p in dim_to_param.items():
                    if p["name"] in overrides or dim not in r.specimen_dimensions or dim in geo_dims:
                        continue
                    lo, hi = p["range"]
                    if not lo <= r.specimen_dimensions[dim] <= hi:
                        self.err(E.RECIPE_PARAM_OUT_OF_RANGE,
                                 f"시편 치수 {dim}={r.specimen_dimensions[dim]:g} 가 레시피 파라미터 {p['name']} 범위 [{lo}, {hi}] 밖입니다.",
                                 pre + "test_run_ids", test_key=tk, run_id=r.run_id, param=p["name"])
            # 컴플라이언스 경고 (api.md §4.7 예시 COMPLIANCE_MISSING)
            comp = man.get("compliance") or {}
            has_disp = any("displacement" in r.channels for r in runs)
            if dc != "deflection" and has_disp:
                if comp.get("value_m_per_n") is None:
                    self.warn(E.COMPLIANCE_MISSING, f"레시피 {t['recipe']} 에 컴플라이언스 C 가 없어 보정 없이 실행합니다 (E 신뢰도 낮음).", test_key=tk)
                elif comp.get("instruments"):
                    bad = sorted({r.instrument or "" for r in runs if r.instrument not in comp["instruments"]})
                    if bad:
                        self.warn(E.COMPLIANCE_INSTRUMENT_MISMATCH,
                                  f"run 의 장비 {bad} 가 컴플라이언스 유효 장비 {comp['instruments']} 에 없습니다.", test_key=tk)
            if row["warnings_count"]:
                self.warn(E.RECIPE_WARNINGS, f"레시피 {t['recipe']} 는 등록 시 경고 {row['warnings_count']}건이 있습니다.", test_key=tk)
            # 플러그인 검증 (TestSpec.validate)
            test_input = {k: v for k, v in t.items()}
            try:
                self.add_issues(spec.validate(test_input, runs, man), tk)
            except Exception as e:  # noqa: BLE001
                self.err(E.SCHEMA_INVALID, f"TestSpec 검증 중 오류: {e}", pre.rstrip(".") or None, test_key=tk)
        self._dedupe_details()

    def _dedupe_details(self) -> None:
        seen: set[tuple[Any, ...]] = set()
        out = []
        for d in self.details:
            k = (d["code"], d.get("field"), d.get("test_key"), d.get("run_id"), d.get("slot"), d.get("param"))
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
        self.details = out

    # ================================================================ ⑥ 곡선 품질 (점 수)
    def step6(self) -> None:
        min_pts = int(self.cfg.get("alignment.min_raw_points"))
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            for r in self.tests_runs.get(tk, []):
                n = len(r.channels.get("force", ()))
                if n < min_pts:
                    self.err(E.CURVE_TOO_FEW_POINTS, f"곡선 점 수 {n} 가 최소 {min_pts} 보다 적습니다.", self.field_prefix(i) + "test_run_ids",
                             test_key=tk, run_id=r.run_id)

    # ================================================================ ⑦ 형상
    def step7(self) -> None:
        cfg = self.cfg
        tol = float(cfg.get("geometry.layer_sum_rel_tol"))
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            spec = self.specs[tk]
            man = self.recipe_rows[tk]["manifest"]
            g = t["geometry"]
            f = self.field_prefix(i) + "geometry"
            kind = g["kind"]
            allowed = tuple(spec.allowed_geometries())
            if kind not in allowed or kind not in man["specimen"]["geometry_kinds"]:
                self.err(E.GEOMETRY_KIND_NOT_ALLOWED,
                         f"형상 종류 {kind!r} 는 허용되지 않습니다 (TestSpec {list(allowed)}, 레시피 {man['specimen']['geometry_kinds']}).",
                         f + ".kind", test_key=tk)
                continue
            for k, v in (g.get("dimensions") or {}).items():
                if not v > 0:
                    self.err(E.GEOMETRY_DIMENSION_INVALID, f"치수 {k}={v} 는 0 보다 커야 합니다.", f"{f}.dimensions.{k}", test_key=tk)
            slot_names = {s["slot"] for s in man["material_slots"]}
            layers = g.get("layers") or []
            if kind == "layered_box":
                for k, la in enumerate(layers):
                    if not la["thickness"] > 0:
                        self.err(E.GEOMETRY_DIMENSION_INVALID, f"layers[{k}].thickness 는 0 보다 커야 합니다.", f"{f}.layers[{k}].thickness", test_key=tk)
                    if la["slot"] not in slot_names:
                        self.err(E.SLOT_UNKNOWN, f"layers[{k}].slot {la['slot']!r} 가 레시피에 없습니다.", f"{f}.layers[{k}].slot",
                                 test_key=tk, slot=la["slot"])
                total_t = (g.get("dimensions") or {}).get("thickness")
                if total_t is None:
                    vals = [r.specimen_dimensions.get("thickness") for r in self.tests_runs.get(tk, [])]
                    total_t = next((v for v in vals if v is not None), None)
                s_sum = sum(la["thickness"] for la in layers)
                if total_t is not None and abs(s_sum - total_t) > tol * max(abs(total_t), 1e-30):
                    self.err(E.GEOMETRY_DIMENSION_INVALID,
                             f"layers 두께 합 {s_sum:g} 가 thickness {total_t:g} 와 상대오차 {tol:g} 를 넘게 다릅니다.", f"{f}.layers", test_key=tk)
            if kind == "stp":
                rec = self.ctx.files.get(g["file_id"]) if g.get("file_id") else None
                if rec is None:
                    self.err(E.FILE_NOT_FOUND, f"입력 파일을 찾을 수 없습니다: {g.get('file_id')}", f + ".file_id", test_key=tk)
                    continue
                for k, b in enumerate(g.get("body_slots") or []):
                    if b["slot"] not in slot_names:
                        self.err(E.SLOT_UNKNOWN, f"body_slots[{k}].slot {b['slot']!r} 가 레시피에 없습니다.", f"{f}.body_slots[{k}].slot",
                                 test_key=tk, slot=b["slot"])
            if kind == "stp" and not self.ctx.files.get(g.get("file_id", "")):
                continue
            # 메셔 검증 (치수 필수 키, STP 임포트·바디 수 등)
            mesher_name = kind
            if mesher_name not in self.reg.names("geometry_mesher"):
                self.err(E.GEOMETRY_KIND_NOT_ALLOWED, f"형상 {kind!r} 를 만드는 메셔가 등록되어 있지 않습니다.", f + ".kind", test_key=tk)
                continue
            try:
                mesher = self.reg.create("geometry_mesher", mesher_name, {})
                gdict = self._snapshot_geometry(i, t)
                if kind == "stp":  # 서버 경로는 스냅샷이 아니라 메셔 검증에만 쓴다
                    gdict["stp_path"] = str(self.ctx.files.path(self.ctx.files.get(g["file_id"])))  # type: ignore[arg-type]
                gspec = geometry_spec_from_dict(gdict)
                self.add_issues(mesher.validate(gspec), tk)
            except Exception as e:  # noqa: BLE001
                self.err(E.GEOMETRY_DIMENSION_INVALID, f"형상 검증 중 오류: {e}", f, test_key=tk)
        self._dedupe_details()

    # ================================================================ ⑧ 파라미터·예산
    def step8(self) -> None:
        cfg = self.cfg
        for j, s in enumerate(self.req["materials"]["slots"]):
            model = self.models.get(s["slot"])
            if model is None:
                continue
            space = {p.name: p for p in model.param_space().params}
            for name, ov in (s.get("params") or {}).items():
                f = f"materials.slots[{j}].params.{name}"
                spec = space.get(name)
                if spec is None:
                    self.err(E.PARAM_UNKNOWN, f"재료 모델 {self.model_names[s['slot']]!r} 에 없는 변수: {name!r} (허용: {sorted(space)})",
                             f, slot=s["slot"], param=name)
                    continue
                lo, hi = ov.get("lower", spec.lower), ov.get("upper", spec.upper)
                init = ov.get("initial", spec.initial)
                fixed = ov.get("fixed_value")
                positive = spec.scale == "log" or spec.lower > 0
                bad: list[str] = []
                if lo >= hi:
                    bad.append(f"lower({lo:g}) >= upper({hi:g})")
                if init is not None and not (lo <= init <= hi):
                    bad.append(f"initial {init:g} 가 [{lo:g}, {hi:g}] 밖")
                if positive:
                    for label, v in (("lower", ov.get("lower")), ("upper", ov.get("upper")), ("initial", ov.get("initial")),
                                     ("fixed_value", fixed)):
                        if v is not None and not v > 0:
                            bad.append(f"{label}={v:g} 는 0 보다 커야 하는 물리 변수 ({name})")
                if fixed is not None and not (lo <= fixed <= hi) and ("lower" not in ov and "upper" not in ov):
                    pass  # 고정값은 범위 밖일 수 있다(범위는 θ 에서 빠지므로 무관)
                for b in bad:
                    self.err(E.PARAM_RANGE_INVALID, f"변수 {name}: {b}", f, slot=s["slot"], param=name)
        budget = self.req.get("budget") or {}
        limit = int(cfg.get("budget.max_evaluations_limit"))
        if budget.get("max_evaluations", 0) > limit:
            self.err(E.BUDGET_EXCEEDS_LIMIT, f"max_evaluations {budget['max_evaluations']} 가 상한 {limit} 를 넘습니다.", "budget.max_evaluations")

    # ================================================================ ⑨ 조합
    def step9(self) -> None:
        solver_name, solver_opts = self.names["solver"]
        adapter = None
        try:
            adapter = self.reg.create("solver", solver_name, solver_opts)
            caps = adapter.capabilities()
        except UnknownPluginError:
            adapter = caps = None
        except Exception as e:  # noqa: BLE001
            self.err(E.SOLVER_UNKNOWN, f"솔버 어댑터 {solver_name!r} 를 초기화할 수 없습니다: {e}", "solver")
            adapter = caps = None
        for j, s in enumerate(self.req["materials"]["slots"]):
            model = self.models.get(s["slot"])
            if model is None:
                continue
            if solver_name not in model.capabilities().solvers:
                self.err(E.MATERIAL_SOLVER_INCOMPATIBLE,
                         f"재료 모델 {self.model_names[s['slot']]!r} 는 솔버 {solver_name!r} 를 지원하지 않습니다.",
                         f"materials.slots[{j}].material_model", slot=s["slot"])
        if caps is not None:
            for i, t in enumerate(self.req["tests"]):
                tk = t["test_key"]
                an = self.recipe_rows[tk]["manifest"]["solver"]["analysis"]
                need = {an["primary"]} | ({an["fallback"]} if an["fallback"] != "none" else set())
                miss = sorted(need - set(caps.analysis_modes))
                if miss:
                    self.err(E.SOLVER_ANALYSIS_UNSUPPORTED, f"솔버 {solver_name!r} 가 레시피의 해석 모드 {miss} 를 지원하지 않습니다.",
                             self.field_prefix(i) + "recipe", test_key=tk)
                min_v = self.recipe_rows[tk]["manifest"]["solver"].get("min_version")
                if min_v and adapter is not None:
                    try:
                        have = adapter.solver_version()
                    except Exception:  # noqa: BLE001
                        have = None
                    if have and _version_lt(have, min_v):
                        self.err(E.SOLVER_ANALYSIS_UNSUPPORTED, f"솔버 버전 {have!r} 이 레시피 최소 버전 {min_v!r} 보다 낮습니다.",
                                 self.field_prefix(i) + "recipe", test_key=tk)
        fr = self.req.get("friction")
        if fr is not None:
            if fr["mode"] == "identify":
                self.err(E.FRICTION_IDENTIFY_UNSUPPORTED, "1차(굽힘)에서는 friction.mode=identify 를 지원하지 않습니다.", "friction.mode")
            elif fr.get("value") is not None:
                for i, t in enumerate(self.req["tests"]):
                    if "fric" not in {p["name"] for p in self.recipe_rows[t["test_key"]]["manifest"]["parameters"]}:
                        self.err(E.RECIPE_PARAM_UNKNOWN, "friction.value 를 받을 레시피 파라미터 fric 이 없습니다.", "friction.value",
                                 test_key=t["test_key"], param="fric")
        self.step9_explicit(caps)

    def step9_explicit(self, caps: Any) -> None:
        cfg = self.cfg
        ec = self.req.get("explicit_check")
        by_request = ec is not None and ec["enabled"]
        enabled = ec["enabled"] if ec is not None else bool(cfg.get("verify.explicit_check.enabled"))
        resolved: dict[str, Any] = {"enabled": False}
        if not enabled:
            self.explicit = resolved
            return
        targets = (ec or {}).get("tests") or [t["test_key"] for t in self.req["tests"]]
        section = (ec or {}).get("section") or {}
        active: list[str] = []
        for tk in targets:
            row = self.recipe_rows[tk]
            man = row["manifest"]
            reason = None
            if not man["deck"]["controls"].get("explicit"):
                reason = "레시피에 explicit 제어 파일이 없습니다."
            elif _has_mass_scaling(row):
                reason = "레시피 검증에서 질량 스케일링(WARN_MASS_SCALING)이 기록되었습니다."
            elif caps is not None and not caps.supports_overrides:
                reason = "솔버 어댑터가 실행 덮어쓰기(RunOverrides)를 지원하지 않습니다."
            if reason:
                if by_request:
                    self.err(E.EXPLICIT_CHECK_UNAVAILABLE, f"테스트 {tk}: {reason}", "explicit_check", test_key=tk)
                else:
                    self.warn(E.EXPLICIT_CHECK_SKIPPED, f"설정 기본값 explicit 재현 검사를 건너뜁니다: {reason}", test_key=tk)
                continue
            topo = man["specimen"]["element"]["topology"]
            bad = None
            for slot_name in {s["slot"] for s in man["material_slots"]}:
                ov = {**{k: v for k, v in section.items() if k != "slots"}, **((section.get("slots") or {}).get(slot_name) or {})}
                elform = ov.get("elform", cfg.get("verify.explicit_check.elform"))
                if topo in ELFORMS and elform not in ELFORMS[topo]:
                    bad = f"elform {elform} 는 요소 {topo} 와 맞지 않습니다 (허용 {sorted(ELFORMS[topo])})."
            if bad:
                if by_request and section:
                    self.err(E.EXPLICIT_CHECK_SECTION_INVALID, f"테스트 {tk}: {bad}", "explicit_check.section", test_key=tk)
                elif by_request:
                    self.warn(E.EXPLICIT_CHECK_SKIPPED, f"설정 기본 섹션이 시편 topology 와 맞지 않아 건너뜁니다: {bad}", test_key=tk)
                else:
                    self.warn(E.EXPLICIT_CHECK_SKIPPED, f"설정 기본값 explicit 재현 검사를 건너뜁니다: {bad}", test_key=tk)
                continue
            active.append(tk)
        for sname in (section.get("slots") or {}):
            if sname not in {s["slot"] for s in self.req["materials"]["slots"]}:
                self.err(E.SLOT_UNKNOWN, f"explicit_check.section.slots 의 슬롯 {sname!r} 가 materials.slots 에 없습니다.",
                         "explicit_check.section.slots", slot=sname)
        resolved = {
            "enabled": bool(active), "requested": bool(by_request), "tests": active,
            "section": section,
            "loading_velocity_m_s": (ec or {}).get("loading_velocity", cfg.get("verify.explicit_check.loading_velocity_m_s")),
            "max_ke_ratio": (ec or {}).get("max_ke_ratio", cfg.get("verify.explicit_check.max_ke_ratio")),
        }
        self.explicit = resolved

    # ================================================================ 스냅샷
    def _snapshot_geometry(self, i: int, t: Mapping[str, Any]) -> dict[str, Any]:
        tk = t["test_key"]
        g = t["geometry"]
        man = self.recipe_rows[tk]["manifest"]
        slot_part = {s["slot"]: s["part_id"] for s in man["material_slots"]}
        regions_of = {s["slot"]: s["regions"] for s in man["material_slots"]}
        kind = g["kind"]
        first = (self.tests_runs.get(tk) or [None])[0]
        dims = dict(first.specimen_dimensions) if first is not None else {}
        dims.update(g.get("dimensions") or {})
        layers = [[f"layer:{k}", la["thickness"]] for k, la in enumerate(g.get("layers") or [])]
        region_slot: dict[str, str] = {}
        if kind == "layered_box":
            for k, la in enumerate(g.get("layers") or []):
                region_slot[f"layer:{k}"] = la["slot"]
        elif kind == "stp" and g.get("body_slots"):
            for b in g["body_slots"]:
                region_slot[f"body:{b['body']}"] = b["slot"]
        else:
            for sname, regs in regions_of.items():
                if "specimen" in regs or "*" in regs:
                    region_slot["specimen"] = sname
                    break
        refs = []
        spec = self.specs.get(tk)
        if spec is not None:
            for r in spec.reference_points(man):
                refs.append({"name": r.name, "locate": dict(r.locate)})
        return {
            "kind": kind, "dimensions": dims, "dimension_overrides": dict(g.get("dimensions") or {}),
            "layers": layers, "stack_axis": g.get("stack_axis") or "z", "file_id": g.get("file_id"),
            "placement": g.get("placement") or {}, "body_slots": g.get("body_slots") or [],
            "region_slots": region_slot,
            "region_part_ids": {r: slot_part[s] for r, s in region_slot.items() if s in slot_part},
            "reference_points": refs,
        }

    def build_snapshot(self) -> dict[str, Any]:
        cfg = self.cfg
        budget_in = self.req.get("budget") or {}
        budget = {
            "max_evaluations": int(budget_in.get("max_evaluations", cfg.get("budget.default_max_evaluations"))),
            "max_wall_time_s": float(budget_in.get("max_wall_time_s", cfg.get("budget.default_max_wall_time_s"))),
            "run_timeout_s": float(budget_in.get("run_timeout_s", cfg.get("solver.run_timeout_s"))),
        }
        seed = self.req.get("seed")
        seed = int(cfg.get("engine.default_seed")) if seed is None else int(seed)
        friction = dict(self.req.get("friction") or {"mode": "fixed"})
        tests = []
        for i, t in enumerate(self.req["tests"]):
            tk = t["test_key"]
            row = self.recipe_rows[tk]
            tests.append({
                "test_key": tk, "test_type": t["test_type"], "recipe_ref": t["recipe"], "recipe_content_hash": row["content_hash"],
                "geometry": self._snapshot_geometry(i, t),
                "run_ids": [r.run_id for r in self.tests_runs[tk]],
                "displacement_channel": t.get("displacement_channel", "auto"),
                "recipe_params_si": dict(t.get("recipe_params") or {}),
                "user_window": dict(t.get("user_window") or {}),
                "weight": float(t.get("weight", 1.0)),
            })
        slots = []
        for s in self.req["materials"]["slots"]:
            slots.append({
                "slot": s["slot"], "role": s["role"], "material_model": self.model_names[s["slot"]],
                "options": self.model_options[s["slot"]], "params": s.get("params") or {},
                "member_position": s.get("member_position"), "reference": self.ref_blocks.get(s["slot"]),
            })
        return {
            "schema_version": SCHEMA_VERSION, "tests": tests, "materials": {"mode": self.req["materials"]["mode"], "slots": slots},
            "plasticity_mode": self.plasticity_mode,
            "optimizer": {"name": self.names["optimizer"][0], "options": self.names["optimizer"][1]},
            "elastic_optimizer": {"name": self.names["elastic_optimizer"][0], "options": self.names["elastic_optimizer"][1]},
            "solver": {"name": self.names["solver"][0], "options": self.names["solver"][1]},
            "friction": friction, "budget": budget, "seed": seed, "explicit_check": self.explicit,
            "request_hash": "",  # 호출자가 채운다
        }


def _version_lt(have: str, need: str) -> bool:
    def nums(s: str) -> tuple[int, ...]:
        m = re.search(r"R?(\d+(?:\.\d+)*)", s)
        return tuple(int(p) for p in m.group(1).split(".")) if m else ()

    a, b = nums(have), nums(need)
    return bool(a and b and a < b)


def _has_mass_scaling(row: Mapping[str, Any]) -> bool:
    v = row.get("verification") or {}
    return any(c.get("code") == "WARN_MASS_SCALING" and c.get("result") == "warn" for c in v.get("checks", []))


def validate_request(body: Any, ctx: ValidationContext, job_id: str) -> ValidatedRequest:
    """요청 본문(JSON 파싱 후)을 검증하고 정규화한다. 실패는 ValidationFailed, MatNexus 불통은 UpstreamUnavailable."""
    v = _Validator(ctx, job_id)
    if not v.step1(body):
        raise ValidationFailed(sort_details(v.details))
    v.step2()
    v.step3()
    v.step4()
    v.step5()
    if not v.has_error_in([5]):
        if not v.has_error_in([1, 2, 3, 4]):
            v.step6()
            v.step7()
            v.step8()
            v.step9()
    if v.details:
        raise ValidationFailed(sort_details(v.details))
    snapshot = v.build_snapshot()
    request = dict(v.req)
    request.update({
        "plasticity_mode": v.plasticity_mode, "optimizer": snapshot["optimizer"], "elastic_optimizer": snapshot["elastic_optimizer"],
        "solver": snapshot["solver"], "budget": snapshot["budget"], "seed": snapshot["seed"], "friction": snapshot["friction"],
    })
    request_hash = sha256_json(request)
    snapshot["request_hash"] = request_hash
    runs = {r.run_id: r for rs in v.tests_runs.values() for r in rs}
    stp: dict[str, Path] = {}
    for t in v.req["tests"]:
        fid = t["geometry"].get("file_id")
        if fid:
            stp[fid] = ctx.files.path(ctx.files.get(fid))  # type: ignore[arg-type]
    spec = build_spec(snapshot, job_id=job_id, runs=runs, recipe_resolver=_resolver(ctx), stp_paths=stp)
    # ⑥ 곡선 정렬 1–5단계·⑩ 식별성: 엔진 preflight (①–⑨ 통과 후)
    if ctx.hooks.preflight is not None:
        svc = PreflightServices(ctx.registry, ctx.config, job_id, ctx.config.path("paths.data_dir") / "tmp")
        try:
            issues = list(ctx.hooks.preflight(spec, svc))
        finally:
            svc.cleanup()
        v.add_issues(issues)
        if v.details:
            raise ValidationFailed(sort_details(v.details))
    tests_info = [{"test_key": t["test_key"], "test_type": t["test_type"], "recipe_ref": t["recipe_ref"]} for t in snapshot["tests"]]
    material_id = (v.req.get("material_ref") or {}).get("matnexus_material_id")
    if material_id is None:
        material_id = next((r.material_id for r in runs.values() if r.material_id), None)
    return ValidatedRequest(job_id=job_id, request=request, request_hash=request_hash, snapshot=snapshot, spec=spec, runs=runs,
                            stp_files=stp, warnings=v.warnings, material_id=material_id, tests=tests_info)


def _resolver(ctx: ValidationContext):
    def resolve(recipe_ref: str) -> ResolvedRecipe:
        rid, _, ver = recipe_ref.partition("@")
        row = ctx.recipes.get_best(rid, int(ver))
        if row is None:
            raise MatObserverError(f"recipe {recipe_ref} not found", code=E.RECIPE_NOT_FOUND)
        return ctx.recipes.resolved(row)

    return resolve


def run_json_files(runs: Mapping[str, RunRecord]) -> dict[str, dict[str, Any]]:
    """inputs/runs/{run_id}.json 내용 (run_id 의 `:` 는 파일 이름에서 `_` 로)."""
    return {rid.replace(":", "_").replace("/", "_") + ".json": run_to_json(r) for rid, r in runs.items()}
