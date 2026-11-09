"""설정 로더: (a) 기본값 로드 (b) 키 집합 = module-boundaries.md §6 표."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from matobserver.core.config import (
    CONFIG_KEYS,
    KEY_SPECS,
    Config,
    default_nested,
    default_values,
    load_config,
    resolve_secret,
)
from matobserver.core.errors import ConfigError

REPO = Path(__file__).resolve().parents[3]
MODULE_BOUNDARIES = REPO / "docs" / "contracts" / "module-boundaries.md"
EXAMPLE = REPO / "config" / "matobserver.example.yaml"

# module-boundaries.md §6 표를 사람이 옮겨 적은 기대 키 집합 (alignment 는 testspec.md §5·§5.1 표의 키 전부)
EXPECTED_KEYS = {
    "paths": ["data_dir", "work_dir"],
    "jobstore": ["sqlite_path"],
    "api": ["host", "port", "page_default_limit", "page_max_limit", "idempotency_ttl_s",
            "max_tests_per_job", "max_runs_per_test", "max_slots"],
    "auth": ["service_tokens", "admin_tokens"],
    "files": ["max_bytes"],
    "artifacts": ["retention_days"],
    "matnexus": ["base_url", "token", "timeout_s", "curve_max_points"],
    "worker": ["cancel_poll_s"],
    "registry": ["paths"],
    "recipes": ["store_dir", "max_bundle_bytes", "max_unpacked_bytes", "max_files"],
    "recipe_verify": ["min_points", "unit_ratio_tol", "max_initial_penetration_m", "max_initial_force_ratio",
                      "mesh_convergence_tol", "reference_curve_tol"],
    "cli": ["server_url", "poll_interval_s"],
    "solver": ["run_timeout_s", "kill_grace_s", "poll_interval_s", "keep_d3plot", "keep_all_files",
               "d3plot_frames", "implicit.t_end_s", "explicit.loading_velocity_m_s", "explicit.max_ke_ratio",
               "license_retry.interval_s", "license_retry.max_wait_s"],
    "solver.lsdyna": ["batch_command", "script", "mode", "ncpu", "precision", "termination_patterns",
                      "license_error_patterns", "version_pattern"],
    "mesh": ["max_elements", "min_scaled_jacobian", "max_aspect_ratio", "volume_rel_tol_parametric",
             "volume_rel_tol_stp", "gmsh_threads", "jig.rigid_E_pa", "jig.rigid_poisson",
             "jig.rigid_density_kg_m3"],
    "geometry": ["layer_sum_rel_tol"],
    "engine": ["default_solver", "default_plasticity_mode", "default_seed", "stages.elastic.optimizer",
               "stages.plastic.optimizer", "stages.joint.enabled", "stages.joint.optimizer", "stages.joint.max_evaluations", "stages.joint.e_bound_rel", "elastic_stage.optimizer", "plastic_stage.optimizer", "stagnation.k", "stagnation.rel_tol", "abort_if_initial_all_failed"],
    "budget": ["default_max_evaluations", "max_evaluations_limit", "default_max_wall_time_s"],
    "alignment": ["unload_tol_rel", "dedup_tol_rel", "min_raw_points", "min_loading_fraction",
                  "contact_force_ratio", "toe_correction", "toe_fit_lo", "toe_fit_hi", "toe_fit_min_points",
                  "time_shift_bounds_s", "failure_cut", "failure_drop_ratio", "grid_points",
                  "elastic_min_points", "plastic_min_points", "filter.kind", "filter.presample_points",
                  "filter.window", "filter.cfc_class", "output_oversample",
                  "elastic_dev_tol", "elastic_min_raw_points"],
    "objective": ["region_weights", "shortfall_penalty", "failed_run_J", "scalar_floor", "similarity"],
    "cases": ["dimension_merge_rel_tol"],
    "identifiability": ["precheck_mode", "min_points_per_param", "post_fit", "fd_step_rel", "max_extra_evals",
                        "corr_threshold", "min_sensitivity", "max_condition"],
    "testspec.bending": ["prefer_deflection", "min_span_ratio", "beam_theory_span_ratio", "overtravel_ratio",
                         "min_eps_p0", "rate_from_time_channel"],
    "material": ["default_density_kg_m3", "default_E_range_pa", "default_sigma_y_range_pa", "mat024.max_strain",
                 "mat024_coeff.default_law", "mat024_table.nodes", "mat024_table.first_node_strain", "mat024_table.smoothing_weight"],
    "result": ["table_points", "table_spacing", "table_first_step", "extrapolate_to_strain",
               "extrapolation_method", "at_bound_tol"],
    "uncertainty": ["per_specimen_refit", "refit_max_evaluations"],
    "verify": ["roundtrip_enabled", "roundtrip_tol", "roundtrip_strain", "explicit_check.enabled",
               "explicit_check.elform", "explicit_check.hourglass.ihq", "explicit_check.hourglass.qm",
               "explicit_check.loading_velocity_m_s", "explicit_check.max_ke_ratio",
               "explicit_check.max_region_error", "explicit_check.max_peak_rel_diff",
               "explicit_check.run_timeout_s"],
}


def _expected_flat() -> set[str]:
    return {f"{section}.{k}" for section, keys in EXPECTED_KEYS.items() for k in keys}


# ---------------------------------------------------------------- (a) 기본값
def test_defaults_load_without_file():
    cfg = load_config(env={})  # 파일·환경변수 없음 -> 기본값만
    assert cfg.keys() == CONFIG_KEYS
    assert cfg.get("solver.lsdyna.ncpu") == 4
    assert cfg.solver.lsdyna.ncpu == 4
    assert cfg.solver.lsdyna.batch_command == ["{script}", "-i", "{main}", "-n", "{ncpu}"]
    assert cfg.mesh.jig.rigid_E_pa == 2.1e11
    assert cfg.mesh.jig.rigid_poisson == 0.3
    assert cfg.mesh.jig.rigid_density_kg_m3 == 7850
    assert cfg.mesh.gmsh_threads == 1
    assert cfg.registry.paths == ["config/plugins.d"]
    assert cfg.testspec.bending.prefer_deflection is False  # 결정 2026-11-08: 크로스헤드 기본
    assert cfg.testspec.bending.rate_from_time_channel is True
    assert cfg.verify.explicit_check.enabled is False
    assert cfg.verify.explicit_check.hourglass.ihq == 6
    assert cfg.verify.explicit_check.run_timeout_s is None
    assert cfg.objective.region_weights == {"elastic": 1.0, "plastic": 1.0}
    assert cfg.alignment.filter.kind == "none"
    assert cfg.solver.keep_d3plot == "best"
    assert cfg.identifiability.precheck_mode == "reject"
    assert cfg.artifacts.retention_days is None


def test_placeholder_values_for_unresolved_budget():
    # module-boundaries.md §6 마지막 문단: 자리표시값 120, 300, 172800, 3600
    cfg = Config()
    assert cfg.budget.default_max_evaluations == 120
    assert cfg.budget.max_evaluations_limit == 300
    assert cfg.budget.default_max_wall_time_s == 172800
    assert cfg.solver.run_timeout_s == 3600


def test_contract_table_defaults():
    cfg = Config()
    assert (cfg.api.page_default_limit, cfg.api.page_max_limit) == (50, 500)
    assert cfg.api.idempotency_ttl_s == 86400
    assert (cfg.api.max_tests_per_job, cfg.api.max_runs_per_test, cfg.api.max_slots) == (8, 20, 8)
    assert cfg.files.max_bytes == 200 * 1024 * 1024
    assert cfg.recipes.max_bundle_bytes == 50 * 1024 * 1024
    assert cfg.recipes.max_unpacked_bytes == 200 * 1024 * 1024
    assert cfg.recipes.max_files == 500
    assert cfg.worker.cancel_poll_s == 5
    assert cfg.result.table_points == 50 and cfg.result.table_first_step == 1e-4
    assert cfg.result.at_bound_tol == 0.01
    assert cfg.material.default_E_range_pa == [1e6, 1e12]
    assert cfg.material.default_sigma_y_range_pa == [1e5, 5e9]
    assert cfg.material.mat024.max_strain == 0.2
    assert cfg.material.mat024_table.nodes == 8
    assert cfg.alignment.grid_points == 200 and cfg.alignment.output_oversample == 4


def test_get_returns_copy_and_config_is_read_only():
    cfg = Config()
    v = cfg.get("objective.region_weights")
    v["elastic"] = 99.0
    assert cfg.get("objective.region_weights")["elastic"] == 1.0
    with pytest.raises(AttributeError):
        cfg.foo = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        cfg.solver.ncpu = 1
    with pytest.raises(KeyError):
        cfg.get("solver.nope")


# ---------------------------------------------------------------- (b) 키 집합 = §6 표
def test_key_set_matches_hand_transcribed_table():
    assert set(CONFIG_KEYS) == _expected_flat()


def _expand(token: str) -> list[str]:
    m = re.match(r"^(.*)\{([^{}]+)\}(.*)$", token)
    if not m:
        return [token]
    pre, inner, post = m.groups()
    return [f"{pre}{part.strip()}{post}" for part in inner.split(",")]


def _doc_keys() -> set[str]:
    """module-boundaries.md §6 표의 (섹션, 주요 키) 열을 읽어 점 표기 키 집합을 만든다 (alignment 행은 키 열거 없음)."""
    text = MODULE_BOUNDARIES.read_text(encoding="utf-8")
    start = text.index("## 6. 설정 파일")
    end = text.index("## 7. 결정 추적표")
    out: set[str] = set()
    for line in text[start:end].splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        section = cells[0].strip("`")
        # `\|` (표 안의 이스케이프된 파이프) 때문에 분할이 어긋날 수 있어 키 열은 원문에서 다시 잘라낸다
        key_cell = re.sub(r"\\\|", "/", line.strip().strip("|")).split("|")[1]
        for tok in re.findall(r"`([^`]+)`", key_cell):
            tok = tok.replace("[]", "")
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_.{},]*$", tok):
                continue  # 예시 값(`["{script}", ...]`)·열거값 제외
            for key in _expand(tok):
                out.add(f"{section}.{key}")
    return out


@pytest.mark.skipif(not MODULE_BOUNDARIES.exists(), reason="docs/ not available")
def test_key_set_matches_contract_document():
    doc = _doc_keys()
    assert doc, "failed to parse §6 table"
    code = set(CONFIG_KEYS)
    # alignment 는 표에 키가 열거되지 않고 'testspec.md §5 표의 키 전부'로 위임돼 있다 (위 손 옮김 집합에서 검사)
    code_non_alignment = {k for k in code if not k.startswith("alignment.")}
    doc_non_alignment = {k for k in doc if not k.startswith("alignment.")}
    assert code_non_alignment == doc_non_alignment


@pytest.mark.skipif(not (REPO / "docs" / "contracts" / "testspec.md").exists(), reason="docs/ not available")
def test_alignment_keys_appear_in_testspec_section5():
    text = (REPO / "docs" / "contracts" / "testspec.md").read_text(encoding="utf-8")
    sec = text[text.index("## 5. 정렬"):text.index("## 6. 변위 채널")]
    for key in (k for k in CONFIG_KEYS if k.startswith("alignment.")):
        assert key.split(".", 1)[1] in sec, key


def test_no_key_is_also_a_section_prefix():
    prefixes = {".".join(k.split(".")[:i]) for k in CONFIG_KEYS for i in range(1, len(k.split(".")))}
    assert not (CONFIG_KEYS & prefixes)


def test_every_default_passes_its_own_validation():
    Config(default_values())  # 예외 없음
    for spec in KEY_SPECS:
        assert spec.doc


# ---------------------------------------------------------------- 예시 파일
def test_example_yaml_matches_defaults_exactly():
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert data == default_nested()
    cfg = load_config(EXAMPLE, env={})
    assert cfg.to_dict() == default_nested()


# ---------------------------------------------------------------- 파일·환경변수
def test_yaml_partial_override_and_dict_merge(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("solver:\n  lsdyna:\n    ncpu: 16\n    script: /opt/ls.sh\nobjective:\n  region_weights: {plastic: 2.0}\n",
                 encoding="utf-8")
    cfg = load_config(p, env={})
    assert cfg.solver.lsdyna.ncpu == 16
    assert cfg.solver.lsdyna.script == "/opt/ls.sh"
    assert cfg.solver.lsdyna.mode == "smp"  # 나머지는 기본값
    assert cfg.objective.region_weights == {"elastic": 1.0, "plastic": 2.0}
    assert cfg.source_path == p


def test_env_overrides(tmp_path: Path):
    env = {
        "MATOBSERVER__SOLVER__LSDYNA__NCPU": "8",
        "MATOBSERVER__MESH__JIG__RIGID_E_PA": "2.0e11",
        "MATOBSERVER__VERIFY__EXPLICIT_CHECK__ENABLED": "true",
        "MATOBSERVER__ARTIFACTS__RETENTION_DAYS": "30",
        "MATOBSERVER__OBJECTIVE__REGION_WEIGHTS__PLASTIC": "3",
        "MATOBSERVER__REGISTRY__PATHS": "[a, b]",
        "MATOBSERVER_ADMIN_TOKEN": "ignored-not-a-config-key",
    }
    cfg = load_config(env=env)
    assert cfg.solver.lsdyna.ncpu == 8
    assert cfg.mesh.jig.rigid_E_pa == 2.0e11
    assert cfg.verify.explicit_check.enabled is True
    assert cfg.artifacts.retention_days == 30
    assert cfg.objective.region_weights == {"elastic": 1.0, "plastic": 3}
    assert cfg.registry.paths == ["a", "b"]


def test_env_wins_over_file(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("solver:\n  lsdyna:\n    ncpu: 16\n", encoding="utf-8")
    assert load_config(p, env={"MATOBSERVER__SOLVER__LSDYNA__NCPU": "2"}).solver.lsdyna.ncpu == 2


def test_config_path_from_env_var(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text("api:\n  port: 9100\n", encoding="utf-8")
    assert load_config(env={"MATOBSERVER_CONFIG": str(p)}).api.port == 9100
    with pytest.raises(ConfigError):
        load_config(env={"MATOBSERVER_CONFIG": str(tmp_path / "missing.yaml")})
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml", env={})


@pytest.mark.parametrize(
    "yaml_text",
    [
        "nope: 1\n",  # 모르는 섹션
        "solver:\n  nope: 1\n",  # 모르는 키
        "solver:\n  lsdyna:\n    ncpu: \"4\"\n",  # 타입 불일치
        "solver:\n  lsdyna:\n    ncpu: true\n",  # bool 은 int 가 아니다
        "solver:\n  lsdyna:\n    ncpu: 4.5\n",
        "solver:\n  keep_d3plot: sometimes\n",  # 허용값 밖
        "api:\n  port: null\n",  # null 불가
        "- not a mapping\n",
        "solver: 3\n",  # 섹션이 사전이 아님
    ],
)
def test_invalid_yaml_rejected(tmp_path: Path, yaml_text: str):
    p = tmp_path / "c.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p, env={})


@pytest.mark.parametrize("name", ["MATOBSERVER__NOPE__KEY", "MATOBSERVER__SOLVER__LSDYNA__NCPU__X", "MATOBSERVER__"])
def test_invalid_env_rejected(name: str):
    with pytest.raises(ConfigError):
        load_config(env={name: "1"})


def test_env_type_error_rejected():
    with pytest.raises(ConfigError):
        load_config(env={"MATOBSERVER__API__PORT": "abc"})


def test_secret_env_reference():
    cfg = Config()
    assert cfg.get("matnexus.token") == "env:MATNEXUS_TOKEN"
    assert cfg.secret("matnexus.token", {"MATNEXUS_TOKEN": "pat-123"}) == "pat-123"
    with pytest.raises(ConfigError):
        cfg.secret("matnexus.token", {})
    assert cfg.with_overrides({"matnexus.token": "literal"}).secret("matnexus.token", {}) == "literal"
    assert cfg.secret("matnexus.base_url") is None
    assert resolve_secret("plain") == "plain"


def test_section_and_with_overrides():
    cfg = Config()
    assert cfg.section("solver.lsdyna")["ncpu"] == 4
    c2 = cfg.with_overrides({"solver.lsdyna.ncpu": 2})
    assert c2.solver.lsdyna.ncpu == 2 and cfg.solver.lsdyna.ncpu == 4
    with pytest.raises(ConfigError):
        cfg.with_overrides({"solver.bogus": 1})


def test_scientific_notation_without_sign_is_a_number(tmp_path: Path):
    # PyYAML 기본 로더는 `2.1e11`, `1e-3` 을 문자열로 읽는다 — 설정 로더는 실수로 읽어야 한다.
    p = tmp_path / "c.yaml"
    p.write_text("mesh:\n  jig:\n    rigid_E_pa: 2.1e11\nmaterial:\n  default_E_range_pa: [1e6, 1e12]\n"
                 "objective:\n  scalar_floor: 1e-9\n", encoding="utf-8")
    cfg = load_config(p, env={})
    assert cfg.mesh.jig.rigid_E_pa == 2.1e11
    assert cfg.material.default_E_range_pa == [1e6, 1e12]
    assert cfg.objective.scalar_floor == 1e-9
