"""Application factory."""

from cricai_data.db import make_engine, make_session_factory
from cricai_data.storage import FsObjectStore
from fastapi import FastAPI
from sqlalchemy import Engine

import cricai_api
from cricai_api.settings import Settings

#: Router modules wired into the app. Each module exposes ``router``.
ROUTER_MODULES: tuple[str, ...] = (
    "players",
    "sessions",
    "cameras",
    "lifecycle",
    "health_checks",
    "checklists",
    "videos",
    "blocks",
    "tags",
    "storage_admin",
    "privacy",
    "calibration",
    "bounce",
    "heatmap",
    "events",
    "clips",
    "ball_metrics",
    "reference",
    "datasets",
    "models",
    "tracks",
    "rules",
    "reports",
    "workload",
    "wellness",
    "pipeline",
    "drills",
    "alerts",
    "notes",
    "targets",
    "labels",
    "milestones",
    "settings",
)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """Build the API. Tests inject settings/engine; production uses env config."""
    settings = settings if settings is not None else Settings()
    engine = engine if engine is not None else make_engine(settings.database_url)

    app = FastAPI(title="cricAI API", version=cricai_api.__version__)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.store = FsObjectStore(settings.storage_root)

    for module_name in ROUTER_MODULES:
        module = __import__(f"cricai_api.routers.{module_name}", fromlist=["router"])
        app.include_router(module.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": cricai_api.__version__}

    return app
