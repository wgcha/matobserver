"""B-20: 버전 일관성 — `__version__`·pyproject 일치, CONTRACT_SCHEMA_VERSION == 1.3, health·--version 노출 (deploy.md §3.1)."""

from __future__ import annotations

import io
import tomllib
from pathlib import Path

import pytest

import matobserver
from matobserver import cli

REPO = Path(__file__).resolve().parents[2]


def test_package_version_matches_pyproject():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == matobserver.__version__
    parts = matobserver.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)  # SemVer X.Y.Z


def test_contract_schema_version_is_1_3():
    assert matobserver.CONTRACT_SCHEMA_VERSION == "1.3"
    deploy = REPO / "docs" / "contracts" / "deploy.md"
    if deploy.exists():
        assert deploy.read_text(encoding="utf-8").startswith(f"schema_version: {matobserver.CONTRACT_SCHEMA_VERSION}")


def test_health_exposes_package_and_contract_versions(env):
    body = env.client.get("/v1/health").json()
    assert body["version"] == matobserver.__version__ and body["schema_version"] == matobserver.CONTRACT_SCHEMA_VERSION


def test_cli_version_flag_shows_both(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.build_parser().parse_args(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert matobserver.__version__ in out and matobserver.CONTRACT_SCHEMA_VERSION in out


def test_error_body_and_status_carry_contract_version(env):
    r = env.client.get("/v1/calibrations/cal_nope", headers=env.svc)
    assert r.json()["schema_version"] == matobserver.CONTRACT_SCHEMA_VERSION


def test_api_accepts_1_0_through_1_3_requests_only():
    from matobserver.api.schemas import SCHEMA_MAJOR, SCHEMA_MINOR

    assert (SCHEMA_MAJOR, SCHEMA_MINOR) == (1, 3)
