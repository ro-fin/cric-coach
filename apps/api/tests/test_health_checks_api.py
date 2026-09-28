"""US-A4 acceptance: run health check via API, persist report, list by session."""

from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def _camera(camera_id: str = "C1", **overrides: object) -> dict[str, object]:
    stats: dict[str, object] = {
        "camera_id": camera_id,
        "frame_stats": {"mean_brightness": 128.0, "frames_delta": 240},
        "measured_fps": 240.0,
    }
    stats.update(overrides)
    return stats


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "cameras": [_camera("C1"), _camera("C2")],
        "target_fps": 240.0,
        "free_disk_bytes": 100_000_000_000,
        "expected_session_bytes": 10_000_000_000,
        "max_sync_offset_ms": 2.0,
    }
    payload.update(overrides)
    return payload


def _create_session(client: TestClient) -> str:
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
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session_id: str = session["id"]
    return session_id


def test_all_green_returns_201_passed_report(client: TestClient) -> None:
    response = client.post("/health-checks", json=_payload(), headers=auth(PARENT_TOKEN))
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["session_id"] is None
    assert body["passed"] is True
    checks = body["results"]["checks"]
    assert [c["name"] for c in checks] == [
        "feed:C1",
        "fps:C1",
        "exposure:C1",
        "feed:C2",
        "fps:C2",
        "exposure:C2",
        "disk",
        "sync",
    ]
    assert all(c["passed"] for c in checks)


def test_failure_modes_report_named_check(client: TestClient) -> None:
    frozen_stats = {"mean_brightness": 128, "frames_delta": 0}
    frozen = _payload(cameras=[_camera("C1", frame_stats=frozen_stats)])
    throttled = _payload(cameras=[_camera("C1", measured_fps=120.0)])
    full_disk = _payload(free_disk_bytes=1_000)
    desynced = _payload(max_sync_offset_ms=50.0)
    for payload, failing_name in (
        (frozen, "feed:C1"),
        (throttled, "fps:C1"),
        (full_disk, "disk"),
        (desynced, "sync"),
    ):
        body = client.post("/health-checks", json=payload, headers=auth(COACH_TOKEN)).json()
        assert body["passed"] is False
        failed = [c["name"] for c in body["results"]["checks"] if not c["passed"]]
        assert failed == [failing_name]


def test_persistence_round_trip_with_session(client: TestClient) -> None:
    session_id = _create_session(client)
    created = client.post(
        "/health-checks",
        json=_payload(session_id=session_id),
        headers=auth(PARENT_TOKEN),
    ).json()
    assert created["session_id"] == session_id

    listed = client.get(
        "/health-checks", params={"session_id": session_id}, headers=auth(PLAYER_TOKEN)
    )
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [created["id"]]
    assert listed.json()[0]["results"] == created["results"]


def test_list_without_filter_returns_all_records(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post("/health-checks", json=_payload(), headers=auth(PARENT_TOKEN))
    client.post("/health-checks", json=_payload(session_id=session_id), headers=auth(PARENT_TOKEN))
    everything = client.get("/health-checks", headers=auth(COACH_TOKEN)).json()
    assert len(everything) == 2
    filtered = client.get(
        "/health-checks", params={"session_id": session_id}, headers=auth(COACH_TOKEN)
    ).json()
    assert len(filtered) == 1


def test_unknown_session_id_rejected(client: TestClient) -> None:
    payload = _payload(session_id="00000000-0000-0000-0000-000000000000")
    response = client.post("/health-checks", json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert "unknown session_id" in response.json()["detail"]


def test_player_cannot_run_health_check(client: TestClient) -> None:
    response = client.post("/health-checks", json=_payload(), headers=auth(PLAYER_TOKEN))
    assert response.status_code == 403
    unauthenticated = client.post("/health-checks", json=_payload())
    assert unauthenticated.status_code == 401


def test_invalid_payloads_rejected(client: TestClient) -> None:
    no_cameras = client.post(
        "/health-checks", json=_payload(cameras=[]), headers=auth(PARENT_TOKEN)
    )
    assert no_cameras.status_code == 422

    zero_fps = client.post(
        "/health-checks", json=_payload(target_fps=0), headers=auth(PARENT_TOKEN)
    )
    assert zero_fps.status_code == 422
    assert any("target_fps" in str(e["loc"]) for e in zero_fps.json()["detail"])
