"""US-B1 acceptance: create/fetch/list sessions with validation and pagination."""

from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def _create_player(client: TestClient) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    player_id: str = response.json()["id"]
    return player_id


def _machine_payload(player_id: str, day: str = "2026-07-07") -> dict[str, object]:
    return {
        "player_id": player_id,
        "date": day,
        "session_type": "batting",
        "bowler_source": "machine",
        "machine_settings": {"speed_kph": 85, "length": "good", "variation": None},
        "notes": "evening block",
    }


def test_create_session_returns_201_with_id(client: TestClient) -> None:
    player_id = _create_player(client)
    response = client.post(
        "/sessions", json=_machine_payload(player_id), headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["state"] == "created"
    assert body["machine_settings"]["speed_kph"] == 85.0
    fetched = client.get(f"/sessions/{body['id']}", headers=auth(PLAYER_TOKEN))
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_coach_can_create_player_cannot(client: TestClient) -> None:
    player_id = _create_player(client)
    ok = client.post("/sessions", json=_machine_payload(player_id), headers=auth(COACH_TOKEN))
    assert ok.status_code == 201
    forbidden = client.post(
        "/sessions", json=_machine_payload(player_id), headers=auth(PLAYER_TOKEN)
    )
    assert forbidden.status_code == 403


def test_invalid_payloads_return_422_with_field_errors(client: TestClient) -> None:
    player_id = _create_player(client)

    bad_enum = _machine_payload(player_id) | {"session_type": "swimming"}
    response = client.post("/sessions", json=bad_enum, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert any("session_type" in str(e["loc"]) for e in response.json()["detail"])

    bad_speed = _machine_payload(player_id)
    bad_speed["machine_settings"] = {"speed_kph": 250, "length": "good"}  # type: ignore[assignment]
    response = client.post("/sessions", json=bad_speed, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert any("speed_kph" in str(e["loc"]) for e in response.json()["detail"])


def test_unknown_player_rejected(client: TestClient) -> None:
    payload = _machine_payload("00000000-0000-0000-0000-000000000000")
    response = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert "unknown player_id" in response.json()["detail"]


def test_machine_session_requires_machine_settings(client: TestClient) -> None:
    player_id = _create_player(client)
    payload = _machine_payload(player_id)
    del payload["machine_settings"]
    response = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert "machine_settings" in response.json()["detail"]


def test_non_machine_session_needs_no_settings(client: TestClient) -> None:
    player_id = _create_player(client)
    payload = {
        "player_id": player_id,
        "date": "2026-07-07",
        "session_type": "bowling",
        "bowler_source": "coach",
    }
    response = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 201
    assert response.json()["machine_settings"] is None


def test_session_not_found_404(client: TestClient) -> None:
    response = client.get(
        "/sessions/00000000-0000-0000-0000-000000000000", headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404


def test_list_pagination_and_filters(client: TestClient) -> None:
    player_id = _create_player(client)
    for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
        client.post("/sessions", json=_machine_payload(player_id, day), headers=auth(PARENT_TOKEN))

    page = client.get(
        "/sessions",
        params={"player_id": player_id, "limit": 2, "offset": 0},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["items"][0]["session_date"] == "2026-07-03"  # newest first

    page2 = client.get(
        "/sessions",
        params={"player_id": player_id, "limit": 2, "offset": 2},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert [s["session_date"] for s in page2["items"]] == ["2026-07-01"]

    ranged = client.get(
        "/sessions",
        params={"date_from": "2026-07-02", "date_to": "2026-07-02"},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert ranged["total"] == 1
    assert ranged["items"][0]["session_date"] == "2026-07-02"

    other = client.get(
        "/sessions",
        params={"player_id": "00000000-0000-0000-0000-000000000000"},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert other["total"] == 0
    assert other["items"] == []

    bad_limit = client.get("/sessions", params={"limit": 0}, headers=auth(PARENT_TOKEN))
    assert bad_limit.status_code == 422


def _seed_guest_and_family_sessions(client: TestClient) -> tuple[str, str, str, str]:
    """Seed one family and one guest player, one session each.

    Returns (family_player_id, guest_player_id, family_session_id, guest_session_id).
    """
    family_id = _create_player(client)
    guest_id: str = client.post(
        "/players",
        json={"name": "Visitor", "birthdate": "2013-05-01", "is_guest": True},
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    family_session_id: str = client.post(
        "/sessions",
        json=_machine_payload(family_id, "2026-07-01"),
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    guest_session_id: str = client.post(
        "/sessions",
        json=_machine_payload(guest_id, "2026-07-02"),
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    return family_id, guest_id, family_session_id, guest_session_id


def test_player_role_session_list_excludes_guest_sessions(client: TestClient) -> None:
    _, guest_id, family_session_id, guest_session_id = _seed_guest_and_family_sessions(client)

    page = client.get("/sessions", headers=auth(PLAYER_TOKEN)).json()
    assert page["total"] == 1  # pagination total respects the guest filter
    assert [s["id"] for s in page["items"]] == [family_session_id]

    # Filtering by the guest player id leaks nothing either.
    guest_page = client.get(
        "/sessions", params={"player_id": guest_id}, headers=auth(PLAYER_TOKEN)
    ).json()
    assert guest_page["total"] == 0
    assert guest_page["items"] == []

    # Parent and coach still see all sessions, guest included.
    for token in (PARENT_TOKEN, COACH_TOKEN):
        page = client.get("/sessions", headers=auth(token)).json()
        assert page["total"] == 2
        assert {s["id"] for s in page["items"]} == {family_session_id, guest_session_id}


def test_player_role_get_guest_session_is_404(client: TestClient) -> None:
    _, _, family_session_id, guest_session_id = _seed_guest_and_family_sessions(client)

    assert (
        client.get(f"/sessions/{guest_session_id}", headers=auth(PLAYER_TOKEN)).status_code == 404
    )
    assert (
        client.get(f"/sessions/{family_session_id}", headers=auth(PLAYER_TOKEN)).status_code == 200
    )

    # Guest session remains visible to parent and coach.
    for token in (PARENT_TOKEN, COACH_TOKEN):
        assert client.get(f"/sessions/{guest_session_id}", headers=auth(token)).status_code == 200
