"""Backend 공용 픽스처를 불러온다 (be_support: tests/fixtures/backend/be_support.py)."""

import sys
from pathlib import Path

_FIX = Path(__file__).resolve().parents[3 - 1] / "fixtures" / "backend"
if str(_FIX) not in sys.path:
    sys.path.insert(0, str(_FIX))

from be_support import *  # noqa: E402,F401,F403
