"""US-D2 acceptance: clip listing/filtering, per-ball camera set, loud gap report.

Clip rows are seeded directly (the cut_clips worker job owns writing them);
these tests cover the read surfaces, RBAC and US-L3 guest scoping.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from cricai_data.enums import ClipStatus
from cricai_data.models import Clip
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


def _seed_clip(
    client: TestClient,
    session_id: str,
    ball_no: int,
    camera_id: str,
    *,
    status: ClipStatus = ClipStatus.CUT,
    error: str | None = None,
    start_ms: int = 500,
    end_ms: int = 8000,
) -> None:
    object_key = (
        f"sessions/{session_id}/balls/{ball_no}/{camera_id}.mp4"
        if status is ClipStatus.CUT
        else None
    )
    with _db(client) as db:
        db.add(
            Clip(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id=camera_id,
                object_key=object_key,
                start_ms=start_ms,
                end_ms=end_ms,
                status=status,
                error=error,
            )
        )


def _seed_standard_clips(client: TestClient, session_id: str) -> None:
    """Ball 1: both cameras cut; ball 2: C1 cut, C2 gap; ball 3: C1 failed,
    C2 pending."""
    _seed_clip(client, session_id, 1, "C1")
    _seed_clip(client, session_id, 1, "C2")
    _seed_clip(client, session_id, 2, "C1")
    _seed_clip(
        client,
        session_id,
        2,
        "C2",
        status=ClipStatus.GAP,
        error="no video uploaded for camera C2",
    )
    _seed_clip(client, session_id, 3, "C1", status=ClipStatus.FAILED, error="moov atom not found")
    _seed_clip(client, session_id, 3, "C2", status=ClipStatus.PENDING)


def _get(client: TestClient, path: str, token: str, **params: Any) -> Any:
    return client.get(path, headers=auth(token), params=params)


def test_list_clips_returns_every_row_ordered(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)

    response = _get(client, f"/sessions/{session_id}/clips", PARENT_TOKEN)
    assert response.status_code == 200
    body = response.json()
    assert [(row["ball_no"], row["camera_id"]) for row in body] == [
        (1, "C1"),
        (1, "C2"),
        (2, "C1"),
        (2, "C2"),
        (3, "C1"),
        (3, "C2"),
    ]
    first = body[0]
    assert first["session_id"] == session_id
    assert first["object_key"] == f"sessions/{session_id}/balls/1/C1.mp4"
    assert (first["start_ms"], first["end_ms"]) == (500, 8000)
    assert first["status"] == "cut"
    assert first["error"] is None
    gap = body[3]
    assert (gap["status"], gap["object_key"]) == ("gap", None)
    assert gap["error"] == "no video uploaded for camera C2"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"ball_no": 2}, [(2, "C1"), (2, "C2")]),
        ({"camera_id": "C2"}, [(1, "C2"), (2, "C2"), (3, "C2")]),
        ({"status": "cut"}, [(1, "C1"), (1, "C2"), (2, "C1")]),
        ({"status": "gap"}, [(2, "C2")]),
        ({"ball_no": 3, "camera_id": "C1", "status": "failed"}, [(3, "C1")]),
        ({"ball_no": 3, "status": "cut"}, []),
    ],
)
def test_list_clips_filters(
    client: TestClient, params: dict[str, Any], expected: list[tuple[int, str]]
) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)

    response = _get(client, f"/sessions/{session_id}/clips", COACH_TOKEN, **params)
    assert response.status_code == 200
    assert [(row["ball_no"], row["camera_id"]) for row in response.json()] == expected


@pytest.mark.parametrize(
    "params",
    [{"ball_no": 0}, {"camera_id": "C9"}, {"camera_id": "cam1"}, {"status": "bogus"}],
)
def test_list_clips_rejects_invalid_filters(client: TestClient, params: dict[str, Any]) -> None:
    session_id = _make_session(client)
    response = _get(client, f"/sessions/{session_id}/clips", PARENT_TOKEN, **params)
    assert response.status_code == 422


def test_gap_report_groups_missing_cameras_per_ball(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)

    response = _get(client, f"/sessions/{session_id}/clips/gaps", PARENT_TOKEN)
    assert response.status_code == 200
    assert response.json() == [
        {
            "ball_no": 2,
            "missing_cameras": ["C2"],
            "reasons": {"C2": "gap: no video uploaded for camera C2"},
        },
        {
            "ball_no": 3,
            "missing_cameras": ["C1", "C2"],
            "reasons": {"C1": "failed: moov atom not found", "C2": "pending"},
        },
    ]


def test_gap_report_empty_when_everything_cut(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_clip(client, session_id, 1, "C1")
    response = _get(client, f"/sessions/{session_id}/clips/gaps", COACH_TOKEN)
    assert response.status_code == 200
    assert response.json() == []


def test_ball_clips_returns_camera_set_for_dashboard_switching(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)

    response = _get(client, f"/sessions/{session_id}/balls/2/clips", PLAYER_TOKEN)
    assert response.status_code == 200
    body = response.json()
    assert body["ball_no"] == 2
    assert [row["camera_id"] for row in body["cameras"]] == ["C1", "C2"]
    assert body["cameras"][0]["object_key"] == f"sessions/{session_id}/balls/2/C1.mp4"
    assert body["cameras"][0]["status"] == "cut"
    assert (body["cameras"][1]["status"], body["cameras"][1]["object_key"]) == ("gap", None)


def test_ball_clips_404_when_ball_has_no_rows(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)
    response = _get(client, f"/sessions/{session_id}/balls/9/clips", PARENT_TOKEN)
    assert response.status_code == 404


def test_ball_clips_rejects_ball_no_below_one(client: TestClient) -> None:
    session_id = _make_session(client)
    response = _get(client, f"/sessions/{session_id}/balls/0/clips", PARENT_TOKEN)
    assert response.status_code == 422


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_all_roles_can_read_every_endpoint(client: TestClient, token: str) -> None:
    session_id = _make_session(client)
    _seed_standard_clips(client, session_id)
    assert _get(client, f"/sessions/{session_id}/clips", token).status_code == 200
    assert _get(client, f"/sessions/{session_id}/clips/gaps", token).status_code == 200
    assert _get(client, f"/sessions/{session_id}/balls/1/clips", token).status_code == 200


def test_requires_bearer_token(client: TestClient) -> None:
    session_id = _make_session(client)
    for path in (
        f"/sessions/{session_id}/clips",
        f"/sessions/{session_id}/clips/gaps",
        f"/sessions/{session_id}/balls/1/clips",
    ):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=auth("wrong-token")).status_code == 401


def test_player_cannot_see_guest_sessions(client: TestClient) -> None:
    """US-L3: guest-player data is parent/coach-only; players get 404, not 403."""
    session_id = _make_session(client, is_guest=True)
    _seed_standard_clips(client, session_id)
    for path in (
        f"/sessions/{session_id}/clips",
        f"/sessions/{session_id}/clips/gaps",
        f"/sessions/{session_id}/balls/1/clips",
    ):
        assert _get(client, path, PLAYER_TOKEN).status_code == 404
        assert _get(client, path, COACH_TOKEN).status_code == 200
        assert _get(client, path, PARENT_TOKEN).status_code == 200


@pytest.mark.parametrize("token", ALL_TOKENS)
def test_unknown_session_is_404(client: TestClient, token: str) -> None:
    assert _get(client, f"/sessions/{UNKNOWN_SESSION}/clips", token).status_code == 404
    assert _get(client, f"/sessions/{UNKNOWN_SESSION}/clips/gaps", token).status_code == 404
    assert _get(client, f"/sessions/{UNKNOWN_SESSION}/balls/1/clips", token).status_code == 404
