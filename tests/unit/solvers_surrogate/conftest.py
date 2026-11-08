"""Impl-Optim 테스트 더블 경로 등록 (tests/fixtures/optim)."""

import sys
from pathlib import Path

_P = str(Path(__file__).resolve().parents[2] / "fixtures" / "optim")
if _P not in sys.path:
    sys.path.insert(0, _P)
