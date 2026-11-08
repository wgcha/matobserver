"""`random` 최적화기 (시험용): 초기점 다음에는 균일 무작위 실현 가능점."""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

import numpy as np

from matobserver.core.types import StopSignal

from ._common import BaseOptimizer


class RandomSearch(BaseOptimizer):
    name: ClassVar[str] = "random"
    version: ClassVar[str] = "1.0.0"
    max_batch: ClassVar[int] = 64

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        super().__init__(options)

    def _propose(self) -> np.ndarray:
        return self._random_feasible()

    def suggest_stop(self) -> StopSignal:
        return StopSignal(stop=False, reason="none")
