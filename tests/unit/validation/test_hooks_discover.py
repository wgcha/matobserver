"""validation/hooks.py discover — 훅 주입 없이 실제 모듈에서 찾는다 (module-boundaries.md §4.1·§4.2)."""

from __future__ import annotations

from matobserver.validation.hooks import EngineHooks


def test_discover_finds_real_entry_points():
    h = EngineHooks.discover()
    assert callable(h.run_calibration)
    assert callable(h.preflight)
    assert callable(h.verify_recipe)  # matobserver.verify 가 verify_recipe 를 재수출해야 한다
