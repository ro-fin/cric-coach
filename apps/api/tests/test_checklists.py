"""US-A5 acceptance: safety checklist gate for machine sessions (SAF suite)."""

import uuid
from pathlib import Path

import httpx
import pytest
from cricai_api.routers.checklists import REQUIRED_MACHINE_CHECKLIST
from cricai_coaching.content_lint import assert_kid_safe
from cricai_data.lifecycle import SessionState
from cricai_data.models import AuditLog, Session
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _create_machine_session(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    session = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "machine",
            "machine_settings": {"speed_kph": 85, "length": "good"},
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session_id: str = session["id"]
    return session_id


def _full_items() -> dict[str, bool]:
    return {item_id: True for item_id, _ in REQUIRED_MACHINE_CHECKLIST}


def _ack(
    client: TestClient, session_id: str, items: dict[str, bool], token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/checklist-ack",
        json={"items": items, "acked_by": "parent-dinesh"},
        headers=auth(token),
    )


def _db_session(engine: Engine, session_id: str) -> Session:
    with DbSession(engine) as db:
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        db.expunge(session)
        return session


def test_get_machine_checklist_returns_all_items_for_every_role(client: TestClient) -> None:
    for token in (PARENT_TOKEN, COACH_TOKEN, PLAYER_TOKEN):
        response = client.get("/checklists/machine", headers=auth(token))
        assert response.status_code == 200
        items = response.json()["items"]
        assert [i["id"] for i in items] == [item_id for item_id, _ in REQUIRED_MACHINE_CHECKLIST]
        assert all(i["label"] for i in items)


@pytest.mark.safety
def test_checklist_labels_pass_content_lint() -> None:
    for _, label in REQUIRED_MACHINE_CHECKLIST:
        assert_kid_safe(label)


@pytest.mark.safety
def test_cannot_ack_with_missing_item(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    items = _full_items()
    del items["estop_tested"]
    response = _ack(client, session_id, items)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["missing"] == ["estop_tested"]
    assert detail["false"] == []
    assert _db_session(engine, session_id).checklist_ack_id is None


@pytest.mark.safety
def test_cannot_ack_with_item_false(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    items = _full_items() | {"helmet_on": False}
    response = _ack(client, session_id, items)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["missing"] == []
    assert detail["false"] == ["helmet_on"]
    assert _db_session(engine, session_id).checklist_ack_id is None


@pytest.mark.safety
def test_ack_sets_checklist_ack_and_writes_audit(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    response = _ack(client, session_id, _full_items())
    assert response.status_code == 201
    body = response.json()
    assert body["acked_by"] == "parent-dinesh"
    assert body["items"] == _full_items()
    assert body["acked_at"]

    # Lifecycle contract half: the start gate reads checklist_ack_id — it is set.
    session = _db_session(engine, session_id)
    assert session.checklist_ack_id is not None
    assert str(session.checklist_ack_id) == body["id"]

    with DbSession(engine) as db:
        audits = db.scalars(select(AuditLog).where(AuditLog.action == "checklist_ack")).all()
        assert len(audits) == 1
        assert audits[0].actor == "parent-dinesh"
        assert audits[0].entity == "session"
        assert audits[0].entity_id == session_id
        assert audits[0].detail is not None
        assert audits[0].detail["ack_id"] == body["id"]
        assert audits[0].detail["replaced_ack_id"] is None


@pytest.mark.safety
def test_reack_before_start_replaces_previous_ack(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    first = _ack(client, session_id, _full_items()).json()
    second = _ack(client, session_id, _full_items())
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]
    assert str(_db_session(engine, session_id).checklist_ack_id) == second.json()["id"]

    with DbSession(engine) as db:
        audits = db.scalars(
            select(AuditLog).where(AuditLog.action == "checklist_ack").order_by(AuditLog.at)
        ).all()
        assert len(audits) == 2
        assert audits[-1].detail is not None
        assert audits[-1].detail["replaced_ack_id"] == first["id"]


@pytest.mark.safety
def test_rbac_coach_and_player_cannot_ack(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    for token in (COACH_TOKEN, PLAYER_TOKEN):
        response = _ack(client, session_id, _full_items(), token=token)
        assert response.status_code == 403
    assert _db_session(engine, session_id).checklist_ack_id is None


def test_ack_unknown_session_404(client: TestClient) -> None:
    response = _ack(client, "00000000-0000-0000-0000-000000000000", _full_items())
    assert response.status_code == 404


@pytest.mark.safety
def test_ack_after_start_conflicts_409(client: TestClient, engine: Engine) -> None:
    session_id = _create_machine_session(client)
    with DbSession(engine) as db:
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        session.state = SessionState.RECORDING  # lifecycle router is another story
        db.commit()
    response = _ack(client, session_id, _full_items())
    assert response.status_code == 409
    assert "already started" in response.json()["detail"]
    assert _db_session(engine, session_id).checklist_ack_id is None


def test_ack_payload_validation(client: TestClient) -> None:
    session_id = _create_machine_session(client)
    bad = client.post(
        f"/sessions/{session_id}/checklist-ack",
        json={"items": _full_items(), "acked_by": ""},
        headers=auth(PARENT_TOKEN),
    )
    assert bad.status_code == 422
    assert any("acked_by" in str(e["loc"]) for e in bad.json()["detail"])
