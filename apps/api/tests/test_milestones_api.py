"""US-K4/G5: milestone feed — guardian+player read, deterministic ordering."""

import uuid
from datetime import date
from pathlib import Path

import pytest
from cricai_data.enums import MilestoneKind
from cricai_data.models import Milestone
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

UNKNOWN = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _create_player(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "handedness": "right"},
        headers=auth(PARENT_TOKEN),
    ).json()
    player_id: str = player["id"]
    return player_id


def _seed_milestone(
    engine: Engine,
    player_id: str,
    kind: MilestoneKind,
    metric: str,
    value: float,
    achieved_on: date,
) -> None:
    with DbSession(engine) as db:
        db.add(
            Milestone(
                player_id=uuid.UUID(player_id),
                kind=kind,
                metric=metric,
                value=value,
                context={"n": 40},
                achieved_on=achieved_on,
            )
        )
        db.commit()


def test_unknown_player_404(client: TestClient) -> None:
    response = client.get(f"/milestones/players/{UNKNOWN}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_requires_a_role_token(client: TestClient) -> None:
    player_id = _create_player(client)
    assert client.get(f"/milestones/players/{player_id}").status_code == 401


def test_guardians_and_player_can_read(client: TestClient, engine: Engine) -> None:
    """US-K4: milestones are the child's celebratory surface — player reads too."""
    player_id = _create_player(client)
    _seed_milestone(
        engine, player_id, MilestoneKind.PERSONAL_BEST, "control_pct", 0.7, date(2026, 6, 17)
    )
    for token in (PARENT_TOKEN, COACH_TOKEN, PLAYER_TOKEN):
        response = client.get(f"/milestones/players/{player_id}", headers=auth(token))
        assert response.status_code == 200
        (row,) = response.json()
        assert row["kind"] == "personal_best"
        assert row["metric"] == "control_pct"
        assert row["value"] == 0.7
        assert row["achieved_on"] == "2026-06-17"
        assert row["context"] == {"n": 40}
        assert row["player_id"] == player_id


def test_empty_feed_is_an_empty_list(client: TestClient) -> None:
    player_id = _create_player(client)
    response = client.get(f"/milestones/players/{player_id}", headers=auth(PLAYER_TOKEN))
    assert response.status_code == 200
    assert response.json() == []


def test_newest_first_with_deterministic_tiebreak(client: TestClient, engine: Engine) -> None:
    player_id = _create_player(client)
    _seed_milestone(engine, player_id, MilestoneKind.VOLUME, "bowling_balls", 100, date(2026, 6, 1))
    _seed_milestone(engine, player_id, MilestoneKind.STREAK, "practice_days", 3, date(2026, 6, 17))
    _seed_milestone(
        engine, player_id, MilestoneKind.PERSONAL_BEST, "control_pct", 0.7, date(2026, 6, 17)
    )
    rows = client.get(f"/milestones/players/{player_id}", headers=auth(COACH_TOKEN)).json()
    assert [(row["achieved_on"], row["kind"]) for row in rows] == [
        ("2026-06-17", "personal_best"),
        ("2026-06-17", "streak"),
        ("2026-06-01", "volume"),
    ]


def test_kind_filter(client: TestClient, engine: Engine) -> None:
    player_id = _create_player(client)
    _seed_milestone(engine, player_id, MilestoneKind.VOLUME, "bowling_balls", 100, date(2026, 6, 1))
    _seed_milestone(engine, player_id, MilestoneKind.STREAK, "practice_days", 3, date(2026, 6, 17))
    rows = client.get(
        f"/milestones/players/{player_id}",
        params={"kind": "streak"},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert [row["kind"] for row in rows] == ["streak"]


def test_feed_scopes_to_the_named_player(client: TestClient, engine: Engine) -> None:
    player_id = _create_player(client)
    other_id = _create_player(client)
    _seed_milestone(engine, other_id, MilestoneKind.STREAK, "practice_days", 3, date(2026, 6, 17))
    rows = client.get(f"/milestones/players/{player_id}", headers=auth(PARENT_TOKEN)).json()
    assert rows == []
