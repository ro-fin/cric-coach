"""In-memory app harness: SQLite engine + tmp object store + role tokens."""

from __future__ import annotations

from pathlib import Path

from cricai_api.app import create_app
from cricai_api.settings import Settings
from cricai_data.db import create_all
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool

PARENT_TOKEN = "test-parent-token"
COACH_TOKEN = "test-coach-token"
PLAYER_TOKEN = "test-player-token"


def make_sqlite_engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    create_all(engine)
    return engine


def make_test_app(tmp_path: Path, engine: Engine | None = None) -> FastAPI:
    settings = Settings(
        database_url="sqlite://",  # unused: engine injected
        storage_root=tmp_path / "storage",
        parent_token=PARENT_TOKEN,
        coach_token=COACH_TOKEN,
        player_token=PLAYER_TOKEN,
    )
    return create_app(settings=settings, engine=engine or make_sqlite_engine())


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
