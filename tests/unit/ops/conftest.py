"""운영 명령 시험 공용 픽스처 (ops_support: tests/fixtures/backend/ops_support.py)."""

import sys
from pathlib import Path

import pytest

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "backend"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from ops_support import *  # noqa: E402,F401,F403
from ops_support import Ops  # noqa: E402


@pytest.fixture
def ops(tmp_path):
    o = Ops(tmp_path)
    yield o
    o.close()
