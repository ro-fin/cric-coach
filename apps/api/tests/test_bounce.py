"""US-C5 acceptance: bounce-point clicks -> pitch coordinates, zones, agreement.

Extrinsic calibrations are seeded directly in the DB using the extrinsics
``to_params`` contract shape, hand-built from a simple analytic homography
``pitch = (scale * u, scale * v - 1.525)`` so every pixel->pitch mapping in
these tests is checkable without the real extrinsics module. Intrinsic seeds
use the intrinsics ``to_params`` shape with a single radial ``k1`` term whose
forward model is reproduced analytically in :func:`_distorted`.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cricai_api.services.bounce_mapping import (
    MappingUnavailableError,
    map_click,
    recompute_session_marks,
    reevaluate_agreement,
)
from cricai_data.enums import CalibrationKind, Handedness, Length, Line
from cricai_data.models import Calibration
from cricai_data.models import Session as SessionModel
from cricai_vision.trajectory import BouncePoint, TargetZone, score_delivery
from cricai_vision.zones import ZoneConfig
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
SCALE = 0.01  # meters per pixel in the hand-built homography
Y_OFFSET_M = -1.525

#: Pinhole used by intrinsic seeds: focal length and principal point in px.
FOCAL_PX = 1000.0
CENTER_X_PX = 960.0
CENTER_Y_PX = 540.0
#: Radial distortion strong enough to shift a test click by ~19 px (~0.19 m).
K1 = -0.2


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _params(scale: float = SCALE) -> dict[str, Any]:
    """Extrinsics ``to_params`` shape: pitch = (scale*u, scale*v - 1.525)."""
    return {
        "version": 1,
        "matrix": [[scale, 0.0, 0.0], [0.0, scale, Y_OFFSET_M], [0.0, 0.0, 1.0]],
        "rms_px": 0.4,
        "rms_m": 0.008,
        "n_landmarks": 8,
    }


def _px_for(pitch_x: float, pitch_y: float) -> dict[str, float]:
    """Invert the analytic homography: the pixel that maps to (pitch_x, pitch_y)."""
    return {"x": pitch_x / SCALE, "y": (pitch_y - Y_OFFSET_M) / SCALE}


def _intrinsic_params(k1: float = 0.0) -> dict[str, Any]:
    """Intrinsics ``to_params`` shape; ``k1`` is the only distortion term."""
    return {
        "version": 1,
        "camera_matrix": [
            [FOCAL_PX, 0.0, CENTER_X_PX],
            [0.0, FOCAL_PX, CENTER_Y_PX],
            [0.0, 0.0, 1.0],
        ],
        "dist_coeffs": [k1, 0.0, 0.0, 0.0, 0.0],
        "reprojection_error_px": 0.3,
        "board_spec": {
            "squares_x": 7,
            "squares_y": 10,
            "square_len_m": 0.08,
            "marker_len_m": 0.06,
            "aruco_dict": "DICT_5X5_100",
        },
        "n_views": 10,
        "captured_on": "2026-07-01",
    }


def _distorted(px: dict[str, float], k1: float) -> dict[str, float]:
    """Forward OpenCV radial model: the distorted pixel a lens shows for ``px``."""
    x = (px["x"] - CENTER_X_PX) / FOCAL_PX
    y = (px["y"] - CENTER_Y_PX) / FOCAL_PX
    factor = 1.0 + k1 * (x * x + y * y)
    return {"x": FOCAL_PX * x * factor + CENTER_X_PX, "y": FOCAL_PX * y * factor + CENTER_Y_PX}


def _seed_calibration(
    engine: Engine,
    camera_id: str,
    *,
    era_no: int = 1,
    scale: float = SCALE,
    valid: bool = True,
    kind: CalibrationKind = CalibrationKind.EXTRINSIC,
    created_at: datetime | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    with DbSession(engine) as db:
        calibration = Calibration(
            camera_id=camera_id,
            era_no=era_no,
            kind=kind,
            params=_params(scale) if params is None else params,
            rms=0.008,
            valid=valid,
        )
        if created_at is not None:
            calibration.created_at = created_at
        db.add(calibration)
        db.commit()
        return str(calibration.id)


def _register_camera(client: TestClient, camera_id: str) -> None:
    response = client.post(
        "/cameras",
        json={
            "camera_id": camera_id,
            "position_label": f"{camera_id} test rig",
            "xyz_offset_m": {"x": 0.0, "y": 3.0, "z": 1.2},
            "height_m": 1.2,
            "fps": 120,
            "resolution": "1920x1080",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _create_session(
    client: TestClient,
    *,
    handedness: str = "right",
    is_guest: bool = False,
    session_type: str = "batting",
) -> str:
    player = client.post(
        "/players",
        json={
            "name": "Visitor" if is_guest else "Arjun",
            "birthdate": "2014-11-20",
            "handedness": handedness,
            "is_guest": is_guest,
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": session_type,
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session_id: str = session["id"]
    return session_id


def _setup_session(
    client: TestClient,
    engine: Engine,
    *,
    handedness: str = "right",
    is_guest: bool = False,
    session_type: str = "batting",
    cameras: tuple[str, ...] = ("C3",),
) -> str:
    for camera_id in cameras:
        _register_camera(client, camera_id)
        _seed_calibration(engine, camera_id)
    return _create_session(
        client, handedness=handedness, is_guest=is_guest, session_type=session_type
    )


def _attach_calibration(client: TestClient, session_id: str, calibration_id: str) -> None:
    response = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": calibration_id},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 200


def _invalidate_calibration(client: TestClient, calibration_id: str) -> None:
    response = client.post(f"/calibrations/{calibration_id}/invalidate", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200


def _click(
    client: TestClient,
    session_id: str,
    ball_no: int,
    px: dict[str, float],
    *,
    camera_id: str = "C3",
    frame_no: int = 42,
    zone_config: dict[str, Any] | None = None,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "ball_no": ball_no,
        "camera_id": camera_id,
        "frame_no": frame_no,
        "px": px,
    }
    if zone_config is not None:
        payload["zone_config"] = zone_config
    return client.post(f"/sessions/{session_id}/bounce-marks", json=payload, headers=auth(token))


def _marks(client: TestClient, session_id: str, **params: Any) -> list[dict[str, Any]]:
    response = client.get(
        f"/sessions/{session_id}/bounce-marks", params=params, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    marks: list[dict[str, Any]] = response.json()
    return marks


def _shifted_config() -> dict[str, Any]:
    """Valid zone config with the full band stretched to 7.0 m ('inf' stays JSON-safe)."""
    return {
        "length_bands_m": {
            "yorker": [0.0, 2.0],
            "full": [2.0, 7.0],
            "good": [7.0, 9.0],
            "short": [9.0, "inf"],
        },
        "line_channels_m": {
            "leg": ["-inf", -0.1143],
            "middle": [-0.1143, 0.1143],
            "off": [0.1143, 0.40],
            "outside_off": [0.40, "inf"],
        },
    }


def test_click_stores_pixel_pitch_and_zones(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    response = _click(client, session_id, 1, _px_for(6.0, 0.2), token=COACH_TOKEN)
    assert response.status_code == 201
    body = response.json()
    mark = body["mark"]
    assert mark["ball_no"] == 1
    assert mark["camera_id"] == "C3"
    assert mark["frame_no"] == 42
    assert mark["px_x"] == pytest.approx(600.0)
    assert mark["px_y"] == pytest.approx(172.5)
    assert mark["pitch_x"] == pytest.approx(6.0)
    assert mark["pitch_y"] == pytest.approx(0.2)
    assert mark["line"] == "off"
    assert mark["length"] == "good"
    assert mark["calibration_id"] is not None
    assert mark["flagged_for_review"] is False
    assert body["agreement"] is None  # single-camera ball: nothing to compare


def test_latest_valid_extrinsic_calibration_is_used(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    _seed_calibration(engine, "C3", scale=0.02, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    current_id = _seed_calibration(
        engine, "C3", scale=SCALE, created_at=datetime(2026, 2, 1, tzinfo=UTC)
    )
    _seed_calibration(
        engine, "C3", scale=0.05, valid=False, created_at=datetime(2026, 3, 1, tzinfo=UTC)
    )
    _seed_calibration(
        engine,
        "C3",
        kind=CalibrationKind.INTRINSIC,
        params=_intrinsic_params(),  # all-zero dist coeffs: undistortion is identity
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    session_id = _create_session(client)

    mark = _click(client, session_id, 1, _px_for(6.0, 0.0)).json()["mark"]
    # scale 0.02 would have produced pitch_x = 12.0; invalid/intrinsic rows never win.
    assert mark["pitch_x"] == pytest.approx(6.0)
    assert mark["calibration_id"] == current_id


def test_session_attached_calibration_survives_camera_move(
    client: TestClient, engine: Engine
) -> None:
    """US-C3 era consistency: a session linked to its calibration keeps mapping
    through it even after the camera moves and a new era is calibrated."""
    _register_camera(client, "C3")
    attached_id = _seed_calibration(engine, "C3")
    session_id = _create_session(client)
    _attach_calibration(client, session_id, attached_id)
    _register_camera(client, "C3")  # camera moved: era 2 becomes current
    _seed_calibration(engine, "C3", era_no=2, scale=0.02)

    mark = _click(client, session_id, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert mark["pitch_x"] == pytest.approx(6.0)  # the era-2 scale would give 12.0
    assert mark["calibration_id"] == attached_id

    unattached = _create_session(client)
    fallback = _click(client, unattached, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert fallback["pitch_x"] == pytest.approx(12.0)  # current era wins without a link


def test_attached_calibration_for_other_camera_falls_back(
    client: TestClient, engine: Engine
) -> None:
    _register_camera(client, "C3")
    _register_camera(client, "C4")
    c4_id = _seed_calibration(engine, "C4", scale=0.02)
    c3_id = _seed_calibration(engine, "C3")
    session_id = _create_session(client)
    _attach_calibration(client, session_id, c4_id)

    mark = _click(client, session_id, 1, _px_for(6.0, 0.2), camera_id="C3").json()["mark"]
    assert mark["pitch_x"] == pytest.approx(6.0)  # C3 clicks never use the C4 link
    assert mark["calibration_id"] == c3_id


def test_invalidated_attached_calibration_falls_back(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    attached_id = _seed_calibration(
        engine, "C3", scale=0.02, created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    current_id = _seed_calibration(engine, "C3", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    session_id = _create_session(client)
    _attach_calibration(client, session_id, attached_id)
    _invalidate_calibration(client, attached_id)  # US-C4: camera moved

    mark = _click(client, session_id, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert mark["pitch_x"] == pytest.approx(6.0)
    assert mark["calibration_id"] == current_id


def test_dangling_attached_calibration_falls_back(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    with DbSession(engine) as db:
        db_session = db.get(SessionModel, uuid.UUID(session_id))
        assert db_session is not None
        db_session.calibration_id = uuid.uuid4()  # record removed out-of-band
        db.commit()

    mark = _click(client, session_id, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert mark["pitch_x"] == pytest.approx(6.0)


def test_new_camera_era_requires_fresh_calibration(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)  # C3 era 1, calibrated
    _register_camera(client, "C3")  # camera moved: opens era 2, deactivates era 1

    stale = _click(client, session_id, 1, _px_for(6.0, 0.0))
    assert stale.status_code == 422
    assert "calibrate first" in stale.json()["detail"]

    _seed_calibration(engine, "C3", era_no=2)
    assert _click(client, session_id, 1, _px_for(6.0, 0.0)).status_code == 201


def test_missing_calibration_and_unregistered_camera_422(
    client: TestClient, engine: Engine
) -> None:
    _register_camera(client, "C3")  # registered but never calibrated
    session_id = _create_session(client)

    uncalibrated = _click(client, session_id, 1, _px_for(6.0, 0.0))
    assert uncalibrated.status_code == 422
    assert "no valid extrinsic calibration for camera C3" in uncalibrated.json()["detail"]

    unregistered = _click(client, session_id, 1, _px_for(6.0, 0.0), camera_id="C8")
    assert unregistered.status_code == 422
    assert "no active registration" in unregistered.json()["detail"]


def test_intrinsic_calibration_undistorts_click_before_homography(
    client: TestClient, engine: Engine
) -> None:
    """US-C1: the stored pitch xy reflects the undistorted pixel, not the raw click."""
    session_id = _setup_session(client, engine)
    _seed_calibration(engine, "C3", kind=CalibrationKind.INTRINSIC, params=_intrinsic_params(k1=K1))
    ideal = _px_for(6.0, 0.2)
    clicked = _distorted(ideal, k1=K1)  # what the distorting lens actually shows
    assert abs(clicked["x"] * SCALE - 6.0) > 0.1  # raw click would map ~0.19 m away

    saved = _click(client, session_id, 1, clicked)
    assert saved.status_code == 201
    mark = saved.json()["mark"]
    assert mark["px_x"] == pytest.approx(clicked["x"])  # raw distorted click persisted
    assert mark["px_y"] == pytest.approx(clicked["y"])
    assert mark["pitch_x"] == pytest.approx(6.0, abs=5e-3)  # mapped via undistorted px
    assert mark["pitch_y"] == pytest.approx(0.2, abs=5e-3)


def test_malformed_intrinsic_calibration_is_422(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    _seed_calibration(engine, "C3", kind=CalibrationKind.INTRINSIC, params={"version": 1})

    response = _click(client, session_id, 1, _px_for(6.0, 0.2))
    assert response.status_code == 422
    assert "stored intrinsic calibration for camera C3 is unusable" in response.json()["detail"]


def test_malformed_stored_calibration_422(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    _seed_calibration(engine, "C3", params={"version": 99})
    session_id = _create_session(client)

    response = _click(client, session_id, 1, _px_for(6.0, 0.0))
    assert response.status_code == 422
    assert "unusable" in response.json()["detail"]


def test_singular_stored_matrix_is_422_not_500(client: TestClient, engine: Engine) -> None:
    """A corrupt-but-shape-valid (singular) matrix must reject, never crash."""
    _register_camera(client, "C3")
    params = _params()
    params["matrix"] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]]
    _seed_calibration(engine, "C3", params=params)
    session_id = _create_session(client)

    response = _click(client, session_id, 1, _px_for(6.0, 0.0))
    assert response.status_code == 422
    assert "unusable" in response.json()["detail"]


def test_click_on_mapping_horizon_is_422_not_500(client: TestClient, engine: Engine) -> None:
    """An invertible matrix can still send a pixel to the horizon (w ~ 0)."""
    _register_camera(client, "C3")
    params = _params()
    params["matrix"] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.001, 0.0, -1.0]]
    _seed_calibration(engine, "C3", params=params)
    session_id = _create_session(client)

    response = _click(client, session_id, 1, {"x": 1000.0, "y": 500.0})  # w = 0 exactly
    assert response.status_code == 422
    assert "unusable" in response.json()["detail"]
    assert _marks(client, session_id) == []


def test_click_mapping_off_pitch_is_rejected(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    response = _click(client, session_id, 1, _px_for(-0.5, 0.0))  # behind the stumps line
    assert response.status_code == 422
    assert "cannot be classified" in response.json()["detail"]


def test_stump_line_click_clamps_to_yorker(client: TestClient, engine: Engine) -> None:
    """A click mapping a hair behind the stumps line (within the zones clamp
    epsilon) is a legitimate yorker, not a rejected mis-click."""
    session_id = _setup_session(client, engine)
    saved = _click(client, session_id, 1, _px_for(-0.01, 0.0))
    assert saved.status_code == 201
    mark = saved.json()["mark"]
    assert mark["length"] == "yorker"
    assert mark["pitch_x"] == pytest.approx(-0.01)  # raw mapping stored; class clamped


def test_click_beyond_clamp_epsilon_is_rejected_and_not_persisted(
    client: TestClient, engine: Engine
) -> None:
    """Beyond the clamp epsilon the click is a mis-click: rejected, nothing stored."""
    session_id = _setup_session(client, engine)
    response = _click(client, session_id, 1, _px_for(-0.10, 0.0))
    assert response.status_code == 422
    assert "cannot be classified" in response.json()["detail"]
    assert _marks(client, session_id) == []


def test_ball_exactly_on_band_edge_belongs_to_farther_band(
    client: TestClient, engine: Engine
) -> None:
    """Bands are half-open [lo, hi): a bounce at exactly 5.0 m is GOOD, not FULL."""
    session_id = _setup_session(client, engine)
    mark = _click(client, session_id, 1, _px_for(5.0, 0.0)).json()["mark"]
    assert mark["pitch_x"] == pytest.approx(5.0)
    assert mark["length"] == "good"


def test_lh_guest_batter_mirrors_line_channels(client: TestClient, engine: Engine) -> None:
    rh_session = _setup_session(client, engine)
    lh_guest_session = _create_session(client, handedness="left", is_guest=True)

    rh_mark = _click(client, rh_session, 1, _px_for(6.0, 0.2)).json()["mark"]
    lh_mark = _click(client, lh_guest_session, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert rh_mark["line"] == "off"
    assert lh_mark["line"] == "leg"  # same +y is the leg side for a LH batter
    assert lh_mark["pitch_y"] == pytest.approx(0.2)  # raw frame is never mirrored


def test_lh_bowling_session_marks_use_canonical_frame_not_mirrored(
    client: TestClient, engine: Engine
) -> None:
    """US-I4 cross-surface: a LEFT-handed kid's BOWLING mark must classify in
    the SAME canonical right-hand frame the accuracy scorecard scores in, so
    the pitch map (stored mark.line) can never disagree with score_delivery.

    A batting session for the same kid keeps mirroring (handedness frames the
    zones); only the bowling session is pinned to the canonical frame.
    """
    bowling = _setup_session(client, engine, handedness="left", session_type="bowling")
    batting = _create_session(client, handedness="left", session_type="batting")

    bowling_mark = _click(client, bowling, 1, _px_for(6.0, 0.2)).json()["mark"]
    batting_mark = _click(client, batting, 1, _px_for(6.0, 0.2)).json()["mark"]

    # Bowling: NOT mirrored — +y is the off channel in the canonical frame.
    assert bowling_mark["line"] == "off"
    assert bowling_mark["length"] == "good"
    # Batting for the same LH kid still mirrors +y to the leg channel (US-C5).
    assert batting_mark["line"] == "leg"

    # The scorecard scores the SAME raw coordinates in the canonical frame:
    # the stored line must be exactly the channel score_delivery calls a hit.
    target = TargetZone(key="off/good", line=Line.OFF, length=Length.GOOD)
    score = score_delivery(
        ZoneConfig(),
        ball_no=1,
        target=target,
        bounce=BouncePoint(
            pitch_x=bowling_mark["pitch_x"], pitch_y=bowling_mark["pitch_y"], confidence=1.0
        ),
        handedness=Handedness.RIGHT,
    )
    assert score.hit is True
    assert bowling_mark["line"] == target.line.value  # pitch map agrees with scorecard


def test_lh_bowling_reclassify_uses_canonical_frame(client: TestClient, engine: Engine) -> None:
    """Reclassify (the higher-precedence re-derive path) shares the gate: a LH
    bowling session's marks re-derive in the canonical frame, never mirrored."""
    bowling = _setup_session(client, engine, handedness="left", session_type="bowling")
    _click(client, bowling, 1, _px_for(6.0, 0.2))

    reclassified = client.post(
        f"/sessions/{bowling}/bounce-marks/reclassify", json={}, headers=auth(COACH_TOKEN)
    ).json()
    assert reclassified["marks"][0]["line"] == "off"  # canonical frame, not "leg"


def test_reclick_upserts_single_row_and_rederives(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    first = _click(client, session_id, 1, _px_for(6.0, 0.0), frame_no=10)
    assert first.status_code == 201

    second = _click(client, session_id, 1, _px_for(3.0, 0.5), frame_no=11)
    assert second.status_code == 200  # replaced, not duplicated
    mark = second.json()["mark"]
    assert mark["id"] == first.json()["mark"]["id"]
    assert mark["frame_no"] == 11
    assert mark["px_x"] == pytest.approx(300.0)
    assert mark["pitch_x"] == pytest.approx(3.0)
    assert mark["line"] == "outside_off"
    assert mark["length"] == "full"

    assert len(_marks(client, session_id)) == 1


def test_cross_camera_agreement_flags_then_clears(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine, cameras=("C3", "C4"))
    assert _click(client, session_id, 7, _px_for(6.0, 0.0), camera_id="C3").status_code == 201

    disagree = _click(client, session_id, 7, _px_for(6.2, 0.0), camera_id="C4").json()
    assert disagree["agreement"]["other_camera"] == "C3"
    assert disagree["agreement"]["distance_m"] == pytest.approx(0.2)
    assert disagree["mark"]["flagged_for_review"] is True
    assert [m["flagged_for_review"] for m in _marks(client, session_id, ball_no=7)] == [True, True]

    agree = _click(client, session_id, 7, _px_for(6.05, 0.0), camera_id="C4").json()
    assert agree["agreement"]["other_camera"] == "C3"
    assert agree["agreement"]["distance_m"] == pytest.approx(0.05)
    assert agree["mark"]["flagged_for_review"] is False
    assert [m["flagged_for_review"] for m in _marks(client, session_id, ball_no=7)] == [
        False,
        False,
    ]


def test_list_filters_by_ball_no_and_flagged(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine, cameras=("C3", "C4"))
    _click(client, session_id, 1, _px_for(6.0, 0.0), camera_id="C3")
    _click(client, session_id, 1, _px_for(6.5, 0.0), camera_id="C4")  # disagree: flagged
    _click(client, session_id, 2, _px_for(3.0, 0.0), camera_id="C3")

    assert len(_marks(client, session_id)) == 3
    assert [m["camera_id"] for m in _marks(client, session_id, ball_no=1)] == ["C3", "C4"]
    assert [m["ball_no"] for m in _marks(client, session_id, flagged=True)] == [1, 1]
    assert [m["ball_no"] for m in _marks(client, session_id, flagged=False)] == [2]


def test_zone_config_override_applies_at_click_time(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    mark = _click(client, session_id, 1, _px_for(6.0, 0.0), zone_config=_shifted_config()).json()[
        "mark"
    ]
    assert mark["length"] == "full"  # 6.0 m sits in the stretched full band


def test_invalid_zone_config_is_422(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    bad = {"length_bands_m": {}, "line_channels_m": {}}

    click = _click(client, session_id, 1, _px_for(6.0, 0.0), zone_config=bad)
    assert click.status_code == 422
    assert "invalid zone config" in click.json()["detail"]

    reclassify = client.post(
        f"/sessions/{session_id}/bounce-marks/reclassify",
        json={"zone_config": bad},
        headers=auth(PARENT_TOKEN),
    )
    assert reclassify.status_code == 422
    assert "invalid zone config" in reclassify.json()["detail"]


def test_reclassify_rederives_from_stored_xy_without_reclicking(
    client: TestClient, engine: Engine
) -> None:
    session_id = _setup_session(client, engine)
    original = _click(client, session_id, 1, _px_for(6.0, 0.2)).json()["mark"]
    assert original["length"] == "good"

    response = client.post(
        f"/sessions/{session_id}/bounce-marks/reclassify",
        json={"zone_config": _shifted_config()},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    reclassified = body["marks"][0]
    assert reclassified["length"] == "full"  # re-derived from stored xy
    assert reclassified["line"] == "off"
    assert reclassified["px_x"] == original["px_x"]  # raw click untouched
    assert reclassified["pitch_x"] == original["pitch_x"]
    assert _marks(client, session_id)[0]["length"] == "full"  # persisted

    # Omitting zone_config reclassifies with the defaults, reverting the class.
    reverted = client.post(
        f"/sessions/{session_id}/bounce-marks/reclassify", json={}, headers=auth(PARENT_TOKEN)
    ).json()
    assert reverted["marks"][0]["length"] == "good"


def test_reclassify_empty_session_counts_zero(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    body = client.post(
        f"/sessions/{session_id}/bounce-marks/reclassify", json={}, headers=auth(PARENT_TOKEN)
    ).json()
    assert body == {"count": 0, "marks": []}


def test_map_click_without_session_uses_current_era(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    calibration_id = _seed_calibration(engine, "C3")
    with DbSession(engine) as db:
        point = map_click(db, "C3", (600.0, 152.5))
    assert point.pitch_x == pytest.approx(6.0)
    assert point.pitch_y == pytest.approx(0.0)
    assert str(point.calibration_id) == calibration_id
    assert point.intrinsics_applied is False  # no intrinsic record for the era


def test_map_click_reports_intrinsics_applied(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    _seed_calibration(engine, "C3")
    _seed_calibration(engine, "C3", kind=CalibrationKind.INTRINSIC, params=_intrinsic_params())
    with DbSession(engine) as db:
        point = map_click(db, "C3", (600.0, 152.5))
    assert point.intrinsics_applied is True
    assert point.pitch_x == pytest.approx(6.0)  # all-zero dist coeffs = identity


def test_reevaluate_agreement_without_marks_is_none(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    with DbSession(engine) as db:
        assert reevaluate_agreement(db, uuid.UUID(session_id), 1) is None


def test_recompute_session_marks_remaps_reclassifies_and_reflags(
    client: TestClient, engine: Engine
) -> None:
    session_id = _setup_session(client, engine, cameras=("C3", "C4"))
    _click(client, session_id, 1, _px_for(6.0, 0.0), camera_id="C3")
    _click(client, session_id, 1, _px_for(6.1, 0.0), camera_id="C4")  # 0.10 m: agrees
    _click(client, session_id, 2, _px_for(3.0, 0.2), camera_id="C3")
    assert [m["flagged_for_review"] for m in _marks(client, session_id)] == [False, False, False]

    later = datetime(2027, 1, 1, tzinfo=UTC)
    new_c3 = _seed_calibration(engine, "C3", scale=0.02, created_at=later)
    new_c4 = _seed_calibration(engine, "C4", scale=0.02, created_at=later)

    with DbSession(engine) as db:
        session = db.get(SessionModel, uuid.UUID(session_id))
        assert session is not None
        assert recompute_session_marks(db, session) == 3
        db.commit()

    ball1 = _marks(client, session_id, ball_no=1)
    assert [m["camera_id"] for m in ball1] == ["C3", "C4"]
    assert ball1[0]["pitch_x"] == pytest.approx(12.0)
    assert ball1[1]["pitch_x"] == pytest.approx(12.2)
    assert [m["calibration_id"] for m in ball1] == [new_c3, new_c4]
    assert [m["length"] for m in ball1] == ["short", "short"]
    # The doubled scale doubled the disagreement to 0.20 m: ball 1 is now flagged.
    assert [m["flagged_for_review"] for m in ball1] == [True, True]

    (ball2,) = _marks(client, session_id, ball_no=2)
    assert ball2["pitch_x"] == pytest.approx(6.0)
    assert ball2["length"] == "good"  # was "full" at 3.0 m; classes re-derived
    assert ball2["px_x"] == pytest.approx(300.0)  # raw clicks never change
    assert ball2["calibration_id"] == new_c3
    assert ball2["flagged_for_review"] is False  # single-camera ball stays clear


def test_recompute_empty_session_returns_zero(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    with DbSession(engine) as db:
        session = db.get(SessionModel, uuid.UUID(session_id))
        assert session is not None
        assert recompute_session_marks(db, session) == 0


def test_recompute_unavailable_cameras_listed_sorted(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine, cameras=("C3", "C4"))
    _click(client, session_id, 1, _px_for(6.0, 0.0), camera_id="C3")
    _click(client, session_id, 1, _px_for(6.1, 0.0), camera_id="C4")
    _register_camera(client, "C4")  # both cameras moved: new eras, never calibrated
    _register_camera(client, "C3")

    with DbSession(engine) as db:
        session = db.get(SessionModel, uuid.UUID(session_id))
        assert session is not None
        with pytest.raises(MappingUnavailableError) as excinfo:
            recompute_session_marks(db, session)
    assert excinfo.value.cameras == ["C3", "C4"]
    assert "calibrate first" in str(excinfo.value)


def test_recompute_partially_unavailable_updates_nothing(
    client: TestClient, engine: Engine
) -> None:
    session_id = _setup_session(client, engine, cameras=("C3", "C4"))
    _click(client, session_id, 1, _px_for(6.0, 0.0), camera_id="C3")
    _click(client, session_id, 1, _px_for(6.1, 0.0), camera_id="C4")
    _register_camera(client, "C4")  # only C4 moved

    with DbSession(engine) as db:
        session = db.get(SessionModel, uuid.UUID(session_id))
        assert session is not None
        with pytest.raises(MappingUnavailableError) as excinfo:
            recompute_session_marks(db, session)
    assert excinfo.value.cameras == ["C4"]
    # All-or-nothing: the still-calibrated C3 mark was not remapped either.
    assert [m["pitch_x"] for m in _marks(client, session_id)] == [
        pytest.approx(6.0),
        pytest.approx(6.1),
    ]


def test_player_role_cannot_write(client: TestClient, engine: Engine) -> None:
    session_id = _setup_session(client, engine)
    assert _click(client, session_id, 1, _px_for(6.0, 0.0), token=PLAYER_TOKEN).status_code == 403
    reclassify = client.post(
        f"/sessions/{session_id}/bounce-marks/reclassify", json={}, headers=auth(PLAYER_TOKEN)
    )
    assert reclassify.status_code == 403


def test_player_role_guest_session_reads_are_404(client: TestClient, engine: Engine) -> None:
    guest_session = _setup_session(client, engine, is_guest=True)
    family_session = _create_session(client)
    _click(client, guest_session, 1, _px_for(6.0, 0.0))

    hidden = client.get(f"/sessions/{guest_session}/bounce-marks", headers=auth(PLAYER_TOKEN))
    assert hidden.status_code == 404  # US-L3: existence hidden from players

    visible = client.get(f"/sessions/{family_session}/bounce-marks", headers=auth(PLAYER_TOKEN))
    assert visible.status_code == 200
    assert visible.json() == []

    for token in (PARENT_TOKEN, COACH_TOKEN):
        allowed = client.get(f"/sessions/{guest_session}/bounce-marks", headers=auth(token))
        assert allowed.status_code == 200
        assert len(allowed.json()) == 1


def test_unknown_session_is_404_everywhere(client: TestClient, engine: Engine) -> None:
    _register_camera(client, "C3")
    _seed_calibration(engine, "C3")
    assert _click(client, UNKNOWN_SESSION, 1, _px_for(6.0, 0.0)).status_code == 404
    assert (
        client.get(f"/sessions/{UNKNOWN_SESSION}/bounce-marks", headers=auth(PARENT_TOKEN))
    ).status_code == 404
    assert (
        client.post(
            f"/sessions/{UNKNOWN_SESSION}/bounce-marks/reclassify",
            json={},
            headers=auth(PARENT_TOKEN),
        )
    ).status_code == 404
