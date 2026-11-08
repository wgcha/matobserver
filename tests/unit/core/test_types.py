"""계약 타입 전사 검사: 이름·필드·기본값, ParamSpace 연산, NeutralMaterial 직렬화·불변식, Protocol 형태."""

from __future__ import annotations

import dataclasses
import typing
from pathlib import Path

import numpy as np
import pytest

from matobserver.core import types as T
from matobserver.core.types import (
    Constraint,
    ElasticBlock,
    NeutralMaterial,
    ParamSpace,
    ParamSpec,
    RateTableBlock,
    RateTableRow,
    TableRow,
    neutral_material_violations,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts"

# plugins.md §4 / testspec.md §3 / module-boundaries.md §4.1 의 필드 이름 (선언 순서 그대로)
FIELDS = {
    "Issue": ["code", "message", "severity", "field", "context"],
    "ParamSpec": ["name", "unit", "lower", "upper", "initial", "scale", "stage", "description"],
    "Constraint": ["kind", "params", "coeffs", "bound"],
    "ParamSpace": ["params", "constraints"],
    "ElasticBlock": ["youngs_modulus", "poisson_ratio", "density"],
    "TableRow": ["plastic_strain", "true_stress"],
    "RateTableRow": ["strain_rate", "plastic_strain", "true_stress"],
    "RateTableBlock": ["values", "rows"],
    "NeutralMaterial": ["elastic", "table", "rate_table", "extensions"],
    "ExtractorRef": ["name", "args"],
    "ResponseWindow": ["x_min", "x_max", "split", "region_weights"],
    "ResponseDef": ["response_key", "kind", "test_channel", "axis", "extractor", "weight", "window"],
    "EvalOutcome": ["J", "penalized", "components"],
    "StopSignal": ["stop", "reason"],
    "SlotBinding": ["slot", "part_ids", "mid", "secid", "section", "lcid"],
    "OutputRequest": ["dt_out_s", "dt_plot_s", "dt_max_s", "history_nodes"],
    "SimulationCase": ["case_id", "test_key", "recipe_ref", "recipe_dir", "manifest", "params_si",
                       "engine_params_si", "specimen_mesh", "jig_mesh", "slots", "responses",
                       "analysis_primary", "analysis_fallback", "output"],
    "PreparedRun": ["workdir", "case", "main_file", "mode"],
    "RunStatus": ["state", "mode_used", "fallback_used", "elapsed_s", "termination_message",
                  "solver_version", "license_waits", "warnings"],
    "CurveData": ["x", "y", "x_quantity", "y_quantity"],
    "Extracted": ["curves", "scalars", "field_max", "energy"],
    "SolverCapabilities": ["analysis_modes", "extractors", "material_keywords", "extension_keys",
                           "unit_system", "supports_overrides", "card_format"],
    "RunOverrides": ["section_by_slot", "forbid_mass_scaling", "engine_params_si"],
    "MaterialCapabilities": ["solvers", "solver_keyword", "stages", "produces_table", "rate_dependent"],
    "ReferencePointRequest": ["name", "locate"],
    "GeometrySpec": ["kind", "dimensions", "layers", "stack_axis", "stp_path", "placement",
                     "region_part_ids", "reference_points"],
    "MeshOptions": ["topology", "target_size_m", "min_elems_through_thickness", "max_elements", "size_factor",
                    "symmetry", "node_id_range", "element_id_range", "part_set_id", "node_set_ids"],
    "MeshQuality": ["min_scaled_jacobian", "max_aspect_ratio", "elems_through_thickness"],
    "MeshResult": ["mesh_path", "node_count", "element_count", "regions", "volume_m3", "reference_volume_m3",
                   "bbox_m", "reference_nodes", "node_sets", "top_z_at_load_m", "bottom_z_m", "symmetry",
                   "quality", "target_size_used_m", "warnings"],
    "JigSpec": ["generator", "params_si", "part_ids", "mids", "rigid", "specimen", "symmetry",
                "node_id_range", "element_id_range"],
    "JigResult": ["mesh_path", "reference_nodes", "part_ids"],
    "RunRecord": ["run_id", "test_type_key", "channels", "specimen_dimensions", "conditions", "instrument",
                  "specimen_id", "material_id", "source_sha256", "fetched_hash", "scalars"],
    "TestData": ["run_id", "x_quantity", "x", "y", "displacement_source", "compliance_m_per_n", "scalars",
                 "dimensions", "meta"],
    "RateInfo": ["value_1_s", "formula", "inputs", "velocity_source"],
    "ResolvedRecipe": ["recipe_ref", "content_hash", "store_dir", "manifest"],
    "ResolvedTest": ["test_key", "test_type", "recipe", "geometry", "runs", "displacement_channel",
                     "recipe_params_si", "user_window", "weight"],
    "ResolvedMaterials": ["mode", "slots"],
    "CalibrationSpec": ["job_id", "schema_version", "tests", "materials", "optimizer", "elastic_optimizer",
                        "solver", "friction", "budget", "seed", "request_hash", "plasticity_mode", "explicit_check"],
}

PROTOCOL_MEMBERS = {
    "Optimizer": ({"name", "version", "max_batch"},
                  {"setup", "ask", "tell", "suggest_stop", "best", "state", "restore"}),
    "SolverAdapter": ({"name", "version"},
                      {"capabilities", "prepare", "run", "extract", "solver_version", "cleanup",
                       "single_element_uniaxial", "render_card"}),
    "MaterialModel": ({"name", "version"},
                      {"bind", "param_space", "to_neutral", "capabilities", "initial_guess", "describe"}),
    "GeometryMesher": ({"name", "version", "role"}, {"validate", "mesh"}),
    "TestSpec": ({"name", "version", "matnexus_test_type_keys"},
                 {"allowed_geometries", "required_channels", "required_dimensions", "validate", "map_run",
                  "param_space", "param_targets", "responses", "analysis_mode", "reference_points",
                  "initial_hints", "reference_strain_rate", "engine_params"}),
}


@pytest.mark.parametrize("name,expected", sorted(FIELDS.items()))
def test_dataclass_fields_match_contract(name, expected):
    cls = getattr(T, name)
    assert dataclasses.is_dataclass(cls)
    assert [f.name for f in dataclasses.fields(cls)] == expected
    # 계약: frozen
    params = cls.__dataclass_params__
    assert params.frozen


def test_defaults_match_contract():
    mo = T.MeshOptions(topology="hex8", target_size_m=1e-3, min_elems_through_thickness=3, max_elements=1000)
    assert (mo.size_factor, mo.symmetry, mo.node_id_range, mo.element_id_range, mo.part_set_id) == (
        1.0, "none", (1, 4_999_999), (1, 4_999_999), 0)
    assert mo.node_set_ids == {}
    gs = T.GeometrySpec(kind="box", dimensions={"width": 0.01})
    assert gs.stack_axis == "z" and gs.stp_path is None and gs.layers == () and gs.placement == {}
    assert gs.reference_points == () and gs.region_part_ids == {}
    iss = T.Issue("X", "m", "error")
    assert iss.field is None and iss.context == {}
    c = Constraint("monotonic_nondecreasing", ("a", "b"))
    assert c.coeffs == () and c.bound == 0.0
    assert T.ParamSpec("E", "Pa", 1.0, 2.0, None, "log", "elastic").description == ""
    assert T.EvalOutcome(1.0, False).components == {}
    assert T.RunOverrides({}, True).engine_params_si is None
    assert T.RunRecord("r", "flexural", {}, {}, {}, None, None, None, None, "h").scalars == {}
    jig = T.JigSpec("jig.bending3p_rollers", {}, {}, {}, {}, None, "quarter")  # type: ignore[arg-type]
    assert jig.node_id_range == (5_000_000, 5_999_999) and jig.element_id_range == (5_000_000, 5_999_999)


def test_frozen_instances_reject_mutation():
    e = ElasticBlock(1.0, 0.3, 7850.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.youngs_modulus = 2.0  # type: ignore[misc]


def test_run_state_values():
    assert {s.name: s.value for s in T.RunState} == {
        "NORMAL": "normal", "ERROR": "error", "TIMEOUT": "timeout", "DIVERGED": "diverged",
        "LICENSE_TIMEOUT": "license_timeout", "CANCELLED": "cancelled"}
    assert T.RunState.NORMAL == "normal"  # str Enum


def test_type_aliases():
    assert typing.get_args(T.Stage) == ("elastic", "plastic")


def test_testdata_not_collected_by_pytest():
    assert T.TestData.__test__ is False


# ---------------------------------------------------------------- ParamSpace
def _space() -> ParamSpace:
    return ParamSpace(
        params=(
            ParamSpec("E", "Pa", 1e9, 1e11, None, "log", "elastic"),
            ParamSpec("sigma_y", "Pa", 1e7, 1e9, 1e8, "log", "plastic"),
            ParamSpec("dsig_1", "Pa", 0.0, 1e8, None, "linear", "plastic"),
        ),
        constraints=(
            Constraint("monotonic_nondecreasing", ("sigma_y", "dsig_1")),
            Constraint("linear_le", ("E",), (1.0,), 5e10),
        ),
    )


def test_param_space_names_subset_qualify():
    sp = _space()
    assert sp.names() == ("E", "sigma_y", "dsig_1")
    el = sp.subset("elastic")
    assert el.names() == ("E",)
    assert [c.kind for c in el.constraints] == ["linear_le"]   # 모든 변수가 남은 제약만
    pl = sp.subset("plastic")
    assert pl.names() == ("sigma_y", "dsig_1")
    assert [c.kind for c in pl.constraints] == ["monotonic_nondecreasing"]
    q = sp.qualify("body")
    assert q.names() == ("body.E", "body.sigma_y", "body.dsig_1")
    assert q.constraints[0].params == ("body.sigma_y", "body.dsig_1")
    assert sp.names() == ("E", "sigma_y", "dsig_1")  # 원본 불변
    assert sp.qualify("a").qualify("b").names()[0] == "b.a.E"


def test_param_name_pattern():
    ok = ["E", "sigma_y", "dsig_12", "a" * 16]
    bad = ["", "1E", "_x", "a" * 17, "a.b", "a-b"]
    assert all(T.PARAM_NAME_RE.match(n) for n in ok)
    assert not any(T.PARAM_NAME_RE.match(n) for n in bad)


# ---------------------------------------------------------------- NeutralMaterial
def _mat(table=True) -> NeutralMaterial:
    rows = (TableRow(0.0, 4.1e7), TableRow(0.01, 4.8e7)) if table else None
    return NeutralMaterial(ElasticBlock(2.31e9, 0.38, 1200.0), table=rows)


def test_to_card_blocks_matches_contract_example():
    # plugins.md §1 JSON 예
    assert _mat().to_card_blocks() == {
        "elastic": {"youngs_modulus": 2.31e9, "poisson_ratio": 0.38, "density": 1200.0},
        "table": [{"plastic_strain": 0.0, "true_stress": 4.1e7}, {"plastic_strain": 0.01, "true_stress": 4.8e7}],
        "rate_table": None,
    }


def test_card_blocks_key_sets_contract_m4():
    cb = _mat().to_card_blocks()
    assert set(cb) == {"elastic", "table", "rate_table"}
    assert set(cb["elastic"]) == {"youngs_modulus", "poisson_ratio", "density"}
    assert all(set(r) == {"plastic_strain", "true_stress"} for r in cb["table"])
    assert _mat(table=False).to_card_blocks()["table"] is None


def test_card_blocks_rate_table_shape():
    m = NeutralMaterial(
        ElasticBlock(2e11, 0.3, 7850.0), table=(TableRow(0.0, 3e8), TableRow(0.1, 4e8)),
        rate_table=RateTableBlock({"model": "cowper_symonds", "cs_d": 40.4, "cs_p": 5.0, "rate_count": 0},
                                  (RateTableRow(0.001, 0.0, 3.0e8),)))
    rt = m.to_card_blocks()["rate_table"]
    assert rt == {"values": {"model": "cowper_symonds", "cs_d": 40.4, "cs_p": 5.0, "rate_count": 0},
                  "rows": [{"strain_rate": 0.001, "plastic_strain": 0.0, "true_stress": 3.0e8}]}


def test_card_blocks_are_plain_python_and_json_serializable():
    import json

    m = NeutralMaterial(ElasticBlock(np.float64(2e9), np.float64(0.3), np.float64(1000.0)),
                        table=(TableRow(np.float64(0.0), np.float64(1e7)), TableRow(np.float64(0.1), np.float64(2e7))))
    cb = m.to_card_blocks()
    json.dumps(cb)
    assert type(cb["elastic"]["youngs_modulus"]) is float


def test_neutral_material_invariants():
    assert neutral_material_violations(_mat()) == []
    assert neutral_material_violations(_mat(table=False)) == []

    def v(**kw):
        e = dict(youngs_modulus=2e9, poisson_ratio=0.3, density=1000.0)
        rows = kw.pop("rows", ((0.0, 1e7), (0.1, 2e7)))
        e.update(kw)
        return neutral_material_violations(
            NeutralMaterial(ElasticBlock(**e), table=None if rows is None else tuple(TableRow(*r) for r in rows)))

    assert v(youngs_modulus=0.0) and v(youngs_modulus=-1.0) and v(youngs_modulus=float("inf"))
    assert v(youngs_modulus=float("nan"))
    assert v(poisson_ratio=0.5) and v(poisson_ratio=-1.0) and v(poisson_ratio=0.49) == []
    assert v(poisson_ratio=-0.99) == []
    assert v(density=0.0)
    assert v(rows=((0.0, 1e7),))                       # 1행
    assert v(rows=((0.001, 1e7), (0.1, 2e7)))          # 첫 행 εp != 0
    assert v(rows=((0.0, 1e7), (0.0, 2e7)))            # 변형률 비엄격 증가
    assert v(rows=((0.0, 1e7), (0.1, 5e6)))            # 응력 감소
    assert v(rows=((0.0, 0.0), (0.1, 5e6)))            # 응력 0
    assert v(rows=((0.0, 1e7), (0.1, 1e7))) == []      # 비감소(동일)는 허용
    assert v(rows=None) == []


# ---------------------------------------------------------------- Protocol
@pytest.mark.parametrize("name,expected", sorted(PROTOCOL_MEMBERS.items()))
def test_protocol_members_match_contract(name, expected):
    from matobserver.core.registry import required_members

    attrs, methods = required_members(getattr(T, name))
    assert (set(attrs), set(methods)) == expected


def test_cancel_token_protocol():
    class C:
        def is_set(self) -> bool:
            return False

    assert isinstance(C(), T.CancelToken)
    assert not isinstance(object(), T.CancelToken)


def test_runtime_checkable_protocols_accept_conforming_instance():
    class Opt:
        name = "x"
        version = "1.0.0"
        max_batch = 1

        def __init__(self, options): ...
        def setup(self, space, seed, initial_points): ...
        def ask(self, n=1): ...
        def tell(self, thetas, outcomes): ...
        def suggest_stop(self): ...
        def best(self): ...
        def state(self): ...
        def restore(self, state): ...

    assert isinstance(Opt({}), T.Optimizer)
    assert not isinstance(object(), T.Optimizer)


def test_engine_services_protocol_members():
    hints = typing.get_type_hints(T.EngineServices, include_extras=False, localns={"Registry": object, "Config": object})
    assert {"registry", "config", "owner_kind", "owner_id", "cancel"} <= set(hints)
    for m in ("workdir", "add_artifact", "record_eval", "progress"):
        assert callable(getattr(T.EngineServices, m))
