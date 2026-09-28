"""US-F3 acceptance: track listing/filtering and per-ball payload retrieval.

Track rows/payloads are seeded directly (the track_balls worker job owns
writing them); these tests cover the read surfaces, RBAC and US-L3 scoping.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from cricai_data.models import BallTrack
from cricai_data.storage import FsObjectStore
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"

ALL_TOKENS = (PARENT_TOKEN, COACH_TOKEN, PLAYER_TOKEN)


@contextmanager
def _db(client: TestClient) -> Iterator[OrmSession]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db: OrmSession = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _store(client: TestClient) -> FsObjectStore:
    store: FsObjectStore = client.app.state.store  # type: ignore[union-attr]
    return store


def _create_player(client: TestClient, *, is_guest: bool = False) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "is_guest": is_guest},
        headers=auth(PARENT_TOKEN),
    )
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
    session_id: str = response.json()["id"]
    return session_id


def _make_session(client: TestClient, *, is_guest: bool = False) -> str:
    return _create_session(client, _create_player(client, is_guest=is_guest))


def _payload(ball_no: int, camera_id: str) -> dict[str, Any]:
    return {
        "points": [
            {
                "frame_no": 300,
                "ts_ms": 10_000.0,
                "px_x": 192.0 + ball_no,
                "px_y": 324.0,
                "score": 0.9,
                "bridged": False,
            }
        ],
        "segments": [
            {"kind": "pre_bounce", "start_ms": 10_000.0, "end_ms": 12_400.0, "confidence": 0.9}
        ],
        "flags": {"identity_risk": False, "long_gap": False, "pitch_mapped": False},
    }


def _seed_track(
    client: TestClient,
    session_id: str,
    ball_no: int,
    camera_id: str,
    *,
    coverage: float = 0.97,
    confidence: float = 0.87,
    identity_risk: bool = False,
    long_gap: bool = False,
    with_payload: bool = True,
) -> None:
    key = f"sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json"
    payload = _payload(ball_no, camera_id)
    payload["flags"] = {
        "identity_risk": identity_risk,
        "long_gap": long_gap,
        "pitch_mapped": False,
    }
    if with_payload:
        _store(client).put(key, json.dumps(payload).encode("utf-8"))
    with _db(client) as db:
        db.add(
            BallTrack(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id=camera_id,
                tracker_version="cv-kalman-1",
                points_key=key,
                coverage=coverage,
                segments=payload["segments"],
                flags=payload["flags"],
                confidence=confidence,
            )
        )


def _seed_standard_tracks(client: TestClient, session_id: str) -> None:
    """Ball 1: two clean cameras; ball 2: low coverage + long gap on C1 and an
    identity-risk track on C3; ball 3: C1 only."""
    _seed_track(client, session_id, 1, "C1")
    _seed_track(client, session_id, 1, "C3")
    _seed_track(client, session_id, 2, "C1", coverage=0.42, long_gap=True)
    _seed_track(client, session_id, 2, "C3", identity_risk=True)
    _seed_track(client, session_id, 3, "C1")


def _get(client: TestClient, path: str, token: str, **params: Any) -> Any:
    return client.get(path, headers=auth(token), params=params)


def test_list_tracks_returns_every_row_ordered(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_tracks(client, session_id)

    response = _get(client, f"/sessions/{session_id}/tracks", PARENT_TOKEN)
    assert response.status_code == 200
    body = response.json()
    assert [(row["ball_no"], row["camera_id"]) for row in body] == [
        (1, "C1"),
        (1, "C3"),
        (2, "C1"),
        (2, "C3"),
        (3, "C1"),
    ]
    first = body[0]
    assert first["session_id"] == session_id
    assert first["tracker_version"] == "cv-kalman-1"
    assert first["points_key"] == f"sessions/{session_id}/balls/1/track-C1.json"
    assert first["coverage"] == 0.97
    assert first["confidence"] == 0.87
    assert first["segments"] == [
        {"kind": "pre_bounce", "start_ms": 10_000.0, "end_ms": 12_400.0, "confidence": 0.9}
    ]
    assert first["flags"] == {
        "identity_risk": False,
        "long_gap": False,
        "pitch_mapped": False,
    }


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"ball_no": 2}, [(2, "C1"), (2, "C3")]),
        ({"camera_id": "C3"}, [(1, "C3"), (2, "C3")]),
        ({"min_coverage": 0.9}, [(1, "C1"), (1, "C3"), (2, "C3"), (3, "C1")]),
        ({"min_coverage": 0.0}, [(1, "C1"), (1, "C3"), (2, "C1"), (2, "C3"), (3, "C1")]),
        ({"identity_risk": True}, [(2, "C3")]),
        ({"identity_risk": False}, [(1, "C1"), (1, "C3"), (2, "C1"), (3, "C1")]),
        ({"long_gap": True}, [(2, "C1")]),
        ({"long_gap": False}, [(1, "C1"), (1, "C3"), (2, "C3"), (3, "C1")]),
        ({"ball_no": 2, "long_gap": True, "camera_id": "C1"}, [(2, "C1")]),
        ({"ball_no": 3, "identity_risk": True}, []),
    ],
)
def test_list_tracks_filters(
    client: TestClient, params: dict[str, Any], expected: list[tuple[int, str]]
) -> None:
    session_id = _make_session(client)
    _seed_standard_tracks(client, session_id)

    response = _get(client, f"/sessions/{session_id}/tracks", COACH_TOKEN, **params)
    assert response.status_code == 200
    assert [(row["ball_no"], row["camera_id"]) for row in response.json()] == expected


@pytest.mark.parametrize(
    "params",
    [
        {"ball_no": 0},
        {"camera_id": "C9"},
        {"camera_id": "cam1"},
        {"min_coverage": -0.1},
        {"min_coverage": 1.5},
        {"identity_risk": "bogus"},
    ],
)
def test_list_tracks_rejects_invalid_filters(client: TestClient, params: dict[str, Any]) -> None:
    session_id = _make_session(client)
    response = _get(client, f"/sessions/{session_id}/tracks", PARENT_TOKEN, **params)
    assert response.status_code == 422


def test_missing_flag_key_is_treated_as_false(client: TestClient) -> None:
    """Older rows without a flag key match ``flag=false`` filters, never crash."""
    session_id = _make_session(client)
    _seed_track(client, session_id, 1, "C1")
    with _db(client) as db:
        row = db.query(BallTrack).one()
        row.flags = {}
    assert (
        _get(client, f"/sessions/{session_id}/tracks", PARENT_TOKEN, identity_risk=False).json()
        != []
    )
    assert (
        _get(client, f"/sessions/{session_id}/tracks", PARENT_TOKEN, identity_risk=True).json()
        == []
    )


def test_ball_track_returns_row_and_payload(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_tracks(client, session_id)

    response = _get(client, f"/sessions/{session_id}/balls/1/track", PLAYER_TOKEN, camera="C1")
    assert response.status_code == 200
    body = response.json()
    assert (body["ball_no"], body["camera_id"]) == (1, "C1")
    assert body["points_key"] == f"sessions/{session_id}/balls/1/track-C1.json"
    payload = body["payload"]
    assert set(payload) == {"points", "segments", "flags"}
    assert payload["points"][0]["px_x"] == 193.0
    assert set(payload["points"][0]) == {
        "frame_no",
        "ts_ms",
        "px_x",
        "px_y",
        "score",
        "bridged",
    }


def test_ball_track_404_when_no_row(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_tracks(client, session_id)
    response = _get(client, f"/sessions/{session_id}/balls/3/track", PARENT_TOKEN, camera="C3")
    assert response.status_code == 404
    assert "no track for ball 3" in response.json()["detail"]


@pytest.mark.parametrize("params", [{}, {"camera": "C9"}, {"camera": "cam"}])
def test_ball_track_requires_valid_camera_param(client: TestClient, params: dict[str, str]) -> None:
    session_id = _make_session(client)
    _seed_track(client, session_id, 1, "C1")
    response = _get(client, f"/sessions/{session_id}/balls/1/track", PARENT_TOKEN, **params)
    assert response.status_code == 422


def test_ball_track_rejects_ball_no_below_one(client: TestClient) -> None:
    session_id = _make_session(client)
    response = _get(client, f"/sessions/{session_id}/balls/0/track", PARENT_TOKEN, camera="C1")
    assert response.status_code == 422


def test_missing_payload_is_a_conflict_not_a_500(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_track(client, session_id, 1, "C1", with_payload=False)
    response = _get(client, f"/sessions/{session_id}/balls/1/track", PARENT_TOKEN, camera="C1")
    assert response.status_code == 409
    assert "missing from store" in response.json()["detail"]


def test_corrupt_payload_is_a_conflict_not_a_500(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_track(client, session_id, 1, "C1", with_payload=False)
    key = f"sessions/{session_id}/balls/1/track-C1.json"
    _store(client).put(key, b"{not json")
    response = _get(client, f"/sessions/{session_id}/balls/1/track", PARENT_TOKEN, camera="C1")
    assert response.status_code == 409
    assert "unreadable" in response.json()["detail"]


def test_non_object_payload_is_a_conflict_not_a_500(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_track(client, session_id, 1, "C1", with_payload=False)
    key = f"sessions/{session_id}/balls/1/track-C1.json"
    _store(client).put(key, b"[1, 2, 3]")
    response = _get(client, f"/sessions/{session_id}/balls/1/track", PARENT_TOKEN, camera="C1")
    assert response.status_code == 409
    assert "malformed" in response.json()["detail"]


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_all_roles_can_read_every_endpoint(client: TestClient, token: str) -> None:
    session_id = _make_session(client)
    _seed_standard_tracks(client, session_id)
    assert _get(client, f"/sessions/{session_id}/tracks", token).status_code == 200
    assert (
        _get(client, f"/sessions/{session_id}/balls/1/track", token, camera="C1").status_code == 200
    )


def test_requires_bearer_token(client: TestClient) -> None:
    session_id = _make_session(client)
    for path, params in (
        (f"/sessions/{session_id}/tracks", {}),
        (f"/sessions/{session_id}/balls/1/track", {"camera": "C1"}),
    ):
        assert client.get(path, params=params).status_code == 401
        assert client.get(path, params=params, headers=auth("wrong-token")).status_code == 401


def test_player_cannot_see_guest_sessions(client: TestClient) -> None:
    """US-L3: guest-player data is parent/coach-only; players get 404, not 403."""
    session_id = _make_session(client, is_guest=True)
    _seed_standard_tracks(client, session_id)
    for path, params in (
        (f"/sessions/{session_id}/tracks", {}),
        (f"/sessions/{session_id}/balls/1/track", {"camera": "C1"}),
    ):
        assert _get(client, path, PLAYER_TOKEN, **params).status_code == 404
        assert _get(client, path, COACH_TOKEN, **params).status_code == 200
        assert _get(client, path, PARENT_TOKEN, **params).status_code == 200


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_unknown_session_is_404(client: TestClient, token: str) -> None:
    assert _get(client, f"/sessions/{UNKNOWN_SESSION}/tracks", token).status_code == 404
    assert (
        _get(client, f"/sessions/{UNKNOWN_SESSION}/balls/1/track", token, camera="C1").status_code
        == 404
    )
