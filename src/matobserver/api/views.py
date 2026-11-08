"""JobStore/RecipeRepository 행 -> 응답 본문 (api.md §4, §8). 서버 경로는 어디에도 넣지 않는다."""

from __future__ import annotations

from typing import Any, Mapping

from ..jobs.clock import iso, parse_iso
from ..jobs.store import RUNNING
from .schemas import SCHEMA_VERSION


def job_status(row: Mapping[str, Any], now: Any) -> dict[str, Any]:
    progress = row["progress"]
    if progress is not None and row["status"] == RUNNING and row.get("started_at"):
        progress = dict(progress)
        progress["elapsed_s"] = round((now - parse_iso(row["started_at"])).total_seconds(), 3)
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": row["job_id"], "status": row["status"], "phase": row["phase"], "iter": row["iter"], "best_J": row["best_j"],
        "progress": progress, "cancel_requested": row["cancel_requested"],
        "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
        "created_by": row["created_by"], "warnings": row["warnings"], "error": row["error"],
    }


def job_summary(row: Mapping[str, Any], tests: list[Mapping[str, str]]) -> dict[str, Any]:
    return {
        "job_id": row["job_id"], "name": row["name"], "status": row["status"], "phase": row["phase"],
        "test_types": sorted({t["test_type"] for t in tests}), "recipes": [t["recipe_ref"] for t in tests],
        "material_id": row["material_id"], "best_J": row["best_j"], "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


def job_created(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "job_id": row["job_id"], "status": row["status"], "created_at": row["created_at"],
            "warnings": row["warnings"]}


def page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": items, "total": total, "limit": limit, "offset": offset}


def recipe_admin_summary(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recipe_ref": r["recipe_ref"], "recipe_id": r["recipe_id"], "version": r["version"], "kind": r["kind"],
        "test_type": r["test_type"], "status": r["status"], "content_hash": r["content_hash"], "created_at": r["created_at"],
        "created_by": r["created_by"], "retired_at": r["retired_at"], "retire_reason": r["retire_reason"],
        "warnings_count": r["warnings_count"],
    }


def recipe_admin_detail(r: Mapping[str, Any], attempts: list[Mapping[str, Any]]) -> dict[str, Any]:
    d = recipe_admin_summary(r)
    d.update({
        "schema_version": SCHEMA_VERSION, "manifest": r["manifest"], "files": r["files"], "verification": r["verification"],
        "static_warnings": r["static_warnings"], "verification_id": r["verification_id"],
        "attempts": [
            {"verification_id": a["verification_id"], "status": a["status"], "content_hash": a["content_hash"],
             "created_at": a["created_at"], "verification": a["verification"]}
            for a in attempts if a["verification_id"] != r["verification_id"]
        ],
    })
    return d


def recipe_public_summary(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"recipe_ref": r["recipe_ref"], "recipe_id": r["recipe_id"], "version": r["version"], "kind": r["kind"],
            "test_type": r["test_type"], "title": r["title"], "standards": r["standards"],
            "compliance_available": r["compliance_available"]}


def recipe_public(r: Mapping[str, Any]) -> dict[str, Any]:
    m = r["manifest"]
    comp = m.get("compliance") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "recipe_ref": r["recipe_ref"], "kind": m["kind"], "test_type": m["test_type"], "title": m["title"],
        "standards": m.get("standards") or [], "unit_system": m["unit_system"],
        "parameters": [
            {"name": p["name"], "dimension": p["dimension"], "unit_si": p["unit_si"], "default": p["default"], "range": p["range"],
             "matnexus_key": p.get("matnexus_key")}
            for p in m["parameters"]
        ],
        "material_slots": [{"slot": s["slot"], "region": s["regions"]} for s in m["material_slots"]],
        "specimen": {"geometry_kinds": m["specimen"]["geometry_kinds"]},
        "responses": [{"response_key": x["response_key"], "test_channel": x["test_channel"], "axis": x.get("axis")} for x in m["responses"]],
        "compliance": {"available": comp.get("value_m_per_n") is not None, "value_m_per_n": comp.get("value_m_per_n")},
        "status": r["status"],
    }


def recipe_registered(r: Mapping[str, Any], warnings: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    d = {"schema_version": SCHEMA_VERSION, "recipe_ref": r["recipe_ref"], "recipe_id": r["recipe_id"], "version": r["version"],
         "content_hash": r["content_hash"], "status": r["status"], "verification_id": r["verification_id"]}
    if warnings:
        d["warnings"] = [dict(w) for w in warnings]
    return d


def now_iso(clock: Any) -> str:
    return iso(clock())
