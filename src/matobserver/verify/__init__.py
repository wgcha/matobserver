"""레시피 dry-run 검사 (recipe.md §7.2, Impl-Solver). Backend 진입점: `matobserver.verify:verify_recipe` (module-boundaries.md §4.2)."""

from .dryrun import verify_recipe
from .recipe_checks import check_reference_curve

__all__ = ["verify_recipe", "check_reference_curve"]
