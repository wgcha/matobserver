"""설정 로더 (module-boundaries.md §6).

* 파일: `MATOBSERVER_CONFIG`(기본 `./config/matobserver.yaml`). 기본 경로에 파일이 없으면 기본값만 쓴다.
  `MATOBSERVER_CONFIG`로 명시한 파일이 없으면 오류.
* 환경변수 덮어쓰기: `MATOBSERVER__<SECTION>__<KEY>`(대문자, `__` 구분). 값은 YAML 스칼라/플로 문법으로 해석한다
  (`4` → int, `true` → bool, `[a, b]` → list, `null` → None). 사전 값 키는 `...__REGION_WEIGHTS__PLASTIC=2` 처럼 하위 키를 줄 수 있다.
* 비밀값: `env:VAR` 참조 허용 — `Config.secret(key)`가 환경변수 값으로 풀어 준다(원본 문자열은 `get`으로 그대로 보인다).
* 환경별 값(경로·배치 스크립트·코어 수·임계값)은 코드에 쓰지 않고 이 키 표로만 둔다.

`KEY_SPECS`가 §6 표의 전 키와 기본값의 단일 출처다. `config/matobserver.example.yaml`은 이 표와 같은 값을 담는다
(단위 테스트가 일치를 검사한다). 기본값은 각 계약 문서 표의 괄호 값이며, 계약에 값이 없는 키는 `doc`에 "계약 값 없음"으로 표시했다.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

import yaml

from .errors import ConfigError

ENV_CONFIG_PATH: Final = "MATOBSERVER_CONFIG"
DEFAULT_CONFIG_PATH: Final = "./config/matobserver.yaml"
ENV_PREFIX: Final = "MATOBSERVER__"
SECRET_REF_PREFIX: Final = "env:"

class _YamlLoader(yaml.SafeLoader):
    """PyYAML(YAML 1.1)은 `2.1e11`·`1e-3`(소수점 또는 지수 부호 없음)을 문자열로 읽는다.
    설정 수치를 과학 표기로 쓰는 것이 자연스러우므로 YAML 1.2 식으로 실수로 읽는다."""


_YamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(
        r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
        |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
        |\.[0-9][0-9_]*(?:[eE][-+]?[0-9]+)?
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
        re.X,
    ),
    list("-+0123456789."),
)


def safe_load_yaml(text: str) -> Any:
    """설정·레지스트리 공통 YAML 로더 (safe_load + 과학 표기 실수 인식)."""
    return yaml.load(text, Loader=_YamlLoader)  # noqa: S506 - SafeLoader 하위 클래스


_NUM: Final = (int, float)
_STR_OR_NONE: Final = (str, type(None))
_NUM_OR_NONE: Final = (int, float, type(None))
_INT_OR_NONE: Final = (int, type(None))


@dataclass(frozen=True)
class KeySpec:
    key: str  # 점으로 구분한 전체 경로 ("solver.lsdyna.ncpu")
    default: Any
    doc: str  # 정의 문서 / 근거 (예시 파일 주석에 쓰인다)
    types: tuple[type, ...] | None = None  # None이면 기본값 타입에서 추론
    choices: tuple[Any, ...] | None = None

    def accepted_types(self) -> tuple[type, ...]:
        if self.types is not None:
            return self.types
        d = self.default
        if isinstance(d, bool):
            return (bool,)
        if isinstance(d, int):
            return (int,)
        if isinstance(d, float):
            return _NUM
        if isinstance(d, str):
            return (str,)
        if isinstance(d, list):
            return (list,)
        if isinstance(d, dict):
            return (dict,)
        return (object,)


def _k(key: str, default: Any, doc: str, *, types: tuple[type, ...] | None = None,
       choices: tuple[Any, ...] | None = None) -> KeySpec:
    return KeySpec(key, default, doc, types, choices)


_PLACEHOLDER = "미결(U-2): 자리표시값, plan 해석 예산 결정 대기"
_NO_VALUE = "계약 값 없음 — 초기값, 환경/운영에서 조정"

KEY_SPECS: Final[tuple[KeySpec, ...]] = (
    # ---- paths · jobstore (module-boundaries.md §4.5, §6) ----
    _k("paths.data_dir", "./data", f"module-boundaries.md §4.5 ({_NO_VALUE})"),
    _k("paths.work_dir", "./data/work", f"module-boundaries.md §4.5: 작업 폴더 {{work_dir}}/{{job_id}}/ ({_NO_VALUE})"),
    _k("jobstore.sqlite_path", "./data/jobs.sqlite", f"api.md §5 SQLite JobStore ({_NO_VALUE})"),
    # ---- api · auth · files · artifacts (api.md) ----
    _k("api.host", "127.0.0.1", f"api.md ({_NO_VALUE})"),
    _k("api.port", 8000, f"api.md ({_NO_VALUE})"),
    _k("api.page_default_limit", 50, "api.md §1 페이지 기본 limit"),
    _k("api.page_max_limit", 500, "api.md §1 페이지 최대 limit"),
    _k("api.idempotency_ttl_s", 86400, "api.md §4.2 Idempotency-Key 보존(s)", types=_NUM),
    _k("api.max_tests_per_job", 8, "api.md §4.2 tests 1..N"),
    _k("api.max_runs_per_test", 20, "api.md §4.2 시험당 run 1..N"),
    _k("api.max_slots", 8, "api.md §4.3 slots 1..N"),
    _k("auth.service_tokens", [], "api.md §2: [{name, sha256}] (평문 저장 금지)"),
    _k("auth.admin_tokens", [], "api.md §2: [{name, sha256}] (서비스 범위 + /v1/admin/*)"),
    _k("files.max_bytes", 209715200, "api.md §4.1: 업로드 파일 최대 크기(기본 200 MiB)"),
    _k("artifacts.retention_days", None, "api.md §6: null=무기한", types=_NUM_OR_NONE),
    # ---- matnexus · worker (api.md §2, §4.12, result.md §7.1) ----
    _k("matnexus.base_url", None, "api.md §2 MatNexus 주소 (환경별 필수 입력)", types=_STR_OR_NONE),
    _k("matnexus.token", "env:MATNEXUS_TOKEN", "api.md §2: 서비스 계정 PAT, `env:VAR` 참조 형식 허용", types=_STR_OR_NONE),
    _k("matnexus.timeout_s", 30.0, f"api.md §2 ({_NO_VALUE})", types=_NUM),
    _k("matnexus.curve_max_points", 5000, "result.md §4.11 예시 traceability.matnexus.curve_max_points"),
    _k("worker.cancel_poll_s", 5, "api.md §4.12: 워커 취소 폴링(s)", types=_NUM),
    # ---- registry · recipes · recipe_verify · cli ----
    _k("registry.paths", ["config/plugins.d"], "plugins.md §2: 아래 *.yaml을 이름순으로 모두 읽는다"),
    _k("recipes.store_dir", "./data/recipes", f"recipe.md §2 ({_NO_VALUE})"),
    _k("recipes.max_bundle_bytes", 52428800, "recipe.md §2/api.md §8.1: 번들 zip 최대(50 MiB)"),
    _k("recipes.max_unpacked_bytes", 209715200, "recipe.md §2: 해제 총크기 상한(200 MiB)"),
    _k("recipes.max_files", 500, "recipe.md §2: 파일 수 상한"),
    _k("recipe_verify.min_points", 20, "recipe.md §7.2 VERIFY_RESPONSE"),
    _k("recipe_verify.unit_ratio_tol", 1.5, "recipe.md §7.2 VERIFY_UNITS", types=_NUM),
    _k("recipe_verify.max_initial_penetration_m", 1e-6, "recipe.md §7.2 VERIFY_PENETRATION", types=_NUM),
    _k("recipe_verify.max_initial_force_ratio", 0.01, "recipe.md §7.2 VERIFY_PENETRATION", types=_NUM),
    _k("recipe_verify.mesh_convergence_tol", 0.02, "recipe.md §7.2 VERIFY_MESH_CONVERGENCE", types=_NUM),
    _k("recipe_verify.reference_curve_tol", 0.05, "recipe.md §7.2 VERIFY_REFERENCE_CURVE", types=_NUM),
    _k("cli.server_url", "http://127.0.0.1:8000", f"recipe.md §6 ({_NO_VALUE})"),
    _k("cli.poll_interval_s", 5.0, f"recipe.md §6 --wait 폴링 간격 ({_NO_VALUE})", types=_NUM),
    # ---- solver (plugins.md §4.2, testspec.md) ----
    _k("solver.run_timeout_s", 3600, f"plugins.md §4.2/testspec.md §8: 해석 1회 상한(s). {_PLACEHOLDER}", types=_NUM),
    _k("solver.kill_grace_s", 10.0, f"plugins.md §5.2 S4: 강제 종료 유예(s) ({_NO_VALUE})", types=_NUM),
    _k("solver.poll_interval_s", 1.0, f"plugins.md §4.2: CancelToken 확인 간격(s) ({_NO_VALUE})", types=_NUM),
    _k("solver.keep_d3plot", "best", "plugins.md §4.2 cleanup 매핑표", choices=("best", "all", "none")),
    _k("solver.keep_all_files", False, "plugins.md §4.2: true면 매핑표와 무관하게 모두 보존(디버그)"),
    _k("solver.d3plot_frames", 20, "testspec.md §5 7단계: dt_plot = mo_tend / d3plot_frames"),
    _k("solver.implicit.t_end_s", 1.0, "testspec.md §10.4 mo_tend (implicit)", types=_NUM),
    _k("solver.explicit.loading_velocity_m_s", 0.5, "testspec.md §10.4 mo_tend (explicit)", types=_NUM),
    _k("solver.explicit.max_ke_ratio", 0.05, "recipe.md §7.2 WARN_KE_RATIO / testspec.md §11", types=_NUM),
    _k("solver.license_retry.interval_s", 30.0, f"plugins.md §4.2: 라이선스 재시도 간격(s) ({_NO_VALUE})", types=_NUM),
    _k("solver.license_retry.max_wait_s", 1800.0, f"api.md §7.3 LICENSE_TIMEOUT: 대기 상한(s) ({_NO_VALUE})", types=_NUM),
    # ---- solver.lsdyna (plugins.md §4.2, U-1 환경 미정) ----
    _k("solver.lsdyna.batch_command", ["{script}", "-i", "{main}", "-n", "{ncpu}"],
       "module-boundaries.md §6: 인자 템플릿 리스트. 치환 키 {script} {main} {ncpu}"),
    _k("solver.lsdyna.script", None, "U-1 미결: 사내 배치 스크립트 경로 (환경별 필수 입력)", types=_STR_OR_NONE),
    _k("solver.lsdyna.mode", "smp", "module-boundaries.md §6 smp|mpp", choices=("smp", "mpp")),
    _k("solver.lsdyna.ncpu", 4, "module-boundaries.md §6: 기본 4"),
    _k("solver.lsdyna.precision", "double", f"result.md §4.11 예시 solver.precision ({_NO_VALUE}; single|double)",
       choices=("single", "double")),
    _k("solver.lsdyna.termination_patterns",
       {"normal": ["N o r m a l    t e r m i n a t i o n", "Normal termination"],
        "error": ["E r r o r    t e r m i n a t i o n", "Error termination"]},
       f"종료 판정 정규식/문구. 구조 {{normal: [...], error: [...]}}는 해석 ({_NO_VALUE})", types=(dict,)),
    _k("solver.lsdyna.license_error_patterns",
       ["license", "LICENSE", "lmgrd", "FlexNet", "Unable to checkout"],
       f"라이선스 오류 로그 문구 (대소문자 구분) ({_NO_VALUE})"),
    _k("solver.lsdyna.version_pattern", r"(R\d+(?:\.\d+)*\S*(?:\s+(?:SMP|MPP))?(?:\s+[sd])?)",
       f"d3hsp/messag에서 버전 문자열을 뽑는 정규식(첫 그룹) ({_NO_VALUE})"),
    # ---- mesh · geometry ----
    _k("mesh.max_elements", 200000, f"plugins.md §4.4 MeshOptions.max_elements ({_NO_VALUE})"),
    _k("mesh.min_scaled_jacobian", 0.2, "plugins.md §5.4 G2", types=_NUM),
    _k("mesh.max_aspect_ratio", 10.0, "plugins.md §5.4 G2", types=_NUM),
    _k("mesh.volume_rel_tol_parametric", 1e-9, "plugins.md §5.4 G1", types=_NUM),
    _k("mesh.volume_rel_tol_stp", 0.01, "plugins.md §5.4 G1", types=_NUM),
    _k("mesh.gmsh_threads", 1, "plugins.md §4.4 결정성: 스레드 1 고정"),
    _k("mesh.jig.rigid_E_pa", 2.1e11, "module-boundaries.md §2 지그 *MAT_RIGID E (Pa)", types=_NUM),
    _k("mesh.jig.rigid_poisson", 0.3, "module-boundaries.md §2 지그 *MAT_RIGID PR", types=_NUM),
    _k("mesh.jig.rigid_density_kg_m3", 7850.0, "module-boundaries.md §2 지그 *MAT_RIGID RO (kg/m^3)", types=_NUM),
    _k("geometry.layer_sum_rel_tol", 1e-3, "api.md §4.4 layered_box 두께 합 상대오차", types=_NUM),
    # ---- engine · budget ----
    _k("engine.default_solver", "lsdyna", "api.md §4.2 solver 기본"),
    _k("engine.default_plasticity_mode", "coeff",
       "api.md §4.3 plasticity_mode 기본. U-1(plugins.md) 미결 — 현재 계수 모드 유지", choices=("coeff", "table")),
    _k("engine.default_seed", 20261108, "result.md §4.11 예시 seed (계약 값 없음 — 예시값 사용)"),
    _k("engine.elastic_stage.optimizer", "lsq", "plugins.md P-5: 탄성 단계 기본 최적화기"),
    _k("engine.plastic_stage.optimizer", "bo", "plan: BO 기본 (plugins.md §4.1)"),
    _k("engine.stagnation.k", 5, f"plugins.md §4.1: 연속 k회 정체 ({_NO_VALUE})"),
    _k("engine.stagnation.rel_tol", 1e-3, f"plugins.md §4.1: 상대 개선 임계 ({_NO_VALUE})", types=_NUM),
    _k("engine.abort_if_initial_all_failed", True, "api.md §7.3 ALL_INITIAL_RUNS_FAILED"),
    _k("budget.default_max_evaluations", 120, f"api.md §4.6. {_PLACEHOLDER}"),
    _k("budget.max_evaluations_limit", 300, f"api.md §4.6 BUDGET_EXCEEDS_LIMIT. {_PLACEHOLDER}"),
    _k("budget.default_max_wall_time_s", 172800, f"api.md §4.6. {_PLACEHOLDER}", types=_NUM),
    # ---- alignment (testspec.md §5, §5.1) ----
    _k("alignment.unload_tol_rel", 0.005, "testspec.md §5 1단계 (d)", types=_NUM),
    _k("alignment.dedup_tol_rel", 1e-9, "testspec.md §5 1단계 (e)", types=_NUM),
    _k("alignment.min_raw_points", 20, "testspec.md §5 1단계 / CURVE_TOO_FEW_POINTS"),
    _k("alignment.min_loading_fraction", 0.3, "testspec.md §5 1단계 / CURVE_NOT_MONOTONIC", types=_NUM),
    _k("alignment.contact_force_ratio", 0.02, "testspec.md §5 3단계", types=_NUM),
    _k("alignment.toe_correction", True, "testspec.md §5 3단계"),
    _k("alignment.toe_fit_lo", 0.10, "testspec.md §5 3단계 (G4 조정 항목 T-7)", types=_NUM),
    _k("alignment.toe_fit_hi", 0.40, "testspec.md §5 3단계 (G4 조정 항목 T-7)", types=_NUM),
    _k("alignment.toe_fit_min_points", 5, "testspec.md §5 3단계 / CURVE_NO_ELASTIC_REGION"),
    _k("alignment.time_shift_bounds_s", None, "testspec.md §5 3단계 (n차 예약, null)", types=_NUM_OR_NONE),
    _k("alignment.failure_cut", True, "testspec.md §5 4단계"),
    _k("alignment.failure_drop_ratio", 0.3, "testspec.md §5 4단계", types=_NUM),
    _k("alignment.grid_points", 200, "testspec.md §5 5단계 N"),
    _k("alignment.elastic_min_points", 30, "testspec.md §5 5단계"),
    _k("alignment.plastic_min_points", 30, "testspec.md §5 5단계"),
    _k("alignment.filter.kind", "none", "testspec.md §5 6단계: 준정적 none (고속 cfc는 n차)",
       choices=("none", "moving_average", "cfc")),
    _k("alignment.filter.presample_points", 2000, "testspec.md §5 6단계"),
    _k("alignment.filter.window", 11, "testspec.md §5 6단계"),
    _k("alignment.filter.cfc_class", 60, "testspec.md §5 6단계 (SAE J211 CFC)"),
    _k("alignment.output_oversample", 4, "testspec.md §5 7단계 k"),
    _k("alignment.elastic_dev_tol", 0.05, "testspec.md §5.1", types=_NUM),
    _k("alignment.elastic_min_raw_points", 5, "testspec.md §5.1 / CURVE_NO_ELASTIC_REGION"),
    # ---- objective · cases · identifiability ----
    _k("objective.region_weights", {"elastic": 1.0, "plastic": 1.0}, "testspec.md §8: 구간 가중 w_k", types=(dict,)),
    _k("objective.shortfall_penalty", 1.0, "testspec.md §5 8단계 / §8 λ_s", types=_NUM),
    _k("objective.failed_run_J", 10.0, "testspec.md §8 해석 실패 J", types=_NUM),
    _k("objective.scalar_floor", 1e-12, "testspec.md §8 스칼라 하한", types=_NUM),
    _k("objective.similarity", "l2", "testspec.md §11 (n차 예약): l2 | iso18571", choices=("l2", "iso18571")),
    _k("cases.dimension_merge_rel_tol", 0.01, "testspec.md §9.1", types=_NUM),
    _k("identifiability.precheck_mode", "reject", "testspec.md §9.2 / T-5 (reject|warn)", choices=("reject", "warn")),
    _k("identifiability.min_points_per_param", 5, "testspec.md §9.2", types=_NUM),
    _k("identifiability.post_fit", True, "testspec.md §9.3"),
    _k("identifiability.fd_step_rel", 0.02, "testspec.md §9.3 h", types=_NUM),
    _k("identifiability.max_extra_evals", 30, "testspec.md §9.3"),
    _k("identifiability.corr_threshold", 0.95, "testspec.md §9.3", types=_NUM),
    _k("identifiability.min_sensitivity", 1e-3, "testspec.md §9.3", types=_NUM),
    _k("identifiability.max_condition", 1e8, "testspec.md §9.3", types=_NUM),
    # ---- testspec.bending (testspec.md §6, §10) ----
    _k("testspec.bending.prefer_deflection", False, "testspec.md §6 (결정 2026-11-08: 크로스헤드 기본)"),
    _k("testspec.bending.min_span_ratio", 4.0, "testspec.md §10.2 GEOMETRY_DIMENSION_INVALID", types=_NUM),
    _k("testspec.bending.beam_theory_span_ratio", 10.0, "testspec.md §10.2 BEAM_THEORY_APPROXIMATE", types=_NUM),
    _k("testspec.bending.overtravel_ratio", 0.05, "testspec.md §10.4 mo_dend", types=_NUM),
    _k("testspec.bending.min_eps_p0", 0.01, "testspec.md §10.5 eps_p_max0 하한", types=_NUM),
    _k("testspec.bending.rate_from_time_channel", True, "testspec.md §10.6"),
    # ---- material (plugins.md §4.3, api.md §4.3) ----
    _k("material.default_density_kg_m3", 7850.0, f"api.md §4.3 DENSITY_DEFAULTED ({_NO_VALUE})", types=_NUM),
    _k("material.default_E_range_pa", [1e6, 1e12], "plugins.md §4.3: [lower, upper], log"),
    _k("material.default_sigma_y_range_pa", [1e5, 5e9], "plugins.md §4.3: [lower, upper], log"),
    _k("material.mat024.max_strain", 0.2, "plugins.md §4.3 옵션 max_strain", types=_NUM),
    _k("material.mat024_coeff.default_law", "swift", "plugins.md §4.3 / U-1", choices=("swift", "voce", "swift_voce")),
    _k("material.mat024_table.nodes", 8, "plugins.md §4.3 옵션 table_nodes"),
    # ---- result · uncertainty (result.md) ----
    _k("result.table_points", 50, "result.md §3.2 / plugins.md §4.3"),
    _k("result.table_spacing", "geometric", "result.md §3.2", choices=("geometric", "linear")),
    _k("result.table_first_step", 1e-4, "result.md §3.2", types=_NUM),
    _k("result.extrapolate_to_strain", None, "result.md §3.2: null이면 행 추가 없음", types=_NUM_OR_NONE),
    _k("result.extrapolation_method", "last_slope", "result.md §3.2", choices=("last_slope", "hardening_law")),
    _k("result.at_bound_tol", 0.01, "result.md §4.2 PARAM_AT_BOUND", types=_NUM),
    _k("uncertainty.per_specimen_refit", False, "result.md §4.4"),
    _k("uncertainty.refit_max_evaluations", 30, f"result.md §4.4: run별 재식별 예산 ({_NO_VALUE})"),
    # ---- verify (result.md §4.9·§4.12, testspec.md §8.1) ----
    _k("verify.roundtrip_enabled", True, "result.md §4.9"),
    _k("verify.roundtrip_tol", 0.01, "result.md §4.9", types=_NUM),
    _k("verify.roundtrip_strain", None, "result.md §4.9: null = εp_valid", types=_NUM_OR_NONE),
    _k("verify.explicit_check.enabled", False, "testspec.md §8.1"),
    _k("verify.explicit_check.elform", 1, "testspec.md §8.1 요소 정식화"),
    _k("verify.explicit_check.hourglass.ihq", 6, "testspec.md §8.1"),
    _k("verify.explicit_check.hourglass.qm", 0.1, "testspec.md §8.1", types=_NUM),
    _k("verify.explicit_check.loading_velocity_m_s", 0.5, "testspec.md §8.1", types=_NUM),
    _k("verify.explicit_check.max_ke_ratio", 0.05, "testspec.md §8.1", types=_NUM),
    _k("verify.explicit_check.max_region_error", 0.05, "testspec.md §8.1", types=_NUM),
    _k("verify.explicit_check.max_peak_rel_diff", 0.05, "testspec.md §8.1", types=_NUM),
    _k("verify.explicit_check.run_timeout_s", None, "testspec.md §8.1: null → solver.run_timeout_s", types=_NUM_OR_NONE),
)

SPECS_BY_KEY: Final[dict[str, KeySpec]] = {s.key: s for s in KEY_SPECS}
assert len(SPECS_BY_KEY) == len(KEY_SPECS), "duplicate config key in KEY_SPECS"

#: 설정 키 전체 집합(점 표기). module-boundaries.md §6 표와 일치해야 한다.
CONFIG_KEYS: Final[frozenset[str]] = frozenset(SPECS_BY_KEY)

# 값이 사전인 키(하위 키는 병합), 접두 경로(섹션) 집합
_DICT_KEYS: Final[frozenset[str]] = frozenset(k for k, s in SPECS_BY_KEY.items() if isinstance(s.default, dict))
_PREFIXES: Final[frozenset[str]] = frozenset(
    ".".join(k.split(".")[:i]) for k in SPECS_BY_KEY for i in range(1, len(k.split(".")))
)
_ENV_NAME_TO_KEY: Final[dict[str, str]] = {k.replace(".", "__").upper(): k for k in SPECS_BY_KEY}


def default_values() -> dict[str, Any]:
    """점 표기 키 → 기본값(깊은 복사)."""
    return {s.key: copy.deepcopy(s.default) for s in KEY_SPECS}


def nest(flat: Mapping[str, Any]) -> dict[str, Any]:
    """점 표기 평면 매핑 → 중첩 사전 (예시 YAML 형태)."""
    root: dict[str, Any] = {}
    for key, value in flat.items():
        parts = key.split(".")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = copy.deepcopy(value)
    return root


def default_nested() -> dict[str, Any]:
    return nest(default_values())


def _type_name(types: tuple[type, ...]) -> str:
    names = []
    for t in types:
        names.append("null" if t is type(None) else t.__name__)
    return "|".join(names)


def _validate(key: str, value: Any) -> Any:
    spec = SPECS_BY_KEY[key]
    types = spec.accepted_types()
    if object not in types:
        # bool 은 int 의 하위 클래스이므로 수치 키에서 따로 걸러낸다.
        is_bool = isinstance(value, bool)
        wants_bool = bool in types
        if is_bool and not wants_bool:
            raise ConfigError(f"config key {key!r}: expected {_type_name(types)}, got bool ({value!r})")
        if not isinstance(value, types):
            raise ConfigError(f"config key {key!r}: expected {_type_name(types)}, got {type(value).__name__} ({value!r})")
    if spec.choices is not None and value not in spec.choices:
        raise ConfigError(f"config key {key!r}: must be one of {list(spec.choices)}, got {value!r}")
    return value


def _flatten_into(flat: dict[str, Any], node: Any, prefix: str, origin: str) -> None:
    """중첩 사전을 점 표기 키로 펴며 `flat`에 반영한다. 모르는 키는 오류."""
    if not isinstance(node, Mapping):
        raise ConfigError(f"{origin}: section {prefix!r} must be a mapping, got {type(node).__name__}")
    for name, value in node.items():
        if not isinstance(name, str):
            raise ConfigError(f"{origin}: non-string key {name!r} under {prefix or '<root>'!r}")
        path = f"{prefix}.{name}" if prefix else name
        if path in SPECS_BY_KEY:
            _assign(flat, path, value, origin)
        elif path in _PREFIXES:
            _flatten_into(flat, value, path, origin)
        else:
            raise ConfigError(f"{origin}: unknown config key {path!r}")


def _assign(flat: dict[str, Any], key: str, value: Any, origin: str) -> None:
    if key in _DICT_KEYS and isinstance(value, Mapping):
        merged = dict(flat[key])
        merged.update(value)  # 사전 값 키는 기본값 위에 얹어 병합
        flat[key] = merged
    else:
        flat[key] = copy.deepcopy(value)


def _env_overrides(env: Mapping[str, str]) -> list[tuple[str, Any, str]]:
    """MATOBSERVER__... 환경변수 → [(key, value, origin)]. 키 이름 대소문자는 표에 맞춘다."""
    out: list[tuple[str, Any, str]] = []
    for name in sorted(env):
        if not name.startswith(ENV_PREFIX):
            continue
        rest = name[len(ENV_PREFIX):]
        if not rest:
            raise ConfigError(f"environment variable {name!r}: missing key path")
        try:
            parsed = safe_load_yaml(env[name])
        except yaml.YAMLError as e:
            raise ConfigError(f"environment variable {name!r}: cannot parse value {env[name]!r}: {e}") from e
        origin = f"env {name}"
        if rest in _ENV_NAME_TO_KEY:
            out.append((_ENV_NAME_TO_KEY[rest], parsed, origin))
            continue
        # 사전 값 키의 하위 키: <KEY>__<SUB>
        for env_key, key in _ENV_NAME_TO_KEY.items():
            if key in _DICT_KEYS and rest.startswith(env_key + "__") and len(rest) > len(env_key) + 2:
                sub = rest[len(env_key) + 2:].lower()
                out.append((key, {sub: parsed}, origin))
                break
        else:
            raise ConfigError(f"environment variable {name!r}: unknown config key")
    return out


class _Node:
    """`cfg.solver.lsdyna.ncpu` 식 접근용 읽기 전용 뷰."""

    __slots__ = ("_cfg", "_prefix")

    def __init__(self, cfg: Config, prefix: str) -> None:
        object.__setattr__(self, "_cfg", cfg)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str) -> Any:
        path = f"{self._prefix}.{name}"
        if path in SPECS_BY_KEY:
            return self._cfg.get(path)
        if path in _PREFIXES:
            return _Node(self._cfg, path)
        raise AttributeError(f"unknown config key {path!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("config is read-only")

    def __repr__(self) -> str:
        return f"<ConfigSection {self._prefix}>"


class Config:
    """불변 설정 객체. 키는 점 표기(`"solver.lsdyna.ncpu"`) 또는 속성(`cfg.solver.lsdyna.ncpu`)으로 읽는다."""

    __slots__ = ("_values", "source_path")

    def __init__(self, values: Mapping[str, Any] | None = None, *, source_path: Path | None = None) -> None:
        flat = default_values()
        if values:
            for key, value in values.items():
                if key not in SPECS_BY_KEY:
                    raise ConfigError(f"unknown config key {key!r}")
                _assign(flat, key, value, "overrides")
        for key, value in flat.items():
            _validate(key, value)
        object.__setattr__(self, "_values", flat)
        object.__setattr__(self, "source_path", source_path)

    # ---- 읽기 ----
    def get(self, key: str) -> Any:
        try:
            return copy.deepcopy(self._values[key])
        except KeyError:
            raise KeyError(f"unknown config key {key!r}") from None

    __getitem__ = get

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in SPECS_BY_KEY:
            return self.get(name)
        if name in _PREFIXES:
            return _Node(self, name)
        raise AttributeError(f"unknown config section {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("config is read-only")

    def keys(self) -> frozenset[str]:
        return frozenset(self._values)

    def to_flat_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._values)

    def to_dict(self) -> dict[str, Any]:
        """중첩 사전 (예시 YAML 형태)."""
        return nest(self._values)

    def section(self, prefix: str) -> dict[str, Any]:
        """접두 경로 아래 값을 중첩 사전으로 돌려준다 (`cfg.section("solver.lsdyna")`)."""
        if prefix not in _PREFIXES:
            raise KeyError(f"unknown config section {prefix!r}")
        sub = {k[len(prefix) + 1:]: v for k, v in self._values.items() if k.startswith(prefix + ".")}
        return nest(sub)

    def path(self, key: str) -> Path:
        """경로 값 키를 Path 로 (상대 경로는 현재 작업 폴더 기준 그대로)."""
        value = self._values[key]
        if not isinstance(value, str):
            raise ConfigError(f"config key {key!r} is not a path string: {value!r}")
        return Path(value).expanduser()

    def secret(self, key: str, env: Mapping[str, str] | None = None) -> str | None:
        """비밀값 키를 풀어 준다: `env:VAR` → 환경변수 값(없으면 ConfigError), 그 외 문자열은 그대로, None은 None."""
        value = self._values[key]
        if value is None:
            return None
        return resolve_secret(value, env, key=key)

    def with_overrides(self, overrides: Mapping[str, Any]) -> Config:
        """점 표기 덮어쓰기를 적용한 새 Config (시험·워커 보조용)."""
        merged = self.to_flat_dict()
        for key, value in overrides.items():
            if key not in SPECS_BY_KEY:
                raise ConfigError(f"unknown config key {key!r}")
            _assign(merged, key, value, "overrides")
        return Config(merged, source_path=self.source_path)

    def __repr__(self) -> str:
        return f"<Config source={str(self.source_path) if self.source_path else 'defaults'}>"


def resolve_secret(value: Any, env: Mapping[str, str] | None = None, *, key: str = "<value>") -> Any:
    """`env:VAR` 참조를 환경변수 값으로 푼다. 참조가 아니면 그대로."""
    if isinstance(value, str) and value.startswith(SECRET_REF_PREFIX):
        var = value[len(SECRET_REF_PREFIX):]
        if not var:
            raise ConfigError(f"config key {key!r}: empty env reference")
        environ = os.environ if env is None else env
        if var not in environ:
            raise ConfigError(f"config key {key!r}: environment variable {var!r} is not set")
        return environ[var]
    return value


def load_config(path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None) -> Config:
    """설정 로드: 기본값 ← YAML 파일 ← 환경변수.

    `path`가 주어지면 그 파일이 없을 때 오류. 없으면 `MATOBSERVER_CONFIG`(명시, 없으면 오류), 그것도 없으면
    `./config/matobserver.yaml`(없으면 기본값만).
    """
    environ = os.environ if env is None else env
    explicit: str | os.PathLike[str] | None = path if path is not None else environ.get(ENV_CONFIG_PATH)
    flat = default_values()
    source: Path | None = None

    if explicit is not None:
        source = Path(explicit).expanduser()
        if not source.is_file():
            raise ConfigError(f"config file not found: {source}")
    else:
        candidate = Path(DEFAULT_CONFIG_PATH)
        if candidate.is_file():
            source = candidate

    if source is not None:
        try:
            data = safe_load_yaml(source.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ConfigError(f"{source}: invalid YAML: {e}") from e
        if data is not None:
            _flatten_into(flat, data, "", str(source))

    for key, value, origin in _env_overrides(environ):
        _assign(flat, key, value, origin)

    for key, value in flat.items():
        _validate(key, value)
    return Config(flat, source_path=source)
