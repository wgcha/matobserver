"""외부(Backend·엔진·레지스트리)가 import 경로로 찾는 진입점."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_verify_recipe_package_export():
    from matobserver.verify import check_reference_curve, verify_recipe
    from matobserver.verify import dryrun, recipe_checks

    assert verify_recipe is dryrun.verify_recipe and check_reference_curve is recipe_checks.check_reference_curve
    import matobserver.verify as v

    assert "verify_recipe" in v.__all__ and callable(getattr(importlib.import_module("matobserver.verify"), "verify_recipe"))


def test_backend_hook_resolves_verify_recipe():
    from matobserver.validation import hooks

    resolved = hooks._resolve(hooks._VERIFY)
    from matobserver.verify.dryrun import verify_recipe

    assert resolved is verify_recipe


def test_lsdyna_registry_class_path_and_package_export():
    from matobserver.core.registry import Registry
    from matobserver.solvers.lsdyna import LsDynaAdapter

    reg = Registry.from_paths([Path(__file__).resolve().parents[3] / "config" / "plugins.d"])
    assert reg.plugin_class("solver", "lsdyna") is LsDynaAdapter
