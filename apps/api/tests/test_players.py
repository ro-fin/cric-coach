from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def test_create_and_fetch_player(client: TestClient) -> None:
    created = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "handedness": "right"},
        headers=auth(PARENT_TOKEN),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["is_guest"] is False

    fetched = client.get(f"/players/{body['id']}", headers=auth(PARENT_TOKEN))
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Arjun"

    listed = client.get("/players", headers=auth(PARENT_TOKEN))
    assert [p["id"] for p in listed.json()] == [body["id"]]


def test_player_validation_422(client: TestClient) -> None:
    response = client.post(
        "/players",
        json={"name": "", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422


def test_player_not_found_404(client: TestClient) -> None:
    response = client.get(
        "/players/00000000-0000-0000-0000-000000000000", headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404


def _seed_family_and_guest(client: TestClient) -> tuple[str, str]:
    """Create one family (non-guest) player and one guest player; return their ids."""
    family = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    guest = client.post(
        "/players",
        json={"name": "Visitor", "birthdate": "2013-05-01", "is_guest": True},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert guest["is_guest"] is True
    return family["id"], guest["id"]


def test_player_role_list_excludes_guests(client: TestClient) -> None:
    family_id, guest_id = _seed_family_and_guest(client)

    listed = client.get("/players", headers=auth(PLAYER_TOKEN)).json()
    assert [p["id"] for p in listed] == [family_id]

    # Parent and coach still see everyone, guests included.
    for token in (PARENT_TOKEN, COACH_TOKEN):
        listed = client.get("/players", headers=auth(token)).json()
        assert {p["id"] for p in listed} == {family_id, guest_id}


def test_player_role_get_guest_is_404(client: TestClient) -> None:
    family_id, guest_id = _seed_family_and_guest(client)

    assert client.get(f"/players/{guest_id}", headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(f"/players/{family_id}", headers=auth(PLAYER_TOKEN)).status_code == 200

    # Guest remains visible to parent and coach.
    for token in (PARENT_TOKEN, COACH_TOKEN):
        assert client.get(f"/players/{guest_id}", headers=auth(token)).status_code == 200
