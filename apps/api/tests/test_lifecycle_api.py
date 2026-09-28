"""US-A3 acceptance: start/stop as one unit — checklist gate, registry-validated
cameras, capture-state evidence rule (US-B2 seam), degraded capture, idempotency."""

import uuid
from pathlib import Path

import httpx
import pytest
from cricai_api.services.capture_state import EVIDENCE_STATUSES, recompute_missing_views
from cricai_data.enums import VideoStatus
from cricai_data.models import ChecklistAck, Session, Video
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

MISSING_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _register_camera(client: TestClient, camera_id: str, fps: int = 120) -> None:
    response = client.post(
        "/cameras",
        json={
            "camera_id": camera_id,
            "position_label": f"{camera_id} test rig",
            "xyz_offset_m": {"x": 0.0, "y": 3.0, "z": 1.2},
            "height_m": 1.2,
            "fps": fps,
            "resolution": "1920x1080",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _create_session(client: TestClient, bowler_source: str = "coach") -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    payload: dict[str, object] = {
        "player_id": player["id"],
        "date": "2026-07-07",
        "session_type": "batting",
        "bowler_source": bowler_source,
    }
    if bowler_source == "machine":
        payload["machine_settings"] = {"speed_kph": 85, "length": "good"}
    response = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 201
    session_id: str = response.json()["id"]
    return session_id


def _start(
    client: TestClient, session_id: str, cameras: list[str], token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/start", json={"cameras": cameras}, headers=auth(token)
    )


def _stop(
    client: TestClient,
    session_id: str,
    reporting: dict[str, dict[str, bool]] | None = None,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    body: dict[str, object] = {}
    if reporting is not None:
        body["cameras_reporting"] = reporting
    return client.post(f"/sessions/{session_id}/stop", json=body, headers=auth(token))


def _add_video_row(
    app: FastAPI,
    session_id: str,
    camera_id: str,
    video_status: VideoStatus = VideoStatus.UPLOADED,
) -> None:
    """Seed footage evidence directly: a Video row with the given status."""
    token = uuid.uuid4().hex
    db = app.state.session_factory()
    try:
        db.add(
            Video(
                session_id=uuid.UUID(session_id),
                camera_id=camera_id,
                object_key=f"sessions/{session_id}/{camera_id}/{token}.mp4",
                filename=f"{token}.mp4",
                checksum_sha256=token * 2,
                size_bytes=1,
                status=video_status,
            )
        )
        db.commit()
    finally:
        db.close()


def _attach_checklist_ack(app: FastAPI, session_id: str) -> None:
    db = app.state.session_factory()
    try:
        ack = ChecklistAck(items={"area_clear": True, "helmet_on": True}, acked_by="parent")
        db.add(ack)
        db.flush()
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        session.checklist_ack_id = ack.id
        db.commit()
    finally:
        db.close()


def test_full_happy_path(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C1", fps=120)
    _register_camera(client, "C2", fps=240)
    session_id = _create_session(client)

    before = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert before == {
        "state": "created",
        "degraded": False,
        "missing_views": [],
        "started_at": None,
        "stopped_at": None,
    }

    started = _start(client, session_id, ["C1", "C2"])
    assert started.status_code == 200
    body = started.json()
    assert body["state"] == "recording"
    assert body["idempotent"] is False
    assert body["cameras"] == ["C1", "C2"]
    assert body["warnings"] == []
    assert body["started_at"] is not None

    _add_video_row(app, session_id, "C1")
    _add_video_row(app, session_id, "C2")
    stopped = _stop(client, session_id, {"C1": {"ok": True}, "C2": {"ok": True}})
    assert stopped.status_code == 200
    stop_body = stopped.json()
    assert stop_body["state"] == "captured"
    assert stop_body["degraded"] is False
    assert stop_body["missing_views"] == []
    assert stop_body["stopped_at"] is not None
    assert stop_body["idempotent"] is False

    after = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PLAYER_TOKEN)).json()
    assert after["state"] == "captured"
    assert after["degraded"] is False
    assert after["started_at"] == body["started_at"]
    assert after["stopped_at"] == stop_body["stopped_at"]


@pytest.mark.safety
def test_machine_session_blocked_until_checklist_acked(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client, bowler_source="machine")

    blocked = _start(client, session_id, ["C1"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "safety checklist not acknowledged"
    state = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert state["state"] == "created"
    assert state["started_at"] is None

    _attach_checklist_ack(app, session_id)
    allowed = _start(client, session_id, ["C1"])
    assert allowed.status_code == 200
    assert allowed.json()["state"] == "recording"


def test_start_rejects_unregistered_cameras(client: TestClient) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)

    response = _start(client, session_id, ["C1", "C4", "C3"])
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "cameras not registered as active in the camera registry"
    assert detail["unknown_cameras"] == ["C3", "C4"]  # sorted, C1 accepted

    state = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert state["state"] == "created"
    assert state["started_at"] is None


def test_start_warns_when_primary_camera_fps_below_120(client: TestClient) -> None:
    _register_camera(client, "C1", fps=60)
    _register_camera(client, "C2", fps=60)  # only C1 carries the US-A2 floor
    session_id = _create_session(client)

    response = _start(client, session_id, ["C1", "C2"])
    assert response.status_code == 200  # non-blocking: config asserted, not enforced
    body = response.json()
    assert body["state"] == "recording"
    assert body["warnings"] == [
        "C1 is registered at 60 fps, below the 120 fps floor for analysis-grade capture (US-A2)"
    ]


def test_start_without_primary_camera_has_no_fps_warning(client: TestClient) -> None:
    _register_camera(client, "C2", fps=30)
    session_id = _create_session(client)

    response = _start(client, session_id, ["C2"])
    assert response.status_code == 200
    assert response.json()["warnings"] == []


def test_start_dedupes_sorts_and_persists_expected_cameras(client: TestClient) -> None:
    _register_camera(client, "C1")
    _register_camera(client, "C2")
    session_id = _create_session(client)

    started = _start(client, session_id, ["C2", "C1", "C2"])
    assert started.status_code == 200
    assert started.json()["cameras"] == ["C1", "C2"]

    # No evidence for either camera: the persisted roster drives the verdict.
    stopped = _stop(client, session_id).json()
    assert stopped["degraded"] is True
    assert stopped["missing_views"] == ["C1", "C2"]


def test_stop_flags_degraded_with_missing_views(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C1")
    _register_camera(client, "C2")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1", "C2"]).status_code == 200
    _add_video_row(app, session_id, "C1")

    stopped = _stop(client, session_id, {"C1": {"ok": True}})
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["state"] == "captured"
    assert body["degraded"] is True
    assert body["missing_views"] == ["C2"]

    view = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(COACH_TOKEN)).json()
    assert view["degraded"] is True
    assert view["missing_views"] == ["C2"]


@pytest.mark.parametrize(
    "video_status",
    [VideoStatus.UPLOADED, VideoStatus.PROBED, VideoStatus.METADATA_CONFLICT],
)
def test_evidence_status_video_row_counts_as_footage(
    app: FastAPI, client: TestClient, video_status: VideoStatus
) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1"]).status_code == 200
    _add_video_row(app, session_id, "C1", video_status=video_status)

    body = _stop(client, session_id).json()
    assert body["degraded"] is False
    assert body["missing_views"] == []


@pytest.mark.parametrize("video_status", [VideoStatus.FAILED, VideoStatus.PENDING])
def test_failed_or_pending_video_row_is_not_evidence(
    app: FastAPI, client: TestClient, video_status: VideoStatus
) -> None:
    """A FAILED row had its bytes deleted (checksum mismatch): never evidence."""
    _register_camera(client, "C1")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1"]).status_code == 200
    _add_video_row(app, session_id, "C1", video_status=video_status)

    body = _stop(client, session_id).json()
    assert body["degraded"] is True
    assert body["missing_views"] == ["C1"]


def test_store_objects_without_video_row_are_not_evidence(app: FastAPI, client: TestClient) -> None:
    """Evidence is the Video table, not stray object-store keys (capture_state rule)."""
    _register_camera(client, "C1")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1"]).status_code == 200
    app.state.store.put(f"sessions/{session_id}/C1/raw.mp4", b"frames")

    body = _stop(client, session_id).json()
    assert body["degraded"] is True
    assert body["missing_views"] == ["C1"]


def test_camera_reporting_not_ok_is_missing_even_with_footage(
    app: FastAPI, client: TestClient
) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1"]).status_code == 200
    _add_video_row(app, session_id, "C1")

    stopped = _stop(client, session_id, {"C1": {"ok": False}})
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["degraded"] is True
    assert body["missing_views"] == ["C1"]


def test_camera_reporting_outside_roster_is_ignored(app: FastAPI, client: TestClient) -> None:
    """Only start-validated camera ids can ever appear in missing_views."""
    _register_camera(client, "C1")
    session_id = _create_session(client)
    assert _start(client, session_id, ["C1"]).status_code == 200
    _add_video_row(app, session_id, "C1")

    body = _stop(client, session_id, {"C9": {"ok": False}, "C1": {"ok": True}}).json()
    assert body["degraded"] is False
    assert body["missing_views"] == []


def test_double_start_is_idempotent_and_ignores_payload(client: TestClient) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)
    first = _start(client, session_id, ["C1"]).json()

    # Second start: even an unregistered camera list is ignored — the roster
    # persisted by the first start is the truth.
    second = _start(client, session_id, ["C9"], token=COACH_TOKEN)
    assert second.status_code == 200
    body = second.json()
    assert body["idempotent"] is True
    assert body["state"] == "recording"
    assert body["cameras"] == ["C1"]
    assert body["warnings"] == []
    assert body["started_at"] == first["started_at"]  # not reset


def test_restop_recomputes_instead_of_freezing_the_verdict(
    app: FastAPI, client: TestClient
) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)
    _start(client, session_id, ["C1"])
    _add_video_row(app, session_id, "C1")
    first = _stop(client, session_id).json()
    assert first["missing_views"] == []

    # Re-stop recomputes: an operator failure report now degrades the session,
    # but state and stopped_at stay frozen.
    second = _stop(client, session_id, {"C1": {"ok": False}})
    assert second.status_code == 200
    body = second.json()
    assert body["idempotent"] is True
    assert body["state"] == "captured"
    assert body["degraded"] is True
    assert body["missing_views"] == ["C1"]
    assert body["stopped_at"] == first["stopped_at"]

    # Reports are per-request, not sticky: recomputing from evidence clears it.
    third = _stop(client, session_id).json()
    assert third["idempotent"] is True
    assert third["degraded"] is False
    assert third["missing_views"] == []
    assert third["stopped_at"] == first["stopped_at"]


def test_late_upload_clears_missing_view(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C1")
    _register_camera(client, "C2")
    session_id = _create_session(client)
    _start(client, session_id, ["C1", "C2"])
    _add_video_row(app, session_id, "C1")
    stopped = _stop(client, session_id).json()
    assert stopped["degraded"] is True
    assert stopped["missing_views"] == ["C2"]

    # C2's footage arrives after stop (uploaded-status Video row); the re-stop
    # recompute path clears it — asserted from the lifecycle read endpoint.
    _add_video_row(app, session_id, "C2")
    restop = _stop(client, session_id)
    assert restop.status_code == 200
    assert restop.json()["idempotent"] is True

    view = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert view["state"] == "captured"
    assert view["degraded"] is False
    assert view["missing_views"] == []


def test_invalid_transitions_return_409(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C1")
    session_id = _create_session(client)

    premature = _stop(client, session_id)  # stop a CREATED session
    assert premature.status_code == 409
    assert "cannot transition session from" in premature.json()["detail"]

    _start(client, session_id, ["C1"])
    _add_video_row(app, session_id, "C1")
    _stop(client, session_id)
    restart = _start(client, session_id, ["C1"])  # start a CAPTURED session
    assert restart.status_code == 409
    assert "cannot transition session from" in restart.json()["detail"]


def test_rbac_and_auth(client: TestClient) -> None:
    session_id = _create_session(client)

    assert _start(client, session_id, ["C1"], token=PLAYER_TOKEN).status_code == 403
    assert _stop(client, session_id, token=PLAYER_TOKEN).status_code == 403
    unauthenticated = client.post(f"/sessions/{session_id}/start", json={"cameras": ["C1"]})
    assert unauthenticated.status_code == 401

    readable = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PLAYER_TOKEN))
    assert readable.status_code == 200


def test_unknown_session_is_404(client: TestClient) -> None:
    assert _start(client, MISSING_ID, ["C1"]).status_code == 404
    assert _stop(client, MISSING_ID).status_code == 404
    missing = client.get(f"/sessions/{MISSING_ID}/lifecycle", headers=auth(PARENT_TOKEN))
    assert missing.status_code == 404


def test_camera_id_validation(client: TestClient) -> None:
    session_id = _create_session(client)

    empty = _start(client, session_id, [])
    assert empty.status_code == 422

    traversal = _start(client, session_id, ["../etc"])  # rejected before any lookup
    assert traversal.status_code == 422

    # Reporting keys are camera ids too — same storage-safe pattern applies.
    bad_stop = _stop(client, session_id, {"C1/evil": {"ok": True}})
    assert bad_stop.status_code == 422


# ------------------------------------------------- capture_state unit seams


@pytest.mark.safety
def test_evidence_statuses_exclude_failed_and_pending() -> None:
    assert VideoStatus.FAILED not in EVIDENCE_STATUSES
    assert VideoStatus.PENDING not in EVIDENCE_STATUSES


def test_recompute_is_noop_before_expected_cameras_exist(app: FastAPI, client: TestClient) -> None:
    """Sessions that never started have no roster: recompute must not invent one."""
    session_id = _create_session(client)
    db = app.state.session_factory()
    try:
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        assert session.expected_cameras == []
        session.missing_views = ["sentinel"]
        session.degraded = True
        recompute_missing_views(db, session)
        assert session.missing_views == ["sentinel"]  # untouched
        assert session.degraded is True
    finally:
        db.close()
