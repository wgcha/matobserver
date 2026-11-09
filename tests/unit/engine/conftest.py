"""Impl-Optim 테스트 더블 경로 등록 (tests/fixtures/optim)."""

import sys
from pathlib import Path

_P = str(Path(__file__).resolve().parents[2] / "fixtures" / "optim")
if _P not in sys.path:
    sys.path.insert(0, _P)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: G2 시드 분포 시험(수십 초). 기본 실행 포함")
