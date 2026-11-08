"""앱 컨텍스트: 설정·저장소·서비스 묶음 (의존성 주입 지점)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..artifacts.service import ArtifactService
from ..core.config import Config
from ..core.registry import Registry
from ..jobs.clock import Clock, utcnow
from ..jobs.db import Database
from ..jobs.files import FileService
from ..jobs.store import JobStore
from ..matnexus.client import MatNexusClient
from ..recipes.repository import RecipeRepository
from ..validation.hooks import EngineHooks
from ..validation.pipeline import ValidationContext
from .auth import TokenAuth


@dataclass
class AppContext:
    config: Config
    registry: Registry
    db: Database
    store: JobStore
    recipes: RecipeRepository
    artifacts: ArtifactService
    files: FileService
    matnexus: MatNexusClient | None
    hooks: EngineHooks
    auth: TokenAuth
    clock: Clock = utcnow
    validation: ValidationContext = field(init=False)

    def __post_init__(self) -> None:
        self.validation = ValidationContext(config=self.config, registry=self.registry, recipes=self.recipes, files=self.files,
                                            matnexus=self.matnexus, hooks=self.hooks)


def build_context(
    config: Config,
    registry: Registry,
    *,
    matnexus: MatNexusClient | None = None,
    hooks: EngineHooks | None = None,
    clock: Clock = utcnow,
    discover_hooks: bool = True,
) -> AppContext:
    db = Database(config.path("jobstore.sqlite_path"))
    store = JobStore(db, clock)
    h = hooks if hooks is not None else EngineHooks()
    if discover_hooks:
        h = h.with_missing_discovered()
    return AppContext(
        config=config, registry=registry, db=db, store=store,
        recipes=RecipeRepository(db, store, config, clock), artifacts=ArtifactService(db, config, clock),
        files=FileService(store, config, clock),
        matnexus=matnexus if matnexus is not None else MatNexusClient.from_config(config),
        hooks=h, auth=TokenAuth(config.get("auth.service_tokens"), config.get("auth.admin_tokens")), clock=clock,
    )

