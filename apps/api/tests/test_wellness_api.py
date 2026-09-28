"""US-H4 acceptance: check-in CRUD, adult-only clearance flow, state endpoint."""

import copy
import hashlib
import uuid
from datetime import date
from typing import Any

from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.models import AuditLog
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

TODAY = date(2026, 7, 10)


def _create_player(client: TestClient) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": "2026-07-10",
            "session_type": "batting",
            "bowler_source": "machine",
            "machine_settings": {"speed_kph": 85, "length": "good", "variation": None},
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201, response.text
    session_id: str = response.json()["id"]
    return session_id


def _checkin_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "checkin_date": TODAY.isoformat(),
        "soreness": {"back_lower": 1},
        "energy": 4,
        "sleep_hours": 9.0,
        "pain": False,
    }
    payload.update(overrides)
    return payload


def _post_checkin(
    client: TestClient, player_id: str, token: str = PLAYER_TOKEN, **overrides: object
) -> dict[str, Any]:
    response = client.post(
        f"/wellness/{player_id}/checkins",
        json=_checkin_payload(**overrides),
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _state(client: TestClient, player_id: str, as_of: date | None = None) -> dict[str, Any]:
    params = {} if as_of is None else {"as_of": as_of.isoformat()}
    response = client.get(f"/wellness/{player_id}/state", params=params, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _pain_clearance_audits(client: TestClient, checkin_id: str) -> list[tuple[str, Any]]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "pain_clearance",
                AuditLog.entity == "wellness_checkin",
                AuditLog.entity_id == checkin_id,
            )
        ).all()
        return [(row.actor, row.detail) for row in rows]
    finally:
        db.close()


class TestCreateCheckin:
    def test_every_role_can_check_in_and_is_recorded(self, client: TestClient) -> None:
        player_id = _create_player(client)
        for token, expected in (
            (PLAYER_TOKEN, "player"),
            (PARENT_TOKEN, "parent"),
            (COACH_TOKEN, "coach"),
        ):
            body = _post_checkin(client, player_id, token)
            assert body["created_by"] == expected
            assert body["player_id"] == player_id
            assert body["soreness"] == {"back_lower": 1}
            assert body["pain"] is False
            assert body["session_id"] is None

    def test_checkin_with_session_link(self, client: TestClient) -> None:
        player_id = _create_player(client)
        session_id = _create_session(client, player_id)
        body = _post_checkin(client, player_id, session_id=session_id)
        assert body["session_id"] == session_id

    def test_player_not_found(self, client: TestClient) -> None:
        response = client.post(
            f"/wellness/{uuid.uuid4()}/checkins",
            json=_checkin_payload(),
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 404

    def test_session_not_found(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.post(
            f"/wellness/{player_id}/checkins",
            json=_checkin_payload(session_id=str(uuid.uuid4())),
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "session not found"

    def test_session_of_another_player_conflicts(self, client: TestClient) -> None:
        player_id = _create_player(client)
        other_session = _create_session(client, _create_player(client))
        response = client.post(
            f"/wellness/{player_id}/checkins",
            json=_checkin_payload(session_id=other_session),
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 409

    def test_validation_problems_are_listed(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.post(
            f"/wellness/{player_id}/checkins",
            json=_checkin_payload(soreness={"tail": 1}, energy=9, sleep_hours=20.0),
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 422
        assert response.json()["detail"] == [
            "unknown soreness body key: 'tail'",
            "energy must be 1..5, got 9",
            "sleep_hours must be 0.0..14.0, got 20.0",
        ]

    def test_pain_checkin_stores_the_note(self, client: TestClient) -> None:
        player_id = _create_player(client)
        body = _post_checkin(
            client, player_id, pain=True, pain_note="back hurt on the last two overs"
        )
        assert body["pain"] is True
        assert body["pain_note"] == "back hurt on the last two overs"


class TestListCheckins:
    def test_list_ordered_and_filtered(self, client: TestClient) -> None:
        player_id = _create_player(client)
        _post_checkin(client, player_id, checkin_date="2026-07-08")
        _post_checkin(client, player_id, checkin_date="2026-07-10")
        _post_checkin(client, player_id, checkin_date="2026-07-09")

        response = client.get(f"/wellness/{player_id}/checkins", headers=auth(PLAYER_TOKEN))
        assert response.status_code == 200
        dates = [row["checkin_date"] for row in response.json()]
        assert dates == ["2026-07-08", "2026-07-09", "2026-07-10"]

        ranged = client.get(
            f"/wellness/{player_id}/checkins",
            params={"start": "2026-07-09", "end": "2026-07-09"},
            headers=auth(COACH_TOKEN),
        )
        assert [row["checkin_date"] for row in ranged.json()] == ["2026-07-09"]

    def test_start_after_end_rejected(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.get(
            f"/wellness/{player_id}/checkins",
            params={"start": "2026-07-10", "end": "2026-07-09"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 400

    def test_list_player_not_found(self, client: TestClient) -> None:
        response = client.get(f"/wellness/{uuid.uuid4()}/checkins", headers=auth(PARENT_TOKEN))
        assert response.status_code == 404

    def test_empty_list(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.get(f"/wellness/{player_id}/checkins", headers=auth(PLAYER_TOKEN))
        assert response.json() == []


class TestPainClearance:
    def _pain_checkin(self, client: TestClient, player_id: str) -> str:
        body = _post_checkin(client, player_id, pain=True, pain_note="sore back")
        checkin_id: str = body["id"]
        return checkin_id

    def test_parent_clears_with_note_and_audit(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = self._pain_checkin(client, player_id)
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "rested, shadow bowling pain-free"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["cleared_by"] == "parent"
        assert body["role"] == "parent"
        assert body["note"] == "rested, shadow bowling pain-free"
        audits = _pain_clearance_audits(client, checkin_id)
        assert len(audits) == 1
        actor, detail = audits[0]
        assert actor == "parent"
        assert detail["player_id"] == player_id
        # The clearance note is health PII: the audit keeps a hash, not the text.
        assert "note" not in detail
        assert detail["note_present"] is True
        assert (
            detail["note_sha256"] == hashlib.sha256(b"rested, shadow bowling pain-free").hexdigest()
        )
        assert detail["checkin_date"] == TODAY.isoformat()

    def test_coach_can_clear(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = self._pain_checkin(client, player_id)
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "cleared after assessment"},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        assert response.json()["role"] == "coach"

    def test_player_role_is_forbidden(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = self._pain_checkin(client, player_id)
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "I feel fine"},
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 403
        assert _pain_clearance_audits(client, checkin_id) == []

    def test_note_is_required(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = self._pain_checkin(client, player_id)
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": ""},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 422

    def test_checkin_not_found(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.post(
            f"/wellness/{player_id}/checkins/{uuid.uuid4()}/clearance",
            json={"note": "n/a"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 404

    def test_checkin_of_another_player_not_found(self, client: TestClient) -> None:
        player_id = _create_player(client)
        other = _create_player(client)
        checkin_id = self._pain_checkin(client, other)
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "wrong player"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 404

    def test_clearance_player_not_found(self, client: TestClient) -> None:
        response = client.post(
            f"/wellness/{uuid.uuid4()}/checkins/{uuid.uuid4()}/clearance",
            json={"note": "n/a"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 404

    def test_no_pain_flag_to_clear(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = _post_checkin(client, player_id)["id"]
        response = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "nothing to clear"},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 400

    def test_double_clearance_conflicts(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = self._pain_checkin(client, player_id)
        first = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "first"},
            headers=auth(PARENT_TOKEN),
        )
        assert first.status_code == 201
        second = client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "second"},
            headers=auth(COACH_TOKEN),
        )
        assert second.status_code == 409


class TestWellnessState:
    def test_absence_is_explicit(self, client: TestClient) -> None:
        player_id = _create_player(client)
        state = _state(client, player_id, TODAY)
        assert state["no_checkin"] is True
        assert state["checked_in"] is False
        assert state["last_checkin_date"] is None
        assert state["days_since_checkin"] is None
        assert state["pain_active"] is False
        assert state["bowling_suppressed"] is False

    def test_pain_suppresses_until_adult_clearance(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_id = _post_checkin(client, player_id, pain=True)["id"]
        suppressed = _state(client, player_id, TODAY)
        assert suppressed["pain_active"] is True
        assert suppressed["bowling_suppressed"] is True
        assert suppressed["open_pain_checkin_ids"] == [checkin_id]

        client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "physio said all good"},
            headers=auth(COACH_TOKEN),
        )
        cleared = _state(client, player_id, TODAY)
        assert cleared["pain_active"] is False
        assert cleared["bowling_suppressed"] is False
        assert cleared["open_pain_checkin_ids"] == []
        assert cleared["checked_in"] is True
        assert cleared["days_since_checkin"] == 0

    def test_escalation_two_pain_reports_in_14_days(self, client: TestClient) -> None:
        player_id = _create_player(client)
        _post_checkin(client, player_id, checkin_date="2026-06-27", pain=True)
        one = _state(client, player_id, date(2026, 6, 27))
        assert one["escalation"] is False
        assert one["pain_reports_in_window"] == 1

        _post_checkin(client, player_id, checkin_date="2026-07-10", pain=True)
        state = _state(client, player_id, TODAY)
        assert state["escalation"] is True
        assert state["pain_reports_in_window"] == 2

    def test_escalation_thresholds_come_from_versioned_config(self, client: TestClient) -> None:
        """The state endpoint resolves escalation from the latest safety_config,
        not code defaults — a single pain report escalates once a coach-approved
        config tightens pain_escalation_count to 1 (default is 2)."""
        player_id = _create_player(client)
        tightened = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
        tightened["wellness"]["pain_escalation_count"] = 1
        posted = client.post(
            "/workload/safety-config",
            json={"config": tightened, "reason": "stricter escalation this term"},
            headers=auth(PARENT_TOKEN),  # tightening count is a lower value: parent may set it
        )
        assert posted.status_code == 201
        _post_checkin(client, player_id, checkin_date=TODAY.isoformat(), pain=True)
        state = _state(client, player_id, TODAY)
        assert state["pain_reports_in_window"] == 1
        assert state["escalation"] is True  # count=1 from config v1, not the default 2

    def test_default_as_of_is_today(self, client: TestClient) -> None:
        player_id = _create_player(client)
        checkin_date = date.today().isoformat()
        _post_checkin(client, player_id, checkin_date=checkin_date)
        state = _state(client, player_id)
        assert state["as_of"] == checkin_date
        assert state["checked_in"] is True

    def test_state_player_not_found(self, client: TestClient) -> None:
        response = client.get(f"/wellness/{uuid.uuid4()}/state", headers=auth(PLAYER_TOKEN))
        assert response.status_code == 404
