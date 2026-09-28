"""US-K3 acceptance: coach-note CRUD + search, author attribution, audit trail,
and the SAF guarantees — the untrusted ``body`` never reaches an LLM-bound
payload (egress strict set) or the audit trail, and kid mode (player role)
sees shared notes only."""

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from cricai_api.egress import EgressViolation, sanitize_egress_payload
from cricai_data.models import AuditLog, CoachNote
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

UNKNOWN = "00000000-0000-0000-0000-000000000000"

INJECTION_BODY = '<script>alert("pwn")</script><img src=x onerror="alert(1)">'


@contextmanager
def _db(client: TestClient) -> Iterator[OrmSession]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db: OrmSession = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _create_player(client: TestClient, name: str = "Arjun") -> str:
    response = client.post(
        "/players",
        json={"name": name, "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": "2026-07-09",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    session_id: str = response.json()["id"]
    return session_id


def _note_payload(player_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"player_id": player_id, "body": "Head falling to off side."}
    payload.update(overrides)
    return payload


def _create_note(client: TestClient, payload: dict[str, Any], token: str = COACH_TOKEN) -> Any:
    return client.post("/notes", json=payload, headers=auth(token))


def _audit_rows(client: TestClient, action: str) -> list[Any]:
    with _db(client) as db:
        rows = db.execute(select(AuditLog).where(AuditLog.action == action)).scalars().all()
        return [(row.actor, row.entity, row.entity_id, dict(row.detail or {})) for row in rows]


# --- create -------------------------------------------------------------------


def test_create_note_attributes_author_and_defaults_visibility(client: TestClient) -> None:
    player_id = _create_player(client)
    response = _create_note(client, _note_payload(player_id))
    assert response.status_code == 201
    note = response.json()
    assert note["player_id"] == player_id
    assert note["session_id"] is None
    assert note["ball_no"] is None
    assert note["author"] == "coach"
    assert note["visibility"] == "coach_only"
    assert note["body"] == "Head falling to off side."
    assert note["created_at"] is not None


def test_create_note_pinned_to_session_and_ball(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    payload = _note_payload(player_id, session_id=session_id, ball_no=7, visibility="shared")
    note = _create_note(client, payload, token=PARENT_TOKEN).json()
    assert note["session_id"] == session_id
    assert note["ball_no"] == 7
    assert note["author"] == "parent"
    assert note["visibility"] == "shared"


def test_create_note_validation_and_reference_errors(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    other_player = _create_player(client, name="Guest")
    # ball_no without a session is meaningless (ball numbering is per-session).
    response = _create_note(client, _note_payload(player_id, ball_no=3))
    assert response.status_code == 422
    assert "ball_no requires session_id" in response.text
    assert _create_note(client, _note_payload(UNKNOWN)).status_code == 404
    payload = _note_payload(player_id, session_id=UNKNOWN)
    assert _create_note(client, payload).status_code == 404
    cross = _note_payload(other_player, session_id=session_id)
    assert _create_note(client, cross).status_code == 409
    assert _create_note(client, _note_payload(player_id, body="")).status_code == 422


def test_create_note_requires_guardian_role(client: TestClient) -> None:
    player_id = _create_player(client)
    assert _create_note(client, _note_payload(player_id), token=PLAYER_TOKEN).status_code == 403
    assert client.post("/notes", json=_note_payload(player_id)).status_code == 401


def test_create_note_audit_carries_body_hash_never_the_text(client: TestClient) -> None:
    player_id = _create_player(client)
    note = _create_note(client, _note_payload(player_id, visibility="shared")).json()
    rows = _audit_rows(client, "note_created")
    assert len(rows) == 1
    actor, entity, entity_id, detail = rows[0]
    assert (actor, entity, entity_id) == ("coach", "coach_note", note["id"])
    expected_sha = hashlib.sha256(b"Head falling to off side.").hexdigest()
    assert detail["body_sha256"] == expected_sha
    assert detail["visibility"] == "shared"
    assert detail["session_id"] is None
    assert "body" not in detail
    assert "Head falling" not in str(detail)


# --- list & search ------------------------------------------------------------


def test_list_notes_filters_by_session_and_ball(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    plain = _create_note(client, _note_payload(player_id)).json()["id"]
    pinned = _create_note(
        client, _note_payload(player_id, session_id=session_id, ball_no=7)
    ).json()["id"]
    other_ball = _create_note(
        client, _note_payload(player_id, session_id=session_id, ball_no=8)
    ).json()["id"]
    everything = client.get(f"/notes?player_id={player_id}", headers=auth(COACH_TOKEN)).json()
    assert [n["id"] for n in everything] == [plain, pinned, other_ball]  # oldest first
    by_session = client.get(
        f"/notes?player_id={player_id}&session_id={session_id}", headers=auth(COACH_TOKEN)
    ).json()
    assert [n["id"] for n in by_session] == [pinned, other_ball]
    by_ball = client.get(
        f"/notes?player_id={player_id}&session_id={session_id}&ball_no=7",
        headers=auth(COACH_TOKEN),
    ).json()
    assert [n["id"] for n in by_ball] == [pinned]
    assert client.get(f"/notes?player_id={UNKNOWN}", headers=auth(COACH_TOKEN)).json() == []


def test_search_is_case_insensitive_and_wildcards_are_literal(client: TestClient) -> None:
    player_id = _create_player(client)
    _create_note(client, _note_payload(player_id, body="Elbow drops on the Pull shot."))
    _create_note(client, _note_payload(player_id, body="Great balance at 100% intent."))
    hits = client.get(f"/notes?player_id={player_id}&q=pull", headers=auth(COACH_TOKEN)).json()
    assert [n["body"] for n in hits] == ["Elbow drops on the Pull shot."]
    # ``%`` and ``_`` in the query match literally, never as LIKE wildcards.
    percent = client.get(f"/notes?player_id={player_id}&q=100%25", headers=auth(COACH_TOKEN))
    assert [n["body"] for n in percent.json()] == ["Great balance at 100% intent."]
    underscore = client.get(f"/notes?player_id={player_id}&q=p_ll", headers=auth(COACH_TOKEN))
    assert underscore.json() == []
    backslash = client.get(f"/notes?player_id={player_id}&q=%5C", headers=auth(COACH_TOKEN))
    assert backslash.json() == []


# --- delete -------------------------------------------------------------------


def test_delete_note_removes_row_and_audits(client: TestClient) -> None:
    player_id = _create_player(client)
    note = _create_note(client, _note_payload(player_id)).json()
    response = client.delete(f"/notes/{note['id']}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 204
    with _db(client) as db:
        assert db.get(CoachNote, uuid.UUID(note["id"])) is None
    rows = _audit_rows(client, "note_deleted")
    assert len(rows) == 1
    actor, entity, entity_id, detail = rows[0]
    assert (actor, entity, entity_id) == ("parent", "coach_note", note["id"])
    assert "body" not in detail


def test_delete_note_404_and_role_gate(client: TestClient) -> None:
    player_id = _create_player(client)
    note = _create_note(client, _note_payload(player_id)).json()
    assert client.delete(f"/notes/{UNKNOWN}", headers=auth(COACH_TOKEN)).status_code == 404
    assert client.delete(f"/notes/{note['id']}", headers=auth(PLAYER_TOKEN)).status_code == 403


# --- SAF: kid mode & untrusted body -------------------------------------------


@pytest.mark.safety
def test_player_role_sees_shared_notes_only(client: TestClient) -> None:
    """Kid mode (US-K3 SAF): coach_only notes never ship to the player role."""
    player_id = _create_player(client)
    _create_note(client, _note_payload(player_id, body="Private plan.", visibility="coach_only"))
    shared = _create_note(
        client, _note_payload(player_id, body="Great pull shots today!", visibility="shared")
    ).json()["id"]
    kid_view = client.get(f"/notes?player_id={player_id}", headers=auth(PLAYER_TOKEN)).json()
    assert [n["id"] for n in kid_view] == [shared]
    assert all(n["visibility"] == "shared" for n in kid_view)
    adult_view = client.get(f"/notes?player_id={player_id}", headers=auth(COACH_TOKEN)).json()
    assert len(adult_view) == 2


@pytest.mark.safety
def test_note_body_is_stored_verbatim_and_served_as_inert_json(client: TestClient) -> None:
    """Script injection in a note body round-trips byte-identical as JSON data
    (US-K3 red team). The API never interprets or embeds it as HTML; the web
    panel's React escaping is covered by the vitest SAF suite."""
    player_id = _create_player(client)
    note = _create_note(client, _note_payload(player_id, body=INJECTION_BODY)).json()
    listed = client.get(f"/notes?player_id={player_id}", headers=auth(COACH_TOKEN)).json()
    assert note["body"] == INJECTION_BODY
    assert listed[0]["body"] == INJECTION_BODY
    response = client.get(f"/notes?player_id={player_id}", headers=auth(COACH_TOKEN))
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.safety
def test_note_body_can_never_enter_an_egress_payload(client: TestClient) -> None:
    """US-K3/US-G4: a note is agent *context only* — any LLM-bound payload that
    embeds a note body (the ``body`` key, at any depth) is rejected outright
    by the egress allow-list, prompt-injection payloads included."""
    player_id = _create_player(client)
    _create_note(client, _note_payload(player_id, body=INJECTION_BODY))
    notes = client.get(f"/notes?player_id={player_id}", headers=auth(COACH_TOKEN)).json()
    llm_bound = {"metrics": {"control_pct": 61.0}, "coach_context": notes}
    with pytest.raises(EgressViolation) as excinfo:
        sanitize_egress_payload(llm_bound)
    assert "body" in excinfo.value.keys
    # Stripping the body is the only way through; the note text is gone.
    stripped = [{k: v for k, v in note.items() if k != "body"} for note in notes]
    sanitized = sanitize_egress_payload({"metrics": {"control_pct": 61.0}, "context": stripped})
    assert INJECTION_BODY not in str(sanitized)
