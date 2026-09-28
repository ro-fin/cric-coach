"""US-A1 acceptance: camera registry eras, validation, RBAC, FOV references.

US-I1 acceptance: camera roles make C5-C7 real bowling-capture registrations
(register/patch role, role-filtered listing, reserved-flag semantics),
7-camera sessions start against the registry, and health/sync checks stay
visible per bowling camera — including the C7 240-FPS tier.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def _payload(camera_id: str = "C1", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "camera_id": camera_id,
        "position_label": "side-on square of batter",
        "xyz_offset_m": {"x": 5.0, "y": 0.0, "z": 1.2},
        "height_m": 1.2,
        "fps": 120,
        "resolution": "1920x1080",
        "lens": "wide",
        "mount": "tripod",
        "protected": False,
    }
    payload.update(overrides)
    return payload


def _create(client: TestClient, camera_id: str = "C1", **overrides: object) -> dict[str, object]:
    response = client.post(
        "/cameras", json=_payload(camera_id, **overrides), headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


def test_create_first_config_is_era_1_and_active(client: TestClient) -> None:
    body = _create(client, "C1")
    assert body["era_no"] == 1
    assert body["active"] is True
    assert body["reserved_for_future"] is False
    assert body["xyz_offset_m"] == {"x": 5.0, "y": 0.0, "z": 1.2}
    assert body["fov_reference_key"] is None


def test_recreate_increments_era_and_deactivates_previous(client: TestClient) -> None:
    first = _create(client, "C1")
    second = _create(client, "C1", position_label="moved 1m back")
    assert second["era_no"] == 2
    assert second["active"] is True

    active = client.get("/cameras", headers=auth(COACH_TOKEN)).json()
    assert [(c["camera_id"], c["era_no"]) for c in active] == [("C1", 2)]

    history = client.get("/cameras?include_history=true", headers=auth(COACH_TOKEN)).json()
    assert [(c["era_no"], c["active"]) for c in history] == [(1, False), (2, True)]
    assert history[0]["id"] == first["id"]


def test_get_camera_returns_active_era(client: TestClient) -> None:
    _create(client, "C2")
    _create(client, "C2")
    _create(client, "C2")
    body = client.get("/cameras/C2", headers=auth(PLAYER_TOKEN)).json()
    assert body["era_no"] == 3
    assert body["active"] is True


def test_get_unknown_camera_404(client: TestClient) -> None:
    response = client.get("/cameras/C4", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_list_orders_by_camera_then_era(client: TestClient) -> None:
    _create(client, "C3")
    _create(client, "C1")
    _create(client, "C1")
    listed = client.get("/cameras", headers=auth(PLAYER_TOKEN)).json()
    assert [(c["camera_id"], c["era_no"]) for c in listed] == [("C1", 2), ("C3", 1)]


def test_validation_rejects_bad_fields(client: TestClient) -> None:
    bad_payloads = [
        _payload(camera_id="C9"),
        _payload(camera_id="X1"),
        _payload(fps=90),
        _payload(height_m=-0.5),
        _payload(height_m=0),
        _payload(resolution="1080p"),
        _payload(resolution="1920x"),
    ]
    for bad in bad_payloads:
        response = client.post("/cameras", json=bad, headers=auth(PARENT_TOKEN))
        assert response.status_code == 422, bad


def test_rbac_only_parent_creates(client: TestClient) -> None:
    for token in (PLAYER_TOKEN, COACH_TOKEN):
        response = client.post("/cameras", json=_payload(), headers=auth(token))
        assert response.status_code == 403


def test_reserved_cameras_c5_to_c8_flagged_but_active(client: TestClient) -> None:
    for camera_id in ("C5", "C6", "C7", "C8"):
        body = _create(client, camera_id)
        assert body["reserved_for_future"] is True
        assert body["active"] is True


def test_fov_reference_stores_object_and_records_key(client: TestClient, tmp_path: Path) -> None:
    _create(client, "C1")
    response = client.post(
        "/cameras/C1/fov-reference",
        content=b"\x89PNG fake bytes",
        headers={**auth(PARENT_TOKEN), "Content-Type": "image/png"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fov_reference_key"] == "calibration/fov/C1/era1.png"

    stored = tmp_path / "storage" / "objects" / "calibration" / "fov" / "C1" / "era1.png"
    assert stored.read_bytes() == b"\x89PNG fake bytes"

    fetched = client.get("/cameras/C1", headers=auth(PARENT_TOKEN)).json()
    assert fetched["fov_reference_key"] == "calibration/fov/C1/era1.png"


def test_fov_reference_key_tracks_era(client: TestClient) -> None:
    _create(client, "C4")
    _create(client, "C4")
    response = client.post(
        "/cameras/C4/fov-reference",
        content=b"png",
        headers={**auth(PARENT_TOKEN), "Content-Type": "image/png"},
    )
    assert response.json()["fov_reference_key"] == "calibration/fov/C4/era2.png"


def test_fov_reference_unknown_camera_404(client: TestClient) -> None:
    response = client.post(
        "/cameras/C8/fov-reference",
        content=b"png",
        headers={**auth(PARENT_TOKEN), "Content-Type": "image/png"},
    )
    assert response.status_code == 404


def test_fov_reference_requires_parent(client: TestClient) -> None:
    _create(client, "C1")
    response = client.post(
        "/cameras/C1/fov-reference",
        content=b"png",
        headers={**auth(PLAYER_TOKEN), "Content-Type": "image/png"},
    )
    assert response.status_code == 403


def test_fov_reference_empty_body_422(client: TestClient) -> None:
    _create(client, "C1")
    response = client.post(
        "/cameras/C1/fov-reference",
        content=b"",
        headers={**auth(PARENT_TOKEN), "Content-Type": "image/png"},
    )
    assert response.status_code == 422


# --- US-I1: camera roles — C5-C7 become real bowling-capture registrations ---

#: The Phase-6 bowling-capture trio and their registry roles (US-I1).
BOWLING_CAMERAS = (
    ("C5", "bowling_side", 120, False),
    ("C6", "front_on", 120, False),
    ("C7", "wrist", 240, True),
)


def _register_bowling_trio(client: TestClient) -> None:
    for camera_id, role, fps, protected in BOWLING_CAMERAS:
        _create(client, camera_id, role=role, fps=fps, protected=protected)


def test_register_with_role_is_real_not_reserved(client: TestClient) -> None:
    for camera_id, role, fps, protected in BOWLING_CAMERAS:
        body = _create(client, camera_id, role=role, fps=fps, protected=protected)
        assert body["role"] == role
        assert body["reserved_for_future"] is False
        assert body["active"] is True


def test_register_without_role_has_null_role(client: TestClient) -> None:
    body = _create(client, "C1")
    assert body["role"] is None
    assert body["reserved_for_future"] is False  # C1-C4 were never reserved


def test_new_era_without_role_returns_to_reserved(client: TestClient) -> None:
    _create(client, "C5", role="bowling_side")
    moved = _create(client, "C5")  # camera moved, role not re-declared
    assert moved["era_no"] == 2
    assert moved["role"] is None
    assert moved["reserved_for_future"] is True


def test_register_rejects_unknown_role(client: TestClient) -> None:
    response = client.post(
        "/cameras", json=_payload("C5", role="broadcast"), headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 422


def test_patch_role_sets_role_without_new_era(client: TestClient) -> None:
    _create(client, "C5")
    response = client.patch(
        "/cameras/C5", json={"role": "bowling_side"}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "bowling_side"
    assert body["era_no"] == 1  # no move: calibration/FOV references stay valid
    assert body["reserved_for_future"] is False

    fetched = client.get("/cameras/C5", headers=auth(COACH_TOKEN)).json()
    assert fetched["role"] == "bowling_side"


def test_patch_role_explicit_null_clears_role(client: TestClient) -> None:
    _create(client, "C7", role="wrist", fps=240)
    response = client.patch("/cameras/C7", json={"role": None}, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json()["role"] is None
    assert response.json()["reserved_for_future"] is True


def test_patch_role_omitted_field_422(client: TestClient) -> None:
    _create(client, "C5")
    response = client.patch("/cameras/C5", json={}, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422


def test_patch_role_unknown_camera_404(client: TestClient) -> None:
    response = client.patch("/cameras/C6", json={"role": "front_on"}, headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_patch_role_requires_parent(client: TestClient) -> None:
    _create(client, "C5")
    for token in (COACH_TOKEN, PLAYER_TOKEN):
        response = client.patch("/cameras/C5", json={"role": "bowling_side"}, headers=auth(token))
        assert response.status_code == 403


def test_list_filters_by_role(client: TestClient) -> None:
    _create(client, "C1", role="batting_side")
    _register_bowling_trio(client)
    _create(client, "C2")  # no role

    bowling = client.get("/cameras?role=bowling_side", headers=auth(PLAYER_TOKEN)).json()
    assert [c["camera_id"] for c in bowling] == ["C5"]

    wrist = client.get("/cameras?role=wrist", headers=auth(COACH_TOKEN)).json()
    assert [(c["camera_id"], c["fps"]) for c in wrist] == [("C7", 240)]

    everything = client.get("/cameras", headers=auth(COACH_TOKEN)).json()
    assert len(everything) == 5  # no filter: unchanged behaviour


def test_list_role_filter_rejects_unknown_role(client: TestClient) -> None:
    response = client.get("/cameras?role=broadcast", headers=auth(COACH_TOKEN))
    assert response.status_code == 422


# --- US-I1: 7-camera session support -----------------------------------------


def _create_session(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
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


def test_seven_camera_session_start_persists_roster(client: TestClient) -> None:
    """US-I1: a bowling session starts with the full C1-C7 roster; the roles
    ride the registry, the roster rides the session (Epic A ACs hold)."""
    for camera_id in ("C1", "C2", "C3", "C4"):
        _create(client, camera_id)
    _register_bowling_trio(client)
    session_id = _create_session(client)

    seven = ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]
    response = client.post(
        f"/sessions/{session_id}/start", json={"cameras": seven}, headers=auth(COACH_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cameras"] == seven
    assert body["warnings"] == []

    lifecycle = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PLAYER_TOKEN)).json()
    assert lifecycle["state"] == "recording"


def test_seven_camera_start_rejects_unregistered_bowling_cams(client: TestClient) -> None:
    """The registry gate (US-A1) protects bowling capture too: C5-C7 must be
    registered before they can join a roster."""
    for camera_id in ("C1", "C2", "C3", "C4"):
        _create(client, camera_id)
    session_id = _create_session(client)
    response = client.post(
        f"/sessions/{session_id}/start",
        json={"cameras": ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["unknown_cameras"] == ["C5", "C6", "C7"]


# --- US-I1: health/sync visibility for bowling cameras -----------------------


def _camera_stats(camera_id: str, measured_fps: float) -> dict[str, object]:
    return {
        "camera_id": camera_id,
        "frame_stats": {"mean_brightness": 128.0, "frames_delta": 120},
        "measured_fps": measured_fps,
    }


def _check_names(body: dict[str, object]) -> dict[str, bool]:
    results = body["results"]
    assert isinstance(results, dict)
    checks = results["checks"]
    assert isinstance(checks, list)
    return {str(check["name"]): bool(check["passed"]) for check in checks}


def test_seven_camera_health_check_reports_every_bowling_cam(client: TestClient) -> None:
    """US-I1 REG: the existing health-check framework is fleet-size-agnostic —
    a 7-camera run records per-camera probes for C5-C7 plus the fleet sync."""
    response = client.post(
        "/health-checks",
        json={
            "cameras": [
                _camera_stats(camera_id, 120.0)
                for camera_id in ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
            ],
            "target_fps": 120.0,
            "free_disk_bytes": 10**12,
            "expected_session_bytes": 10**9,
            "max_sync_offset_ms": 2.0,
        },
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["passed"] is True
    verdicts = _check_names(body)
    for camera_id in ("C5", "C6", "C7"):
        for probe in ("feed", "fps", "exposure"):
            assert verdicts[f"{probe}:{camera_id}"] is True
    assert verdicts["sync"] is True


def test_wrist_tier_health_check_fails_throttled_240fps(client: TestClient) -> None:
    """US-I1: C7 runs as its own 240-FPS tier so a throttled wrist camera
    fails loudly instead of hiding under the 120-FPS fleet target."""
    response = client.post(
        "/health-checks",
        json={
            "cameras": [_camera_stats("C7", 200.0)],
            "target_fps": 240.0,
            "free_disk_bytes": 10**12,
            "expected_session_bytes": 10**9,
            "max_sync_offset_ms": 1.0,
        },
        headers=auth(COACH_TOKEN),
    )
    body = response.json()
    assert body["passed"] is False
    assert _check_names(body)["fps:C7"] is False


def test_wrist_tier_sync_budget_tighter_than_fleet(client: TestClient) -> None:
    """One frame at 240 FPS is ~4.2 ms: an offset the 120-FPS fleet tolerates
    must fail the wrist tier (docs/camera_setup.md sync rule)."""

    def run(target_fps: float) -> dict[str, bool]:
        response = client.post(
            "/health-checks",
            json={
                "cameras": [_camera_stats("C7", target_fps)],
                "target_fps": target_fps,
                "free_disk_bytes": 10**12,
                "expected_session_bytes": 10**9,
                "max_sync_offset_ms": 5.0,
            },
            headers=auth(COACH_TOKEN),
        )
        return _check_names(response.json())

    assert run(120.0)["sync"] is True  # 5 ms within the ~8.3 ms fleet budget
    assert run(240.0)["sync"] is False  # 5 ms exceeds the ~4.2 ms wrist budget
