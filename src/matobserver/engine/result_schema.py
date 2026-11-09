"""result.md 결과 본문 JSON Schema (draft 2020-12) — 계약 §1–§6 필드·타입을 옮긴 것 (O-10).

추가 필드는 허용(MINOR 확장 규칙)하고, 계약 표의 필수 필드·enum·타입만 강제한다.
"""

from __future__ import annotations

from typing import Any

_num = {"type": "number"}
_num_n = {"type": ["number", "null"]}
_str_n = {"type": ["string", "null"]}
_int_n = {"type": ["integer", "null"]}

ROW = {"type": "object", "required": ["plastic_strain", "true_stress"],
       "properties": {"plastic_strain": _num, "true_stress": _num}, "additionalProperties": False}

CARD_BLOCKS = {
    "type": "object", "required": ["elastic", "table", "rate_table"], "additionalProperties": False,
    "properties": {
        "elastic": {"type": "object", "required": ["youngs_modulus", "poisson_ratio", "density"], "additionalProperties": False,
                    "properties": {"youngs_modulus": {"type": "number", "exclusiveMinimum": 0},
                                   "poisson_ratio": _num, "density": {"type": "number", "exclusiveMinimum": 0}}},
        "table": {"oneOf": [{"type": "null"}, {"type": "array", "minItems": 2, "items": ROW}]},
        "rate_table": {"type": ["object", "null"]},
    },
}

PARAM_RESULT = {"type": "object", "required": ["value", "unit", "stage", "lower", "upper", "initial", "at_bound"],
                "properties": {"value": _num, "unit": {"type": "string"}, "stage": {"type": "string"}, "lower": _num,
                               "upper": _num, "initial": _num_n, "at_bound": {"type": "boolean"}}}

SLOT = {
    "type": "object",
    "required": ["slot", "role", "member_position", "material_model", "card_blocks", "identified_params", "fixed_params",
                 "valid_range", "extrapolation", "hardening", "derived", "regularization"],
    "properties": {
        "slot": {"type": "string"}, "role": {"enum": ["identify", "fixed"]}, "member_position": _int_n,
        "material_model": {"type": "object", "required": ["name", "version", "options"]},
        "card_blocks": CARD_BLOCKS,
        "identified_params": {"type": "object", "additionalProperties": PARAM_RESULT},
        "fixed_params": {"type": "object", "additionalProperties": {
            "type": "object", "required": ["value", "source"],
            "properties": {"source": {"enum": ["request.reference", "request.params", "config.default"]}}}},
        "valid_range": {"oneOf": [{"type": "null"}, {"type": "object", "required": ["max_plastic_strain", "source", "test_keys"]}]},
        "extrapolation": {"oneOf": [{"type": "null"}, {"type": "object",
                                                        "required": ["method", "beyond_plastic_strain",
                                                                     "rows_extrapolated_from_index"]}]},
        "hardening": {"type": "object", "required": ["law", "coefficients"],
                      "properties": {"law": {"enum": [None, "swift", "voce", "swift_voce"]}}},
        "derived": {"type": "object", "required": ["lsdyna_keyword"]},
        "regularization": {"oneOf": [{"type": "null"}, {"type": "object", "required": ["kind", "weight", "J_reg"]}]},
    },
}

WARNING = {"type": "object", "required": ["code", "message"], "properties": {"code": {"type": "string"},
                                                                             "message": {"type": "string"}}}

ARTIFACT = {"type": "object", "required": ["artifact_id", "kind", "media_type", "filename", "bytes", "sha256"],
            "properties": {"bytes": {"type": "integer"}, "kind": {"type": "string"}}}

METADATA = {
    "type": "object",
    "required": ["test_types", "matnexus_test_type_keys", "tests", "assumptions", "residuals", "uncertainty",
                 "identifiability", "compliance", "equivalence", "termination", "roundtrip", "warnings", "traceability",
                 "explicit_check", "rate"],
    "properties": {
        "test_types": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "object", "required": [
            "test_key", "test_type", "recipe_ref", "recipe_params", "geometry", "displacement_channel", "runs", "cases",
            "window", "identified_params", "weight"]}},
        "assumptions": {"type": "array", "items": {"type": "object", "required": ["code", "text"]}},
        "residuals": {"type": "object", "required": ["J", "J_stage", "per_test", "per_run"],
                      "properties": {"J": _num,
                                     "per_run": {"type": "array", "items": {"type": "object", "required": [
                                         "run_id", "test_key", "J", "rmse_N", "max_abs_err_N", "nrmse", "shortfall"]}}}},
        "uncertainty": {"type": "object", "required": ["method", "per_run", "statistics", "notes"],
                        "properties": {"method": {"enum": ["per_specimen_scaling", "per_specimen_refit", "none"]}}},
        "identifiability": {"type": "object", "required": ["precheck", "post_fit", "flags", "recommendations"],
                            "properties": {
                                "precheck": {"type": "object", "required": ["n_params", "n_configs", "n_points_plastic",
                                                                            "verdict", "codes"],
                                             "properties": {"verdict": {"enum": ["ok", "warn", "underdetermined"]}}},
                                "post_fit": {"type": "array", "items": {"type": "object", "required": [
                                    "stage", "params", "normalized_sensitivity", "correlation", "relative_stdev",
                                    "relative_stdev_is_lower_bound", "condition_number", "evaluations"],
                                    "properties": {"relative_stdev_is_lower_bound": {"const": True}}}}}},
        "compliance": {"type": "array", "items": {"type": "object", "required": [
            "test_key", "displacement_channel", "applied", "value_m_per_n", "reason", "warnings"],
            "properties": {"reason": {"enum": [None, "missing", "deflectometer"]}}}},
        "equivalence": {"type": "object", "required": ["mode", "load_mode", "configurations", "note"]},
        "termination": {"type": "object", "required": ["reason", "evaluations", "evaluations_failed", "wall_time_s",
                                                       "per_stage", "final_stage", "e_by_stage"],
                        "properties": {"reason": {"enum": ["budget", "stagnation", "converged", "wall_time"]}}},
        "roundtrip": {"type": "object", "required": ["enabled", "per_slot"],
                      "properties": {"per_slot": {"type": "array", "items": {"type": "object", "required": [
                          "slot", "passed", "max_rel_err", "tolerance", "artifact_id"]}}}},
        "warnings": {"type": "array", "items": WARNING},
        "traceability": {"type": "object", "required": ["input_hash", "request_hash", "run_data_hashes", "recipes", "solver",
                                                        "plugins", "matobserver_version", "config_hash", "seed",
                                                        "matnexus"]},
        "explicit_check": {"type": "object", "required": ["enabled"],
                           "properties": {"per_case": {"type": "array", "items": {"type": "object", "required": [
                               "test_key", "case_id", "status", "run_state", "ke_ie_ratio_max", "vs_implicit",
                               "vs_experiment", "within_tolerance", "artifact_ids"],
                               "properties": {"status": {"enum": ["ok", "deviation", "failed", "skipped"]}}}}}},
        "rate": {"type": "object", "required": ["reference_strain_rate_1_s", "rate_dependence_identified", "rate_model",
                                                "rate_coefficients", "identified_rate_range_1_s", "advisory"],
                 "properties": {"rate_dependence_identified": {"const": False}}},
    },
}

RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "matobserver calibration result 1.0",
    "type": "object",
    "required": ["schema_version", "job_id", "status", "created_at", "finished_at", "units", "slots", "metadata", "artifacts"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "job_id": {"type": "string"},
        "status": {"const": "succeeded"},
        "units": {"type": "object", "required": ["length", "force", "stress", "strain", "density", "time", "compliance"]},
        "slots": {"type": "array", "minItems": 1, "items": SLOT},
        "metadata": METADATA,
        "artifacts": {"type": "array", "items": ARTIFACT},
    },
}
