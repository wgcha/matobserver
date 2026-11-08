"""S-4 종료 판정: Normal/Error termination, 라이선스 오류, 수렴 실패, 버전, 초기 관통 (로그 픽스처)."""

from __future__ import annotations

from pathlib import Path

import pytest

from matobserver.core.config import Config
from matobserver.solvers.lsdyna.adapter import DEFAULT_DIVERGENCE_PATTERNS, DEFAULT_PENETRATION_PATTERN
from matobserver.solvers.lsdyna.termination import initial_penetration_mm, judge

LOGS = Path(__file__).resolve().parents[2] / "fixtures" / "solver" / "logs"
CFG = Config()


def _judge(d3hsp: str = "", messag: str = "", run_log: str = ""):
    logs = {k: v for k, v in (("d3hsp", d3hsp), ("messag", messag), ("mo_run.log", run_log)) if v}
    return judge(logs, termination_patterns=CFG.get("solver.lsdyna.termination_patterns"),
                 license_patterns=CFG.get("solver.lsdyna.license_error_patterns"),
                 divergence_patterns=DEFAULT_DIVERGENCE_PATTERNS, version_pattern=CFG.get("solver.lsdyna.version_pattern"))


@pytest.mark.parametrize("name, verdict, version", [
    ("normal_implicit.txt", "normal", "R13.1.1"),
    ("normal_mpp.txt", "normal", "R12.0.0"),
    ("error.txt", "error", "R13.1.1"),
    ("license.txt", "license", "R13.1.1"),
    ("diverged.txt", "diverged", "R13.1.1"),
    ("truncated.txt", "unknown", "R13.1.1"),
])
def test_log_fixtures(name, verdict, version):
    info = _judge(d3hsp=(LOGS / name).read_text())
    assert info.verdict == verdict
    assert info.version is not None and info.version.startswith(version)


def test_license_in_run_log_without_termination():
    assert _judge(run_log=(LOGS / "license_no_term.txt").read_text()).verdict == "license"


def test_license_word_in_header_is_not_a_license_error():
    header = "Licensed to: ACME license server\n" + "\n".join(f"line {i}" for i in range(100))
    assert _judge(d3hsp=header + "\n E r r o r    t e r m i n a t i o n\n").verdict == "error"
    assert _judge(d3hsp=header + "\n N o r m a l    t e r m i n a t i o n\n").verdict == "normal"


def test_error_wins_over_normal_and_messag_alone():
    assert _judge(messag=" N o r m a l    t e r m i n a t i o n").verdict == "normal"
    assert _judge(d3hsp="Normal termination", messag="Error termination").verdict == "error"


def test_initial_penetration_max_in_mm():
    text = (LOGS / "penetration.txt").read_text()
    assert initial_penetration_mm(text, DEFAULT_PENETRATION_PATTERN) == pytest.approx(3.5e-3)
    assert initial_penetration_mm((LOGS / "normal_implicit.txt").read_text(), DEFAULT_PENETRATION_PATTERN) is None
