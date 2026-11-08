"""식별 엔진 (Impl-Optim). 진입점 `run_calibration`·`preflight` (module-boundaries.md §4.1).

`engine.alignment`·`engine.objective`는 I/O·레지스트리 의존이 없는 순수 모듈이다(§4.6, `verify/`가 import 가능).
이 패키지 초기화는 가볍게 유지한다(순수 모듈 import 시 run 계열을 끌어오지 않도록 지연 import).
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_calibration", "preflight", "CalibrationCancelled"]


def __getattr__(name: str) -> Any:
    if name in ("run_calibration", "preflight"):
        from . import run as _run

        return getattr(_run, name)
    if name == "CalibrationCancelled":
        from .evaluate import CalibrationCancelled

        return CalibrationCancelled
    raise AttributeError(name)
