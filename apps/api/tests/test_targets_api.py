"""US-I4 (built in the US-I1 capture story): declared bowling targets.

Guardian roles declare/delete line-length targets per session or block;
players read them; every mutation writes an AuditLog row; story i3 scores
deliveries against these declarations.
"""

import uuid
from typing import Any

from cricai_data.models import AuditLog
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def _create_session(client: TestClient, *, is_guest: bool = False) -> str:
    player = client.post(
        "/players",
        json={
            "name": "Visitor" if is_guest else "Arjun",
            "birthdate": "2014-11-20",
            "is_guest": is_guest,
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    response = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "bowling",
            "bowler_source": "human",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    session_id: str = response.json()["id"]
    return session_id


def _add_block(client: TestClient, session_id: str, start_s: float = 0.0) -> int:
    response = client.post(
        f"/sessions/{session_id}/blocks",
        json={
            "start_s": start_s,
            "end_s": start_s + 60.0,
            "bowler_source": "human",
            "intent": "spin_specific",
        },
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 201
    block_no: int = response.json()["block_no"]
    return block_no


def _target_payload(session_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": session_id,
        "line": "off",
        "length": "good",
        "description": "leg-break: good length, off-stump line to RH batter",
    }
    payload.update(overrides)
    return payload


def _declare(client: TestClient, session_id: str, **overrides: object) -> dict[str, object]:
    response = client.post(
        "/targets", json=_target_payload(session_id, **overrides), headers=auth(COACH_TOKEN)
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


def _audit_rows(client: TestClient, action: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(actor, entity_id, detail) of every bowling_target audit row."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.action == action, AuditLog.entity == "bowling_target")
        ).all()
        return [(row.actor, row.entity_id, row.detail) for row in rows]
    finally:
        db.close()


def test_declare_session_wide_target(client: TestClient) -> None:
    session_id = _create_session(client)
    body = _declare(client, session_id)
    assert body["session_id"] == session_id
    assert body["block_no"] is None
    assert body["line"] == "off"
    assert body["length"] == "good"
    assert body["description"] == "leg-break: good length, off-stump line to RH batter"
    assert body["created_by"] == "coach"
    assert body["created_at"] is not None


def test_declare_block_target_resolves_block_no(client: TestClient) -> None:
    session_id = _create_session(client)
    block_no = _add_block(client, session_id)
    body = _declare(client, session_id, block_no=block_no, line="leg", length="full")
    assert body["block_no"] == block_no
    assert body["line"] == "leg"
    assert body["length"] == "full"


def test_declare_writes_audit_row(client: TestClient) -> None:
    session_id = _create_session(client)
    body = _declare(client, session_id)
    rows = _audit_rows(client, "target_declare")
    assert rows == [
        (
            "coach",
            str(body["id"]),
            {"session_id": session_id, "block_no": None, "line": "off", "length": "good"},
        )
    ]


def test_declare_unknown_session_422(client: TestClient) -> None:
    response = client.post(
        "/targets", json=_target_payload(str(uuid.uuid4())), headers=auth(COACH_TOKEN)
    )
    assert response.status_code == 422


def test_declare_unknown_block_404(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.post(
        "/targets", json=_target_payload(session_id, block_no=7), headers=auth(COACH_TOKEN)
    )
    assert response.status_code == 404
    assert _audit_rows(client, "target_declare") == []


def test_declare_validation_rejects_bad_fields(client: TestClient) -> None:
    session_id = _create_session(client)
    bad_payloads = [
        _target_payload(session_id, description=""),
        _target_payload(session_id, description="x" * 501),
        _target_payload(session_id, line="wide"),
        _target_payload(session_id, length="half_tracker"),
    ]
    for bad in bad_payloads:
        response = client.post("/targets", json=bad, headers=auth(COACH_TOKEN))
        assert response.status_code == 422, bad


def test_list_returns_targets_oldest_first(client: TestClient) -> None:
    session_id = _create_session(client)
    first = _declare(client, session_id)
    second = _declare(client, session_id, line="middle", length="yorker")
    listed = client.get(f"/targets?session_id={session_id}", headers=auth(PLAYER_TOKEN)).json()
    assert [t["id"] for t in listed] == [first["id"], second["id"]]


def test_list_is_scoped_to_the_session(client: TestClient) -> None:
    session_a = _create_session(client)
    session_b = _create_session(client)
    _declare(client, session_a)
    listed = client.get(f"/targets?session_id={session_b}", headers=auth(COACH_TOKEN)).json()
    assert listed == []


def test_list_filters_by_block_no(client: TestClient) -> None:
    session_id = _create_session(client)
    block_no = _add_block(client, session_id)
    _declare(client, session_id)  # session-wide: excluded by the block filter
    block_target = _declare(client, session_id, block_no=block_no)
    listed = client.get(
        f"/targets?session_id={session_id}&block_no={block_no}", headers=auth(COACH_TOKEN)
    ).json()
    assert [t["id"] for t in listed] == [block_target["id"]]


def test_list_unknown_session_422(client: TestClient) -> None:
    response = client.get(f"/targets?session_id={uuid.uuid4()}", headers=auth(COACH_TOKEN))
    assert response.status_code == 422


def test_list_requires_session_id(client: TestClient) -> None:
    response = client.get("/targets", headers=auth(COACH_TOKEN))
    assert response.status_code == 422


def test_list_unknown_block_404(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.get(f"/targets?session_id={session_id}&block_no=9", headers=auth(COACH_TOKEN))
    assert response.status_code == 404


def test_delete_removes_target_and_audits(client: TestClient) -> None:
    session_id = _create_session(client)
    body = _declare(client, session_id)
    response = client.delete(f"/targets/{body['id']}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 204

    listed = client.get(f"/targets?session_id={session_id}", headers=auth(COACH_TOKEN)).json()
    assert listed == []
    rows = _audit_rows(client, "target_delete")
    assert rows == [
        (
            "parent",
            str(body["id"]),
            {"session_id": session_id, "block_no": None, "line": "off", "length": "good"},
        )
    ]


def test_delete_unknown_target_404(client: TestClient) -> None:
    response = client.delete(f"/targets/{uuid.uuid4()}", headers=auth(COACH_TOKEN))
    assert response.status_code == 404


def test_rbac_players_read_but_never_mutate(client: TestClient) -> None:
    session_id = _create_session(client)
    body = _declare(client, session_id)

    declare = client.post("/targets", json=_target_payload(session_id), headers=auth(PLAYER_TOKEN))
    assert declare.status_code == 403
    delete = client.delete(f"/targets/{body['id']}", headers=auth(PLAYER_TOKEN))
    assert delete.status_code == 403

    listed = client.get(f"/targets?session_id={session_id}", headers=auth(PLAYER_TOKEN))
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_guest_session_targets_are_hidden_from_players(client: TestClient) -> None:
    """US-L3: guest data is parent/coach-only (finding 56). For the player role
    a guest session must be indistinguishable from one that does not exist, so
    the reader returns this router's own pinned unknown-session 422 — the same
    hide-existence discipline the metrics/events/clips readers express as 404."""
    session_id = _create_session(client, is_guest=True)
    _declare(client, session_id)

    unknown = client.get(f"/targets?session_id={uuid.uuid4()}", headers=auth(PLAYER_TOKEN))
    hidden = client.get(f"/targets?session_id={session_id}", headers=auth(PLAYER_TOKEN))
    assert hidden.status_code == unknown.status_code == 422
    assert hidden.json() == unknown.json()  # no existence oracle for players

    for token in (PARENT_TOKEN, COACH_TOKEN):  # guardians still read guest targets
        listed = client.get(f"/targets?session_id={session_id}", headers=auth(token))
        assert listed.status_code == 200
        assert len(listed.json()) == 1
