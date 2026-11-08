"""다른 소유자(엔진·검증기)의 진입점을 느슨하게 붙이는 어댑터.

* `run_calibration(spec, services) -> Mapping`      : `matobserver.engine.run:run_calibration` (module-boundaries.md §4.1)
* `preflight(spec_draft, services) -> list[Issue]`  : `matobserver.engine:preflight` 또는 `matobserver.engine.preflight:preflight` (§4.1, 순수 함수)
* `verify_recipe(recipe, services, strict) -> dict` : `matobserver.verify:verify_recipe` 또는 `matobserver.verify.recipe:verify_recipe` (§4.2)

엔진·검증기 구현이 아직 없으면 해당 필드는 None 이고, 호출하는 쪽이 사유를 남긴다 (preflight 는 건너뜀, run/verify 는 잡·레시피 실패).
시험에서는 테스트 대역을 직접 주입한다.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

RunCalibration = Callable[[Any, Any], Mapping[str, Any]]
Preflight = Callable[[Any, Any], Sequence[Any]]
VerifyRecipe = Callable[[Any, Any, bool], Mapping[str, Any]]

_RUN = (("matobserver.engine.run", "run_calibration"), ("matobserver.engine", "run_calibration"))
_PREFLIGHT = (("matobserver.engine", "preflight"), ("matobserver.engine.preflight", "preflight"))
_VERIFY = (("matobserver.verify", "verify_recipe"), ("matobserver.verify.recipe", "verify_recipe"))


def _resolve(candidates: Sequence[tuple[str, str]]) -> Callable[..., Any] | None:
    for mod_name, attr in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(mod, attr, None)
        if callable(fn):
            return fn
    return None


@dataclass
class EngineHooks:
    run_calibration: RunCalibration | None = None
    preflight: Preflight | None = None
    verify_recipe: VerifyRecipe | None = None

    @classmethod
    def discover(cls) -> "EngineHooks":
        return cls(run_calibration=_resolve(_RUN), preflight=_resolve(_PREFLIGHT), verify_recipe=_resolve(_VERIFY))

    def with_missing_discovered(self) -> "EngineHooks":
        """주입되지 않은 항목만 import 로 찾아 채운다."""
        found = EngineHooks.discover()
        return EngineHooks(
            run_calibration=self.run_calibration or found.run_calibration,
            preflight=self.preflight or found.preflight,
            verify_recipe=self.verify_recipe or found.verify_recipe,
        )
