"""B-15: doctor D01-D11 항목별 OK/WARN/FAIL/SKIP, 종료 코드 0/1/2/3, --json 스키마 (deploy.md §2.2)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import jsonschema
import pytest

from matobserver.core.config import load_config
from matobserver.jobs import db as jdb
from matobserver.ops import doctor as D
from matobserver.ops.runtime import write_pidfile, pid_path

from ops_support import VERSION_CMD, write_config

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "contract_schema_version", "config_path", "summary", "checks"],
    "properties": {
        "version": {"type": "string"}, "contract_schema_version": {"const": "1.3"}, "config_path": {"type": ["string", "null"]},
        "summary": {"type": "object", "additionalProperties": False, "required": ["ok", "warn", "fail", "skip"],
                    "properties": {k: {"type": "integer"} for k in ("ok", "warn", "fail", "skip")}},
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "name", "status", "detail", "data"],
            "properties": {"id": {"pattern": "^D(0[1-9]|1[01])$"}, "name": {"type": "string"},
                           "status": {"enum": ["OK", "WARN", "FAIL", "SKIP", "INFO"]}, "detail": {"type": "string"}, "data": {"type": "object"}}}},
    },
}


def load(path, unknown=None):
    return load_config(path, env={}, unknown=unknown)


def cfg_with(tmp_path, **over):
    p = write_config(tmp_path, **over)
    return load(p), p


# ---- D01
def test_d01_ok_fail_warn_info(tmp_path):
    cfg, p = cfg_with(tmp_path)
    assert D.d01(cfg, [], p).status == D.INFO  # 파일에 없는 키는 코드 기본값 (INFO)
    full = tmp_path / "full.yaml"
    import yaml
    from matobserver.core.config import Config
    full.write_text(yaml.safe_dump({**cfg.to_dict()}), encoding="utf-8")
    assert D.d01(load(full), [], full).status == D.OK
    c2, p2 = cfg_with(tmp_path / "b", **{"solver.lsdyna.script": None})
    r = D.d01(c2, [], p2)
    assert r.status == D.FAIL and "solver.lsdyna.script" in r.detail
    c3, p3 = cfg_with(tmp_path / "c", **{"auth.admin_tokens": []})
    assert D.d01(c3, [], p3).status == D.FAIL
    assert D.d01(cfg, ["foo.bar"], p).status == D.WARN
    c4, p4 = cfg_with(tmp_path / "d", **{"engine.plastic_stage.optimizer": "bo"})
    r = D.d01(c4, [], p4)
    assert r.status == D.WARN and "별칭" in r.detail


def test_d01_unknown_keys_collected_by_lenient_load(tmp_path):
    p = write_config(tmp_path, **{"worker.bogus": 1})
    unknown: list[str] = []
    cfg = load(p, unknown)
    assert unknown == ["worker.bogus"] and D.d01(cfg, unknown, p).status == D.WARN


# ---- D02
def test_d02(tmp_path, monkeypatch):
    cfg, p = cfg_with(tmp_path)
    assert D.d02(cfg, p).status == D.OK
    monkeypatch.setattr(D.sys, "version_info", (3, 10, 0, "final", 0))
    assert D.d02(cfg, p).status == D.FAIL
    monkeypatch.undo()
    (tmp_path / "install.json").write_text(json.dumps({"package_version": "9.9.9"}), encoding="utf-8")
    assert D.d02(cfg, p).status == D.WARN
    from matobserver import __version__
    (tmp_path / "install.json").write_text(json.dumps({"package_version": __version__}), encoding="utf-8")
    assert D.d02(cfg, p).status == D.OK


# ---- D03
def test_d03(tmp_path):
    cfg, _ = cfg_with(tmp_path)
    assert D.d03(cfg).status == D.OK
    bad, _ = cfg_with(tmp_path / "x", **{"solver.lsdyna.script": str(tmp_path / "nope.bat")})
    assert D.d03(bad).status == D.FAIL
    txt = tmp_path / "run.txt"
    txt.write_text("x")
    bad2, _ = cfg_with(tmp_path / "y", **{"solver.lsdyna.script": str(txt)})
    assert D.d03(bad2).status == D.FAIL


# ---- D04
def test_d04(tmp_path, ops):
    env = ops.env()
    env.run = __import__("subprocess").run
    ok, _ = cfg_with(tmp_path)
    r = D.d04(ok, env)
    assert r.status == D.OK and r.data["version"].startswith("R13")
    none, _ = cfg_with(tmp_path / "a", **{"solver.lsdyna.version_command": None})
    assert D.d04(none, env).status == D.WARN
    nomatch, _ = cfg_with(tmp_path / "b", **{"solver.lsdyna.version_command": [sys.executable, "-c", "print('hello')"]})
    assert D.d04(nomatch, env).status == D.WARN
    broken, _ = cfg_with(tmp_path / "c", **{"solver.lsdyna.version_command": [str(tmp_path / "no-such-binary")]})
    assert D.d04(broken, env).status == D.WARN


# ---- D05
def test_d05(tmp_path):
    cfg, _ = cfg_with(tmp_path)
    assert D.d05(cfg).status == D.OK
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad, _ = cfg_with(tmp_path / "x", **{"paths.work_dir": str(blocker / "work")})
    assert D.d05(bad).status == D.FAIL
    low, _ = cfg_with(tmp_path / "y", **{"doctor.min_free_bytes": 2**62})
    assert D.d05(low).status == D.WARN


# ---- D06
def test_d06(tmp_path):
    cfg, _ = cfg_with(tmp_path)
    db = cfg.path("jobstore.sqlite_path")
    assert D.d06(cfg).status == D.OK  # DB 없음
    jdb.migrate(db)
    r = D.d06(cfg)
    assert r.status == D.OK and r.data["user_version"] == jdb.SCHEMA_VERSION
    c = sqlite3.connect(db)
    c.execute(f"PRAGMA user_version={jdb.SCHEMA_VERSION - 1}")
    c.close()
    assert D.d06(cfg).status == D.WARN
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version=99")
    c.close()
    assert D.d06(cfg).status == D.FAIL
    db.write_bytes(b"this is not sqlite" * 100)
    assert D.d06(cfg).status == D.FAIL


# ---- D07 / D08
def test_d07_d08(tmp_path, monkeypatch):
    cfg, _ = cfg_with(tmp_path)
    assert D.d07(cfg).status == D.OK
    bad_dir = tmp_path / "plugins"
    bad_dir.mkdir()
    (bad_dir / "x.yaml").write_text("schema_version: '1.0'\nplugins: {}\n")
    bad, _ = cfg_with(tmp_path / "x", **{"registry.paths": [str(bad_dir)]})
    assert D.d07(bad).status == D.FAIL
    assert D.d08().status == D.OK
    monkeypatch.setitem(sys.modules, "gmsh", None)
    assert D.d08().status == D.FAIL


# ---- D09
def test_d09(tmp_path, ops):
    cfg, _ = cfg_with(tmp_path)
    assert D.d09(cfg, ops.env()).status == D.SKIP
    mn, _ = cfg_with(tmp_path / "m", **{"matnexus.base_url": "http://mn.test", "matnexus.token": "tok", "matnexus.timeout_s": 10})
    ops.http = {"mn.test": (200, {}, 0.1)}
    assert D.d09(mn, ops.env()).status == D.OK
    ops.http = {"mn.test": (200, {}, 6.0)}
    assert D.d09(mn, ops.env()).status == D.WARN
    ops.http = {"mn.test": (401, None, 0.1)}
    assert D.d09(mn, ops.env()).status == D.FAIL
    ops.http = {}
    assert D.d09(mn, ops.env()).status == D.FAIL


# ---- D10
def test_d10(tmp_path, ops):
    cfg, _ = cfg_with(tmp_path)
    assert D.d10(cfg, ops.env(), bind=lambda h, p: True).status == D.OK
    assert D.d10(cfg, ops.env(), bind=lambda h, p: False).status == D.FAIL
    write_pidfile(pid_path(cfg, "server"), os.getpid(), "process")
    ops.http = {"/v1/health": (200, {"status": "ok"}, 0.0)}
    assert D.d10(cfg, ops.env(), bind=lambda h, p: False).status == D.OK
    ops.http = {}
    assert D.d10(cfg, ops.env(), bind=lambda h, p: True).status == D.FAIL


# ---- D11
def test_d11(tmp_path, ops):
    cfg, p = cfg_with(tmp_path)
    assert D.d11(cfg, p, ops.env()).status == D.SKIP
    sec = tmp_path / "secrets"
    sec.mkdir()
    tok = sec / "service.token"
    tok.write_text("t")
    if os.name == "posix":
        tok.chmod(0o600)
        assert D.d11(cfg, p, ops.env()).status == D.OK
        tok.chmod(0o644)
        assert D.d11(cfg, p, ops.env()).status == D.WARN
    ops.run_stdout = "service.token NT AUTHORITY\\SYSTEM:(F)\n  ME\\user:(F)\n"
    assert D.d11(cfg, p, ops.env(windows=True)).status == D.OK
    ops.run_stdout = "service.token Everyone:(R)\n"
    assert D.d11(cfg, p, ops.env(windows=True)).status == D.WARN


# ---- 종료 코드와 JSON
def test_exit_codes_and_json_schema(tmp_path, ops, monkeypatch):
    monkeypatch.setattr(D, "can_bind", lambda h, p: True)
    import io
    from matobserver import cli
    from matobserver.ops.service import OpsEnv

    def run(cfgp, *extra):
        out = io.StringIO()
        code = cli.main(["doctor", "--json", "--config", str(cfgp), *extra], env={}, stdout=out, stderr=io.StringIO(),
                        ops_env=OpsEnv(run=__import__("subprocess").run, http_get=ops.http_get, environ={}))
        return code, json.loads(out.getvalue())

    code, rep = run(ops.config_path)
    jsonschema.validate(rep, SCHEMA)
    assert code == 0, [c for c in rep["checks"] if c["status"] in ("WARN", "FAIL")]
    assert rep["summary"]["fail"] == 0 and rep["summary"]["warn"] == 0
    warn = write_config(tmp_path / "w", **{"solver.lsdyna.version_command": None})
    code, rep = run(warn)
    assert code == 1 and rep["summary"]["warn"] >= 1 and rep["summary"]["fail"] == 0
    fail = write_config(tmp_path / "f", **{"solver.lsdyna.script": None})
    code, rep = run(fail)
    assert code == 2 and rep["summary"]["fail"] >= 1
    code, rep = run(tmp_path / "missing.yaml")
    jsonschema.validate(rep, SCHEMA)
    assert code == 3 and rep["checks"][0]["status"] == "FAIL" and all(c["status"] == "SKIP" for c in rep["checks"][1:])
    assert [c["id"] for c in rep["checks"]] == [f"D{i:02d}" for i in range(1, 12)]


def test_doctor_human_table_has_every_check(ops):
    code, out, _ = ops.cli("doctor")
    for i in range(1, 12):
        assert f"D{i:02d}" in out
