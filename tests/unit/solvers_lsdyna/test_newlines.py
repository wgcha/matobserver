"""Windows LS-DYNA 로그(CRLF·CR)에서도 종료 판정·버전·관통·라이선스 판독이 같다."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from matobserver.core.config import Config
from matobserver.core.types import RunState
from matobserver.solvers.lsdyna.adapter import DEFAULT_DIVERGENCE_PATTERNS, DEFAULT_PENETRATION_PATTERN
from matobserver.solvers.lsdyna.runner import run_batch
from matobserver.solvers.lsdyna.termination import initial_penetration_mm, judge, read_logs

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "solver"
LOGS = _FIX / "logs"
CFG = Config()


def _support():
    if "solver_support" not in sys.modules:
        spec = importlib.util.spec_from_file_location("solver_support", _FIX / "solver_support.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["solver_support"] = mod
        spec.loader.exec_module(mod)
    return sys.modules["solver_support"]


def _judge(logs):
    return judge(logs, termination_patterns=CFG.get("solver.lsdyna.termination_patterns"),
                 license_patterns=CFG.get("solver.lsdyna.license_error_patterns"),
                 divergence_patterns=DEFAULT_DIVERGENCE_PATTERNS, version_pattern=CFG.get("solver.lsdyna.version_pattern"))


@pytest.mark.parametrize("nl", [b"\r\n", b"\r"])
@pytest.mark.parametrize("name", sorted(p.name for p in LOGS.glob("*.txt")))
def test_log_verdicts_newline_independent(tmp_path, nl, name):
    raw = (LOGS / name).read_bytes()
    (tmp_path / "d3hsp").write_bytes(raw.replace(b"\n", nl))
    crlf = _judge(read_logs(tmp_path))
    lf = _judge({"d3hsp": raw.decode()})
    assert (crlf.verdict, crlf.version) == (lf.verdict, lf.version)
    assert "\r" not in (crlf.version or "") and "\r" not in crlf.message
    assert _judge({"d3hsp": raw.replace(b"\n", nl).decode()}).verdict == lf.verdict  # 원문 문자열을 직접 줘도 같다
    assert initial_penetration_mm(raw.replace(b"\n", nl).decode(), DEFAULT_PENETRATION_PATTERN) == \
           initial_penetration_mm(raw.decode(), DEFAULT_PENETRATION_PATTERN)


def test_fake_run_with_crlf_logs(tmp_path, monkeypatch):
    ss = _support()

    class Never:
        def is_set(self):
            return False

    a = ss.fake_adapter()
    prep = a.prepare(ss.make_case("bending3p-param", tmp_path), {"body": ss.body_material()}, tmp_path / "run")
    ss.set_behavior(monkeypatch, tmp_path, {"crlf_logs": True})
    r = run_batch(a.runner_settings(), prep.main_file, 1, 30, Never(), "implicit")
    assert b"\r\n" in (prep.workdir / "d3hsp").read_bytes()
    assert r.state is RunState.NORMAL and r.version == "R13.1.1"
    ss.set_behavior(monkeypatch, tmp_path, {"crlf_logs": True, "implicit": "license"})
    assert run_batch(a.runner_settings(), prep.main_file, 1, 30, Never(), "implicit").state is RunState.LICENSE_TIMEOUT
