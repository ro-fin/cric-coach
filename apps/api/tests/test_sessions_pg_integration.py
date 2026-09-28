"""IT (US-B1): session CRUD round-trip against real PostgreSQL via migrations."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_api.app import create_app
from cricai_api.settings import Settings
from cricai_data.db import make_engine
from fastapi.testclient import TestClient

from cricai_testing.apptest import PARENT_TOKEN, auth

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"


@pytest.fixture
def pg_client(pg_url: str, tmp_path: Path) -> TestClient:
    cfg = Config(str(DATA_PKG / "alembic.ini"))
    os.environ["CRICAI_DATABASE_URL"] = pg_url
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=pg_url,
        storage_root=tmp_path / "storage",
        parent_token=PARENT_TOKEN,
    )
    return TestClient(create_app(settings=settings, engine=make_engine(pg_url)))


def test_create_upload_query_flow_on_postgres(pg_client: TestClient) -> None:
    player = pg_client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()

    created = pg_client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "machine",
            "machine_settings": {"speed_kph": 92.5, "length": "full"},
        },
        headers=auth(PARENT_TOKEN),
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    fetched = pg_client.get(f"/sessions/{session_id}", headers=auth(PARENT_TOKEN)).json()
    assert fetched["machine_settings"] == {
        "speed_kph": 92.5,
        "length": "full",
        "variation": None,
    }

    page = pg_client.get(
        "/sessions", params={"player_id": player["id"]}, headers=auth(PARENT_TOKEN)
    ).json()
    assert page["total"] == 1
