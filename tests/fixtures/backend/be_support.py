"""Backend 테스트 공용 도구 (소유: Impl-Backend).

`--import-mode=importlib` 라 conftest 를 import 할 수 없으므로, 각 테스트 폴더의 conftest 가 이 모듈을 sys.path 에 올려
`from be_support import *` 로 픽스처를 노출한다.

* 실제 레지스트리(config/plugins.d)와 실제 TestSpec·재료·메셔 플러그인을 쓰고, 엔진(`run_calibration`·`preflight`)과
  레시피 검증기(`verify_recipe`)만 대역으로 주입한다. MatNexus 는 `httpx.MockTransport` 로 기록 응답을 돌려준다(외부 호출 없음).
* 모든 경로는 tmp_path 아래다.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient

from matobserver.api.app import create_app
from matobserver.core.config import Config
from matobserver.core.registry import Registry
from matobserver.jobs.worker import Worker
from matobserver.matnexus.client import MatNexusClient
from matobserver.recipes.repository import unlock_tree
from matobserver.validation.hooks import EngineHooks

FIX = Path(__file__).resolve().parent
SHARED = FIX.parent / "shared"
REPO_ROOT = FIX.parents[2]
PLUGINS_DIR = REPO_ROOT / "config" / "plugins.d"
RECIPE_DIR = FIX / "recipes" / "bending3p-param"

SVC_TOKEN = "svc-token-for-tests"
ADMIN_TOKEN = "admin-token-for-tests"
MN_BASE = "http://matnexus.test"

RUN_ID = "6f1c2a3b-0000-4000-8000-000000000001"
RUN_ID_BAD = "6f1c2a3b-0000-4000-8000-0000000000ff"
RUN_ID_MISSING = "6f1c2a3b-0000-4000-8000-0000000000aa"
SPECIMEN_ID = "5a5a5a5a-0000-4000-8000-000000000001"
MATERIAL_ID = "3c3c3c3c-0000-4000-8000-000000000001"
RECIPE_REF = "bending3p-param@1"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_json(*parts: str) -> Any:
    return json.loads(FIX.joinpath(*parts).read_text(encoding="utf-8"))


def inline_run(run_key: str = "spec01") -> dict[str, Any]:
    r = json.loads((SHARED / "flexural_inline_run.json").read_text(encoding="utf-8"))
    r["run_key"] = run_key
    return r


# ---------------------------------------------------------------------- 설정
def make_config(root: Path, **over: Any) -> Config:
    flat: dict[str, Any] = {
        "paths.data_dir": str(root / "data"),
        "paths.work_dir": str(root / "data" / "work"),
        "jobstore.sqlite_path": str(root / "data" / "jobs.sqlite"),
        "recipes.store_dir": str(root / "data" / "recipes"),
        "auth.service_tokens": [{"name": "matnexus-test", "sha256": sha256_hex(SVC_TOKEN)}],
        "auth.admin_tokens": [{"name": "admin-test", "sha256": sha256_hex(ADMIN_TOKEN)}],
        "matnexus.base_url": MN_BASE,
        "matnexus.token": "mn-test-token",
        "matnexus.timeout_s": 2.0,
        "registry.paths": [str(PLUGINS_DIR)],
        "worker.cancel_poll_s": 0,
        "cli.poll_interval_s": 0.01,
    }
    flat.update(over)
    return Config(flat)


# ---------------------------------------------------------------------- MatNexus 대역
class FakeMatNexus:
    """기록 응답을 돌려주는 MatNexus 대역. `calls` 에 (method, path) 를 쌓는다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.mode: str | None = None  # None | "5xx" | "timeout" | "401"
        self.runs: dict[str, dict[str, Any]] = {RUN_ID: load_json("matnexus", "run_flexural.json"),
                                                RUN_ID_BAD: load_json("matnexus", "run_unparsed.json")}
        self.curves: dict[tuple[str, str], dict[str, Any]] = {
            ("displacement", "force"): load_json("matnexus", "curve_displacement_force.json"),
            ("time", "force"): load_json("matnexus", "curve_time_force.json"),
        }
        self.dimensions: dict[str, dict[str, Any]] = {SPECIMEN_ID: load_json("matnexus", "dimensions_flexural.json")}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if self.mode == "5xx":
            return httpx.Response(503, json={"detail": "unavailable"})
        if self.mode == "401":
            return httpx.Response(401, json={"detail": "unauthorized"})
        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "test-runs"] and len(parts) == 3:
            run = self.runs.get(parts[2])
            return httpx.Response(200, json=run) if run else httpx.Response(404, json={"detail": "not found"})
        if parts[:2] == ["api", "test-runs"] and len(parts) == 4 and parts[3] == "curve":
            if parts[2] not in self.runs:
                return httpx.Response(404, json={"detail": "not found"})
            key = (request.url.params.get("x"), request.url.params.get("y"))
            body = self.curves.get(key)
            return httpx.Response(200, json=body) if body else httpx.Response(422, json={"detail": "unknown channel"})
        if parts[:2] == ["api", "specimens"] and len(parts) == 4 and parts[3] == "dimensions":
            body = self.dimensions.get(parts[2])
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(404, json={"detail": "no route"})

    def client(self, config: Config) -> MatNexusClient:
        return MatNexusClient.from_config(config, transport=httpx.MockTransport(self.handler))


# ---------------------------------------------------------------------- 엔진 대역
def passing_verification(recipe: Any, services: Any, strict: bool) -> dict[str, Any]:
    return {"status": "passed", "checks": [{"code": "CHK_DEFAULT_RUN", "result": "pass"}], "artifacts": []}


def rejecting_verification(recipe: Any, services: Any, strict: bool) -> dict[str, Any]:
    return {"status": "failed", "checks": [{"code": "CHK_DEFAULT_RUN", "result": "fail", "detail": "합성 실패"}], "artifacts": []}


def fake_result(job_id: str = "") -> dict[str, Any]:
    return {
        "best_J": 0.01,
        "materials": [{"slot": "body", "card_blocks": {"elastic": {"youngs_modulus": 7.0e10, "poisson_ratio": 0.3, "density": 2700.0}}}],
        "warnings": [],
    }


def fake_run_calibration(spec: Any, services: Any) -> dict[str, Any]:
    """평가 3건과 artifact 1건을 남기고 성공한다."""
    services.progress("preprocess")
    services.progress("elastic", stage_iter=0, max_evaluations=spec.budget["max_evaluations"])
    for i in range(3):
        services.record_eval({"eval_index": i, "stage": "elastic" if i < 2 else "plastic", "theta": {"body.E": 7.0e10 + i},
                              "J": 1.0 / (i + 1), "status": "ok", "wall_time_s": 0.1, "is_best": True})
    out = services.workdir("artifacts") / "history.csv"
    out.write_text("eval_index,J\n0,1.0\n1,0.5\n2,0.33\n", encoding="utf-8")
    services.add_artifact(out, "history")
    return fake_result()


def make_hooks(run: Callable[..., Any] | None = fake_run_calibration, preflight: Callable[..., Any] | None = None,
               verify: Callable[..., Any] | None = passing_verification) -> EngineHooks:
    return EngineHooks(run_calibration=run, preflight=preflight, verify_recipe=verify)


# ---------------------------------------------------------------------- 레시피 번들
def folder_zip(folder: Path, mutate: Callable[[dict[str, bytes]], None] | None = None) -> bytes:
    files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}
    if mutate:
        mutate(files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buf.getvalue()


def recipe_zip(*, version: int | None = None, recipe_id: str | None = None, edit_manifest: Callable[[str], str] | None = None,
               extra: Mapping[str, bytes] | None = None, drop: tuple[str, ...] = ()) -> bytes:
    def mutate(files: dict[str, bytes]) -> None:
        text = files["manifest.yaml"].decode()
        if version is not None:
            text = text.replace("version: 1\n", f"version: {version}\n", 1)
        if recipe_id is not None:
            text = text.replace("recipe_id: bending3p-param", f"recipe_id: {recipe_id}", 1)
        if edit_manifest is not None:
            text = edit_manifest(text)
        files["manifest.yaml"] = text.encode()
        for d in drop:
            files.pop(d, None)
        for k, v in (extra or {}).items():
            files[k] = v

    return folder_zip(RECIPE_DIR, mutate)


# ---------------------------------------------------------------------- 가짜 플러그인 (레지스트리 확장용)
from matobserver.materials.mat001 import Mat001  # noqa: E402
from matobserver.solvers.surrogate.adapter import SurrogateBeamAdapter  # noqa: E402
from matobserver.testspecs.bending import BendingTestSpec  # noqa: E402
import dataclasses  # noqa: E402


class ImplicitOnlySolver(SurrogateBeamAdapter):
    """implicit 만, 덮어쓰기 미지원인 솔버 대역 (SOLVER_ANALYSIS_UNSUPPORTED·EXPLICIT_CHECK_UNAVAILABLE 시험용)."""

    name = "implicit_only"

    def capabilities(self):
        return dataclasses.replace(super().capabilities(), analysis_modes=frozenset({"implicit"}), supports_overrides=False)


class LsdynaOnlyMat(Mat001):
    """lsdyna 만 지원하는 재료 모델 대역 (MATERIAL_SOLVER_INCOMPATIBLE 시험용)."""

    name = "lsdyna_only_mat"

    def capabilities(self):
        return dataclasses.replace(super().capabilities(), solvers=frozenset({"lsdyna"}))


class FakeCompressionSpec(BendingTestSpec):
    """MatNexus `compressive` 를 맡는 두 번째 TestSpec 대역 (RUN_TEST_TYPE_MISMATCH·RECIPE_TEST_TYPE_MISMATCH 시험용)."""

    name = "compression"
    matnexus_test_type_keys = ("compressive",)


EXTRA_PLUGINS_YAML = """\
schema_version: "1.0"
plugins:
  - {type: solver, name: implicit_only, class: "be_support:ImplicitOnlySolver", version: "1.0.0", enabled: true}
  - {type: material_model, name: lsdyna_only_mat, class: "be_support:LsdynaOnlyMat", version: "1.0.0", enabled: true}
  - {type: test_spec, name: compression, class: "be_support:FakeCompressionSpec", version: "1.0.0", enabled: true}
"""


def extra_plugins_dir(root: Path) -> Path:
    d = root / "extra_plugins.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / "extra.yaml").write_text(EXTRA_PLUGINS_YAML, encoding="utf-8")
    return d


# ---------------------------------------------------------------------- 환경
@dataclass
class Env:
    root: Path
    config: Config
    registry: Registry
    mn: FakeMatNexus
    hooks: EngineHooks
    app: Any
    client: TestClient
    svc: dict[str, str] = field(default_factory=lambda: {"Authorization": f"Bearer {SVC_TOKEN}"})
    admin: dict[str, str] = field(default_factory=lambda: {"Authorization": f"Bearer {ADMIN_TOKEN}"})

    @property
    def ctx(self) -> Any:
        return self.app.state.ctx

    def worker(self, hooks: EngineHooks | None = None) -> Worker:
        c = self.ctx
        return Worker(config=self.config, registry=self.registry, store=c.store, recipes=c.recipes, artifacts=c.artifacts,
                      hooks=hooks or self.hooks, clock=c.clock)

    # --- 레시피
    def add_recipe(self, data: bytes | None = None, *, strict: bool = False, headers: Mapping[str, str] | None = None) -> httpx.Response:
        data = data if data is not None else recipe_zip()
        return self.client.post("/v1/admin/recipes", files={"bundle": ("recipe.zip", data, "application/zip")},
                                data={"strict": "true" if strict else "false"}, headers=dict(headers or self.admin))

    def activate_recipe(self, data: bytes | None = None) -> str:
        """등록 -> 워커가 (대역) 검증 -> active. recipe_ref 를 돌려준다."""
        r = self.add_recipe(data)
        assert r.status_code in (200, 202), r.text
        while self.worker().run_once():
            pass
        return r.json()["recipe_ref"]

    # --- 잡
    def post_job(self, body: Mapping[str, Any], headers: Mapping[str, str] | None = None, **kw: Any) -> httpx.Response:
        h = dict(self.svc)
        h.update(headers or {})
        return self.client.post("/v1/calibrations", json=body, headers=h, **kw)

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(path, headers=self.svc, **kw)


def calibration_body(**over: Any) -> dict[str, Any]:
    """검증을 통과하는 단일 시험 약식 요청 (inline run, 모드 A, mat024 계수형)."""
    body: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "시험 잡",
        "test_type": "bending",
        "recipe": RECIPE_REF,
        "geometry": {"kind": "box"},
        "inline_runs": [inline_run()],
        "materials": {"mode": "A", "slots": [{
            "slot": "body", "role": "identify", "material_model": "mat024",
            "reference": {"elastic": {"poisson_ratio": 0.33, "density": 2700.0}},
        }]},
        "budget": {"max_evaluations": 20},
    }
    body.update(over)
    return body


def mn_body(**over: Any) -> dict[str, Any]:
    """MatNexus run ID 로 가져오는 요청."""
    b = calibration_body(test_run_ids=[RUN_ID])
    b.pop("inline_runs")
    b.update(over)
    return b


def build_env(tmp_path: Path, *, hooks: EngineHooks | None = None, config_over: Mapping[str, Any] | None = None,
              mn: FakeMatNexus | None = None, clock: Any = None, extra_plugins: bool = False) -> Env:
    over = dict(config_over or {})
    if extra_plugins:
        over["registry.paths"] = [str(PLUGINS_DIR), str(extra_plugins_dir(tmp_path))]
    config = make_config(tmp_path, **over)
    registry = Registry.from_config(config)
    mn = mn or FakeMatNexus()
    hooks = hooks if hooks is not None else make_hooks()
    kw: dict[str, Any] = {}
    if clock is not None:
        kw["clock"] = clock
    app = create_app(config, registry, matnexus=mn.client(config), hooks=hooks, discover_hooks=False, **kw)
    return Env(root=tmp_path, config=config, registry=registry, mn=mn, hooks=hooks, app=app, client=TestClient(app))


@pytest.fixture
def env(tmp_path: Path):
    e = build_env(tmp_path)
    yield e
    e.client.close()
    store = tmp_path / "data" / "recipes"
    if store.exists():
        unlock_tree(store)


@pytest.fixture
def active_env(env: Env):
    """레시피 bending3p-param@1 이 active 인 환경."""
    env.activate_recipe()
    return env


@pytest.fixture
def make_env(tmp_path: Path):
    made: list[Env] = []

    def _make(**kw: Any) -> Env:
        e = build_env(tmp_path, **kw)
        made.append(e)
        return e

    yield _make
    for e in made:
        e.client.close()
    store = tmp_path / "data" / "recipes"
    if store.exists():
        unlock_tree(store)


def error_codes(resp: httpx.Response) -> list[str]:
    """400 응답의 details[].code 목록."""
    return [d["code"] for d in resp.json()["error"]["details"]]


def deep(obj: Any) -> Any:
    return copy.deepcopy(obj)


__all__ = [n for n in dir() if not n.startswith("_")]
