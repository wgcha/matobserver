"""FastAPI 앱 조립. `create_app(config, registry, ...)` 또는 uvicorn 팩토리 `app_factory()` (설정은 `MATOBSERVER_CONFIG`)."""

from __future__ import annotations

from fastapi import FastAPI

from .. import __version__
from ..core.config import Config, load_config
from ..core.registry import Registry
from ..jobs.clock import Clock, utcnow
from ..matnexus.client import MatNexusClient
from ..validation.hooks import EngineHooks
from .context import AppContext, build_context
from .errors import install_error_handlers
from .routes import admin, router


def create_app(
    config: Config,
    registry: Registry,
    *,
    matnexus: MatNexusClient | None = None,
    hooks: EngineHooks | None = None,
    clock: Clock = utcnow,
    discover_hooks: bool = True,
    context: AppContext | None = None,
) -> FastAPI:
    app = FastAPI(title="matobserver", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = context or build_context(config, registry, matnexus=matnexus, hooks=hooks, clock=clock,
                                             discover_hooks=discover_hooks)
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(admin)
    return app


def app_factory() -> FastAPI:
    """`uvicorn matobserver.api.app:app_factory --factory`. 레지스트리 로드 실패는 기동 실패로 둔다 (B-10)."""
    config = load_config()
    registry = Registry.from_config(config)
    return create_app(config, registry)
