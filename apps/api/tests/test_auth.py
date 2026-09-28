"""US-L3: authN/authZ — token resolution, 401/403 matrix."""

from cricai_api.auth import resolve_role
from cricai_api.settings import Settings
from cricai_data.enums import Role
from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def test_resolve_role_maps_each_token() -> None:
    settings = Settings(parent_token="p", coach_token="c", player_token="k")
    assert resolve_role(settings, "p") is Role.PARENT
    assert resolve_role(settings, "c") is Role.COACH
    assert resolve_role(settings, "k") is Role.PLAYER
    assert resolve_role(settings, "wrong") is None


def test_empty_configured_token_never_authenticates() -> None:
    settings = Settings(parent_token="", coach_token="", player_token="")
    assert resolve_role(settings, "") is None


def test_missing_bearer_is_401(client: TestClient) -> None:
    assert client.get("/players").status_code == 401


def test_invalid_token_is_401(client: TestClient) -> None:
    assert client.get("/players", headers=auth("nope")).status_code == 401


def test_role_forbidden_is_403(client: TestClient) -> None:
    response = client.post(
        "/players",
        json={"name": "X", "birthdate": "2014-01-01"},
        headers=auth(PLAYER_TOKEN),
    )
    assert response.status_code == 403


def test_health_needs_no_auth(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_all_roles_can_read_players(client: TestClient) -> None:
    for token in (PARENT_TOKEN, COACH_TOKEN, PLAYER_TOKEN):
        assert client.get("/players", headers=auth(token)).status_code == 200
