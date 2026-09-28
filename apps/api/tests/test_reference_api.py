"""US-E5 acceptance: reference-ball library CRUD, RBAC, guest scoping, audits."""

import uuid
from typing import Any

import httpx
from cricai_data.models import AuditLog, BallEvent
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


def _create_player(client: TestClient, *, name: str = "Arjun", is_guest: bool = False) -> str:
    response = client.post(
        "/players",
        json={"name": name, "birthdate": "2014-11-20", "is_guest": is_guest},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    session_id: str = response.json()["id"]
    return session_id


def _seed_event(client: TestClient, session_id: str, ball_no: int, *, valid: bool = True) -> None:
    """Seed a detected BallEvent directly (events API is another story's file)."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        db.add(
            BallEvent(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                start_ms=1000 * ball_no,
                release_ms=1000 * ball_no + 200,
                contact_ms=None,
                end_ms=1000 * ball_no + 900,
                confidence=0.9,
                valid=valid,
            )
        )
        db.commit()
    finally:
        db.close()


def _tag_ball(client: TestClient, session_id: str, ball_no: int) -> None:
    response = client.post(
        f"/sessions/{session_id}/tags",
        json={
            "ball_no": ball_no,
            "line": "off",
            "length": "good",
            "shot": "drive",
            "footwork": "front",
            "contact": "middle",
            "outcome": "controlled_ground_shot",
            "control": True,
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _mark(
    client: TestClient,
    session_id: str,
    ball_no: int,
    *,
    label: str = "model cover drive",
    token: str = COACH_TOKEN,
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/reference-balls",
        json={"ball_no": ball_no, "label": label},
        headers=auth(token),
    )


def _library(client: TestClient, *, token: str = PARENT_TOKEN, **params: str) -> httpx.Response:
    return client.get("/reference-balls", params=params, headers=auth(token))


def _audit_rows(client: TestClient, action: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(actor, entity_id, detail) of every audit row written for ``action``."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.action == action, AuditLog.entity == "reference_ball")
        ).all()
        return [(row.actor, row.entity_id, row.detail) for row in rows]
    finally:
        db.close()


def test_coach_marks_event_ball_as_reference(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _seed_event(client, session_id, 1)
    response = _mark(client, session_id, 1, token=COACH_TOKEN)
    assert response.status_code == 201
    body = response.json()
    assert body["session_id"] == session_id
    assert body["ball_no"] == 1
    assert body["label"] == "model cover drive"
    assert body["marked_by"] == "coach"
    assert body["id"]
    assert body["created_at"]


def test_parent_marks_tag_only_ball_as_reference(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _tag_ball(client, session_id, 3)  # no BallEvent: manual tag alone validates the ball
    response = _mark(client, session_id, 3, label="straight bat block", token=PARENT_TOKEN)
    assert response.status_code == 201
    assert response.json()["marked_by"] == "parent"


def test_unknown_ball_rejected_422(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    response = _mark(client, session_id, 7)
    assert response.status_code == 422
    assert "no ball event or tag" in response.json()["detail"]

    # A rejected (valid=False) event is not a ball either (US-D4).
    _seed_event(client, session_id, 5, valid=False)
    rejected = _mark(client, session_id, 5)
    assert rejected.status_code == 422


def test_duplicate_reference_ball_409(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _seed_event(client, session_id, 1)
    assert _mark(client, session_id, 1).status_code == 201
    duplicate = _mark(client, session_id, 1, label="another label")
    assert duplicate.status_code == 409
    assert "already a reference ball" in duplicate.json()["detail"]


def test_unknown_session_404(client: TestClient) -> None:
    assert _mark(client, UNKNOWN_ID, 1).status_code == 404
    deleted = client.delete(f"/sessions/{UNKNOWN_ID}/reference-balls/1", headers=auth(PARENT_TOKEN))
    assert deleted.status_code == 404


def test_player_role_cannot_write_403(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _seed_event(client, session_id, 1)
    assert _mark(client, session_id, 1, token=PLAYER_TOKEN).status_code == 403
    assert _mark(client, session_id, 1, token=COACH_TOKEN).status_code == 201
    deleted = client.delete(f"/sessions/{session_id}/reference-balls/1", headers=auth(PLAYER_TOKEN))
    assert deleted.status_code == 403


def test_delete_reference_ball(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _seed_event(client, session_id, 1)
    assert _mark(client, session_id, 1).status_code == 201

    deleted = client.delete(f"/sessions/{session_id}/reference-balls/1", headers=auth(PARENT_TOKEN))
    assert deleted.status_code == 204
    assert _library(client).json() == []

    again = client.delete(f"/sessions/{session_id}/reference-balls/1", headers=auth(PARENT_TOKEN))
    assert again.status_code == 404
    assert "reference ball not found" in again.json()["detail"]


def test_create_and_delete_are_audited(client: TestClient) -> None:
    session_id = _create_session(client, _create_player(client))
    _seed_event(client, session_id, 2)
    created = _mark(client, session_id, 2, label="model pull", token=COACH_TOKEN)
    reference_id = created.json()["id"]

    marked = _audit_rows(client, "reference_ball_marked")
    assert marked == [
        ("coach", reference_id, {"session_id": session_id, "ball_no": 2, "label": "model pull"})
    ]

    deleted = client.delete(f"/sessions/{session_id}/reference-balls/2", headers=auth(PARENT_TOKEN))
    assert deleted.status_code == 204
    unmarked = _audit_rows(client, "reference_ball_unmarked")
    assert unmarked == [
        ("parent", reference_id, {"session_id": session_id, "ball_no": 2, "label": "model pull"})
    ]


def test_library_spans_sessions_and_filters_by_player(client: TestClient) -> None:
    player_a = _create_player(client, name="Arjun")
    player_b = _create_player(client, name="Meera")
    session_a1 = _create_session(client, player_a)
    session_a2 = _create_session(client, player_a)
    session_b = _create_session(client, player_b)
    for session_id, ball_no in ((session_a1, 1), (session_a2, 4), (session_b, 2)):
        _seed_event(client, session_id, ball_no)
        assert _mark(client, session_id, ball_no).status_code == 201

    everything = _library(client)
    assert everything.status_code == 200
    rows = everything.json()
    assert {(row["session_id"], row["ball_no"]) for row in rows} == {
        (session_a1, 1),
        (session_a2, 4),
        (session_b, 2),
    }
    assert all(row["player_id"] in (player_a, player_b) for row in rows)

    filtered = _library(client, player_id=player_a).json()
    assert {(row["session_id"], row["ball_no"]) for row in filtered} == {
        (session_a1, 1),
        (session_a2, 4),
    }
    assert all(row["player_id"] == player_a for row in filtered)

    assert _library(client, player_id=UNKNOWN_ID).status_code == 404


def test_library_guest_scoping_for_players(client: TestClient) -> None:
    regular = _create_player(client, name="Arjun")
    guest = _create_player(client, name="Visitor", is_guest=True)
    regular_session = _create_session(client, regular)
    guest_session = _create_session(client, guest)
    for session_id in (regular_session, guest_session):
        _seed_event(client, session_id, 1)
        assert _mark(client, session_id, 1).status_code == 201

    # US-L3: players never see guest data; guardians see everything.
    player_view = _library(client, token=PLAYER_TOKEN).json()
    assert [row["session_id"] for row in player_view] == [regular_session]
    parent_view = _library(client, token=PARENT_TOKEN).json()
    assert {row["session_id"] for row in parent_view} == {regular_session, guest_session}

    # Filtering to the guest player hides its existence from players.
    assert _library(client, token=PLAYER_TOKEN, player_id=guest).status_code == 404
    coach_guest = _library(client, token=COACH_TOKEN, player_id=guest)
    assert coach_guest.status_code == 200
    assert [row["session_id"] for row in coach_guest.json()] == [guest_session]

    # A non-guest filter stays visible to the player role.
    player_filtered = _library(client, token=PLAYER_TOKEN, player_id=regular)
    assert [row["session_id"] for row in player_filtered.json()] == [regular_session]
