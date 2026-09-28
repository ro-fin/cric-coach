"""US-C3/C4 acceptance: calibration records, session linkage, drift detection.

Landmark clicks are synthesized by projecting the geometry landmark catalog
through the synthetic overhead camera, so the fitted homography must reproduce
them exactly — drift tests then nudge those pixels by known amounts.
"""

import json
import uuid
from pathlib import Path

import httpx
import numpy as np
import pytest
from cricai_api.services import bounce_mapping
from cricai_data.enums import CalibrationKind
from cricai_data.models import AuditLog, Calibration, Session
from cricai_vision import extrinsics, intrinsics, triangulate
from cricai_vision.geometry import landmark_catalog, make_overhead_camera
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

MISSING_ID = "00000000-0000-0000-0000-000000000000"

CAMERA = make_overhead_camera()
CATALOG = landmark_catalog()

#: >= 6 well-spread landmarks; the first four are never 3-collinear so the
#: holdout=2 fit stays well-posed even at the 6-landmark minimum.
SIX_NAMES = [
    "striker_off_stump_base",
    "striker_popping_crease_off",
    "striker_popping_crease_leg",
    "bowler_middle_stump_base",
    "bowler_popping_crease_off",
    "pitch_edge_leg_striker",
]
EIGHT_NAMES = [*SIX_NAMES, "bowler_popping_crease_leg", "pitch_edge_off_striker"]

INTRINSIC_PARAMS: dict[str, object] = {
    "version": 1,
    "camera_matrix": [[1400.0, 0.0, 960.0], [0.0, 1400.0, 540.0], [0.0, 0.0, 1.0]],
    "dist_coeffs": [0.01, -0.02, 0.0, 0.0, 0.0],
}


def _full_intrinsic_params(
    dist_coeffs: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0),
) -> dict[str, object]:
    """A complete ``intrinsics.to_params`` payload, usable for undistortion."""
    return intrinsics.to_params(
        intrinsics.IntrinsicsResult(
            camera_matrix=((1400.0, 0.0, 960.0), (0.0, 1400.0, 540.0), (0.0, 0.0, 1.0)),
            dist_coeffs=dist_coeffs,
            reprojection_error_px=0.2,
            board_spec=intrinsics.BoardSpec(),
            n_views=8,
            captured_on="2026-07-01",
        )
    )


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _clicks(names: list[str], dx: float = 0.0, dy: float = 0.0) -> list[dict[str, object]]:
    """Project catalog landmarks to pixels, optionally shifted by (dx, dy)."""
    pixels = CAMERA.project_pitch_xy(np.array([CATALOG[name].xy for name in names]))
    return [
        {"name": name, "px": [float(x) + dx, float(y) + dy]}
        for name, (x, y) in zip(names, pixels, strict=True)
    ]


def _register_camera(client: TestClient, camera_id: str) -> None:
    response = client.post(
        "/cameras",
        json={
            "camera_id": camera_id,
            "position_label": f"{camera_id} test rig",
            "xyz_offset_m": {"x": 10.0, "y": 7.6, "z": 4.5},
            "height_m": 4.5,
            "fps": 120,
            "resolution": "1920x1080",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _create_session(client: TestClient, *, is_guest: bool = False) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "is_guest": is_guest},
        headers=auth(PARENT_TOKEN),
    ).json()
    response = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    session_id: str = response.json()["id"]
    return session_id


def _create_extrinsic(
    client: TestClient, camera_id: str, names: list[str] | None = None
) -> dict[str, object]:
    response = client.post(
        "/calibrations",
        json={
            "camera_id": camera_id,
            "kind": "extrinsic",
            "landmarks": _clicks(names or EIGHT_NAMES),
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


def _suspect_flag(app: FastAPI, session_id: str) -> bool:
    db = app.state.session_factory()
    try:
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        return bool(session.calibration_suspect)
    finally:
        db.close()


def _audit_rows(app: FastAPI, action: str) -> list[AuditLog]:
    db = app.state.session_factory()
    try:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)).all())
    finally:
        db.close()


# -- POST /calibrations -------------------------------------------------------


def test_extrinsic_calibration_from_landmark_clicks(client: TestClient) -> None:
    _register_camera(client, "C3")
    body = _create_extrinsic(client, "C3")
    assert body["camera_id"] == "C3"
    assert body["era_no"] == 1
    assert body["kind"] == "extrinsic"
    assert body["valid"] is True
    params = body["params"]
    assert isinstance(params, dict)
    assert params["version"] == 1
    assert len(params["matrix"]) == 3
    assert params["n_landmarks"] == len(EIGHT_NAMES)
    # Exact synthetic clicks: held-out mapping error is numerically zero-ish,
    # comfortably inside the 3 cm crease-line target (US-C2).
    rms = body["rms"]
    assert isinstance(rms, float)
    assert rms < 0.03
    # 8 clicks afford the 2-landmark holdout, so the RMS is honest (held-out).
    assert body["rms_in_sample"] is False
    # No intrinsic record exists for this camera/era: nothing to reference.
    assert body["intrinsics_id"] is None
    assert "intrinsics_id" not in params


def test_extrinsic_works_at_six_landmark_minimum(client: TestClient) -> None:
    _register_camera(client, "C4")
    body = _create_extrinsic(client, "C4", SIX_NAMES)
    assert body["params"]["n_landmarks"] == 6  # type: ignore[index]
    # At the US-C2 minimum there is no holdout: the RMS is labeled in-sample.
    assert body["rms_in_sample"] is True


def test_extrinsic_references_intrinsics_used_for_undistortion(client: TestClient) -> None:
    """US-C1 AC: the session references the intrinsics version used, via the
    Session.calibration -> Calibration.params["intrinsics_id"] chain."""
    _register_camera(client, "C3")
    intrinsic_id = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "intrinsic", "params": _full_intrinsic_params()},
        headers=auth(PARENT_TOKEN),
    ).json()["id"]

    body = _create_extrinsic(client, "C3")
    assert body["intrinsics_id"] == intrinsic_id
    assert body["params"]["intrinsics_id"] == intrinsic_id  # type: ignore[index]
    # Zero distortion coefficients: undistortion is the identity, so the
    # exact synthetic clicks still fit within the 3 cm target (US-C2).
    assert body["rms"] < 0.03  # type: ignore[operator]
    assert body["rms_in_sample"] is False

    session_id = _create_session(client)
    attached = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": body["id"]},
        headers=auth(PARENT_TOKEN),
    )
    assert attached.status_code == 200
    linked = client.get(f"/sessions/{session_id}/calibration", headers=auth(PARENT_TOKEN)).json()
    assert linked["params"]["intrinsics_id"] == intrinsic_id


def test_undistortion_changes_the_fit_when_distortion_is_nonzero(client: TestClient) -> None:
    """The clicks must be undistorted BEFORE fit_homography, not just logged."""
    _register_camera(client, "C3")
    _register_camera(client, "C4")
    baseline = _create_extrinsic(client, "C4")  # no intrinsic record: raw clicks
    client.post(
        "/calibrations",
        json={
            "camera_id": "C3",
            "kind": "intrinsic",
            "params": _full_intrinsic_params(dist_coeffs=(0.05, -0.01, 0.0, 0.0, 0.0)),
        },
        headers=auth(PARENT_TOKEN),
    )
    undistorted = _create_extrinsic(client, "C3")
    assert undistorted["params"]["matrix"] != baseline["params"]["matrix"]  # type: ignore[index]


def test_invalidated_intrinsic_is_not_referenced_by_extrinsic_fit(client: TestClient) -> None:
    _register_camera(client, "C3")
    intrinsic_id = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "intrinsic", "params": _full_intrinsic_params()},
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    client.post(f"/calibrations/{intrinsic_id}/invalidate", headers=auth(PARENT_TOKEN))

    body = _create_extrinsic(client, "C3")
    assert body["intrinsics_id"] is None
    assert "intrinsics_id" not in body["params"]  # type: ignore[operator]


def test_unusable_stored_intrinsic_fails_extrinsic_fit_as_422(client: TestClient) -> None:
    """Minimal stored intrinsic params pass the storage-time structural check
    but are not a full undistortion model — the fit must 422, not 500."""
    _register_camera(client, "C3")
    client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "intrinsic", "params": INTRINSIC_PARAMS},
        headers=auth(PARENT_TOKEN),
    )
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "unusable" in response.json()["detail"]


def test_extrinsic_rejects_fewer_than_six_landmarks(client: TestClient) -> None:
    _register_camera(client, "C3")
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": _clicks(SIX_NAMES[:5])},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert ">= 6 landmark clicks" in response.json()["detail"]

    missing = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic"},
        headers=auth(PARENT_TOKEN),
    )
    assert missing.status_code == 422
    assert ">= 6 landmark clicks" in missing.json()["detail"]


def test_extrinsic_rejects_unknown_landmark_names(client: TestClient) -> None:
    _register_camera(client, "C3")
    landmarks = _clicks(SIX_NAMES)
    landmarks[0]["name"] = "third_slip_heel"
    landmarks[1]["name"] = "sightscreen_corner"
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": landmarks},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "unknown landmark names"
    assert detail["unknown_landmarks"] == ["sightscreen_corner", "third_slip_heel"]


def test_extrinsic_rejects_duplicate_landmark_names(client: TestClient) -> None:
    _register_camera(client, "C3")
    landmarks = _clicks(SIX_NAMES)
    landmarks[1]["name"] = landmarks[0]["name"]
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": landmarks},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "unique" in response.json()["detail"]


def test_extrinsic_fit_failure_surfaces_as_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register_camera(client, "C3")

    def boom(*args: object, **kwargs: object) -> object:
        raise extrinsics.ExtrinsicsError("degenerate landmark configuration")

    monkeypatch.setattr("cricai_vision.extrinsics.fit_homography", boom)
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "degenerate" in response.json()["detail"]


def test_calibration_requires_registered_active_camera(client: TestClient) -> None:
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "not registered as active" in response.json()["detail"]


def test_calibration_era_follows_active_camera_era(client: TestClient) -> None:
    _register_camera(client, "C3")
    assert _create_extrinsic(client, "C3")["era_no"] == 1
    _register_camera(client, "C3")  # camera moved: new placement era
    assert _create_extrinsic(client, "C3")["era_no"] == 2


def test_camera_id_pattern_enforced(client: TestClient) -> None:
    response = client.post(
        "/calibrations",
        json={"camera_id": "C9", "kind": "extrinsic", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422


def test_intrinsic_calibration_stored_with_minimal_shape_check(client: TestClient) -> None:
    _register_camera(client, "C1")
    response = client.post(
        "/calibrations",
        json={"camera_id": "C1", "kind": "intrinsic", "params": INTRINSIC_PARAMS},
        headers=auth(COACH_TOKEN),  # coach can write calibrations too
    )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "intrinsic"
    assert body["rms"] is None
    assert body["params"] == INTRINSIC_PARAMS
    # Intrinsic records carry no fitted RMS and reference no other intrinsic.
    assert body["intrinsics_id"] is None
    assert body["rms_in_sample"] is False


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        (None, "requires params"),
        ({"camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}, "version 1"),
        ({"version": 2, "camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}, "version 1"),
        ({"version": 1}, "3x3 numeric camera_matrix"),
        ({"version": 1, "camera_matrix": [[1, 0, 0], [0, 1, 0]]}, "3x3 numeric camera_matrix"),
        (
            {"version": 1, "camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0]]},
            "3x3 numeric camera_matrix",
        ),
        (
            {"version": 1, "camera_matrix": [[1, 0, 0], [0, 1, 0], "row"]},
            "3x3 numeric camera_matrix",
        ),
        (
            {"version": 1, "camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, "x"]]},
            "3x3 numeric camera_matrix",
        ),
        (
            {"version": 1, "camera_matrix": [[1, 0, 0], [0, 1, 0], [0, 0, True]]},
            "3x3 numeric camera_matrix",
        ),
        # NaN/Infinity serialize to null in JSON and poison later reads: the
        # stdlib json codec lets them through the request body, so the shape
        # check must reject them explicitly.
        (
            {"version": 1, "camera_matrix": [[float("nan"), 0, 960], [0, 1400, 540], [0, 0, 1]]},
            "3x3 numeric camera_matrix",
        ),
        (
            {"version": 1, "camera_matrix": [[1400, 0, 960], [0, float("inf"), 540], [0, 0, 1]]},
            "3x3 numeric camera_matrix",
        ),
        (
            {
                "version": 1,
                "camera_matrix": [[1400, 0, 960], [0, 1400, 540], [0, 0, float("-inf")]],
            },
            "3x3 numeric camera_matrix",
        ),
    ],
)
def test_intrinsic_params_shape_rejections(
    client: TestClient, params: dict[str, object] | None, fragment: str
) -> None:
    _register_camera(client, "C1")
    body: dict[str, object] = {"camera_id": "C1", "kind": "intrinsic"}
    if params is not None:
        body["params"] = params
    # stdlib dumps emits NaN/Infinity tokens (allow_nan default) that the
    # server-side stdlib parser accepts — exactly the hole being tested;
    # httpx's own json= path refuses to serialize them.
    response = client.post(
        "/calibrations",
        content=json.dumps(body),
        headers={**auth(PARENT_TOKEN), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


# -- POST /calibrations kind=stereo (US-F6) -----------------------------------

_IDENTITY_3X3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _stereo_params(camera_pair: tuple[str, str] = ("C1", "C4")) -> dict[str, object]:
    """A valid ``triangulate.to_params`` payload (identity rig, 0.5 m baseline)."""
    intrinsics_block = {
        "camera_matrix": [[1400.0, 0.0, 960.0], [0.0, 1400.0, 540.0], [0.0, 0.0, 1.0]],
        "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
    }
    return {
        "version": 1,
        "frame": triangulate.PITCH_FRAME,
        "camera_pair": list(camera_pair),
        "intrinsics_a": intrinsics_block,
        "intrinsics_b": dict(intrinsics_block),
        "rotation": _IDENTITY_3X3,
        "translation": [0.5, 0.0, 0.0],
        "world_rotation_a": _IDENTITY_3X3,
        "world_translation_a": [0.0, 0.0, 0.0],
    }


def test_stereo_calibration_stores_exact_to_params_payload(client: TestClient) -> None:
    """US-F6 row contract: ``params`` is exactly the ``triangulate.to_params``
    payload, stored under the pair's first camera id."""
    _register_camera(client, "C1")
    params = _stereo_params()
    response = client.post(
        "/calibrations",
        json={"camera_id": "C1", "kind": "stereo", "params": params},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "stereo"
    assert body["params"] == params
    assert body["rms"] is None
    assert body["intrinsics_id"] is None
    assert body["rms_in_sample"] is False
    # The stored record must be loadable by the triangulation consumer.
    pair = triangulate.from_params(body["params"])
    assert (pair.camera_a, pair.camera_b) == ("C1", "C4")
    listed = client.get(
        "/calibrations", params={"kind": "stereo"}, headers=auth(COACH_TOKEN)
    ).json()
    assert [record["id"] for record in listed] == [body["id"]]


def test_stereo_rejects_landmark_click_submissions(client: TestClient) -> None:
    """Landmark clicks are the extrinsic flow; routing them into a stereo row
    would persist homography params under ``kind=stereo`` (mislabeled record)."""
    _register_camera(client, "C1")
    response = client.post(
        "/calibrations",
        json={"camera_id": "C1", "kind": "stereo", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "stereo" in response.json()["detail"]
    stereo_rows = client.get(
        "/calibrations", params={"kind": "stereo"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert stereo_rows == []  # nothing mislabeled was persisted


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        (None, "stereo calibration requires params"),
        ({**_stereo_params(), "version": 2}, "unsupported stereo params version"),
        (
            {key: value for key, value in _stereo_params().items() if key != "intrinsics_a"},
            "intrinsics_a",
        ),
        ({**_stereo_params(), "translation": [0.0, 0.0, 0.0]}, "baseline"),
    ],
)
def test_stereo_params_rejections(
    client: TestClient, params: dict[str, object] | None, fragment: str
) -> None:
    """Malformed/degenerate stereo params are 422 with the triangulate reason,
    never stored and never misread as a landmark-click submission."""
    _register_camera(client, "C1")
    body: dict[str, object] = {"camera_id": "C1", "kind": "stereo"}
    if params is not None:
        body["params"] = params
    response = client.post("/calibrations", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


def test_stereo_record_must_be_stored_under_first_pair_camera(client: TestClient) -> None:
    """US-F6 row contract: one row per pair under ``camera_id = camera_pair[0]``
    — storing under the second camera would hide the record from consumers."""
    _register_camera(client, "C4")
    response = client.post(
        "/calibrations",
        json={"camera_id": "C4", "kind": "stereo", "params": _stereo_params()},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "camera_pair" in response.json()["detail"]


def test_player_cannot_write_calibrations(client: TestClient) -> None:
    _register_camera(client, "C3")
    response = client.post(
        "/calibrations",
        json={"camera_id": "C3", "kind": "extrinsic", "landmarks": _clicks(EIGHT_NAMES)},
        headers=auth(PLAYER_TOKEN),
    )
    assert response.status_code == 403


# -- GET /calibrations --------------------------------------------------------


def test_list_calibrations_newest_first_with_filters(client: TestClient) -> None:
    _register_camera(client, "C1")
    _register_camera(client, "C3")
    intrinsic_id = client.post(
        "/calibrations",
        json={"camera_id": "C1", "kind": "intrinsic", "params": INTRINSIC_PARAMS},
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    first_ext = _create_extrinsic(client, "C3")["id"]
    second_ext = _create_extrinsic(client, "C3")["id"]

    everything = client.get("/calibrations", headers=auth(PLAYER_TOKEN))  # players may read
    assert everything.status_code == 200
    assert [c["id"] for c in everything.json()] == [second_ext, first_ext, intrinsic_id]

    by_camera = client.get(
        "/calibrations", params={"camera_id": "C1"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [c["id"] for c in by_camera] == [intrinsic_id]

    by_kind = client.get(
        "/calibrations", params={"kind": "extrinsic"}, headers=auth(COACH_TOKEN)
    ).json()
    assert [c["id"] for c in by_kind] == [second_ext, first_ext]

    client.post(f"/calibrations/{first_ext}/invalidate", headers=auth(PARENT_TOKEN))
    only_valid = client.get(
        "/calibrations", params={"valid": "true"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [c["id"] for c in only_valid] == [second_ext, intrinsic_id]
    only_invalid = client.get(
        "/calibrations", params={"valid": "false"}, headers=auth(PARENT_TOKEN)
    ).json()
    assert [c["id"] for c in only_invalid] == [first_ext]


# -- POST /calibrations/{id}/invalidate ---------------------------------------


def test_invalidate_is_parent_only_and_audited(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C3")
    calibration_id = _create_extrinsic(client, "C3")["id"]

    forbidden = client.post(f"/calibrations/{calibration_id}/invalidate", headers=auth(COACH_TOKEN))
    assert forbidden.status_code == 403

    response = client.post(f"/calibrations/{calibration_id}/invalidate", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json()["valid"] is False

    audits = _audit_rows(app, "calibration_invalidate")
    assert len(audits) == 1
    assert audits[0].entity_id == calibration_id
    assert audits[0].detail == {"camera_id": "C3", "era_no": 1}

    missing = client.post(f"/calibrations/{MISSING_ID}/invalidate", headers=auth(PARENT_TOKEN))
    assert missing.status_code == 404


# -- PUT/GET /sessions/{id}/calibration ---------------------------------------


def test_attach_explicit_calibration_and_read_back(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C3")
    calibration_id = _create_extrinsic(client, "C3")["id"]
    session_id = _create_session(client)

    none_yet = client.get(f"/sessions/{session_id}/calibration", headers=auth(PARENT_TOKEN))
    assert none_yet.status_code == 404
    assert "no linked calibration" in none_yet.json()["detail"]

    attached = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": calibration_id},
        headers=auth(COACH_TOKEN),
    )
    assert attached.status_code == 200
    assert attached.json()["id"] == calibration_id

    fetched = client.get(f"/sessions/{session_id}/calibration", headers=auth(PLAYER_TOKEN))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == calibration_id

    audits = _audit_rows(app, "calibration_attach")
    assert len(audits) == 1
    assert audits[0].entity_id == session_id
    assert audits[0].detail is not None
    assert audits[0].detail["reused_latest"] is False


def test_attach_reuse_latest_valid_extrinsic_for_camera(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C3")
    first = _create_extrinsic(client, "C3")["id"]
    second = _create_extrinsic(client, "C3")["id"]
    session_id = _create_session(client)

    response = client.put(
        f"/sessions/{session_id}/calibration",
        json={"reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 200
    assert response.json()["id"] == second

    audits = _audit_rows(app, "calibration_attach")
    assert audits[0].detail is not None
    assert audits[0].detail["reused_latest"] is True

    # Invalidated records are skipped: reuse now resolves to the older one.
    client.post(f"/calibrations/{second}/invalidate", headers=auth(PARENT_TOKEN))
    again = client.put(
        f"/sessions/{session_id}/calibration",
        json={"reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert again.status_code == 200
    assert again.json()["id"] == first


def test_attach_rejects_unknown_invalid_or_intrinsic_records(client: TestClient) -> None:
    _register_camera(client, "C1")
    _register_camera(client, "C3")
    session_id = _create_session(client)

    unknown = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": MISSING_ID},
        headers=auth(PARENT_TOKEN),
    )
    assert unknown.status_code == 422
    assert "unknown calibration_id" in unknown.json()["detail"]

    intrinsic_id = client.post(
        "/calibrations",
        json={"camera_id": "C1", "kind": "intrinsic", "params": INTRINSIC_PARAMS},
        headers=auth(PARENT_TOKEN),
    ).json()["id"]
    wrong_kind = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": intrinsic_id},
        headers=auth(PARENT_TOKEN),
    )
    assert wrong_kind.status_code == 422
    assert "extrinsic" in wrong_kind.json()["detail"]

    extrinsic_id = _create_extrinsic(client, "C3")["id"]
    client.post(f"/calibrations/{extrinsic_id}/invalidate", headers=auth(PARENT_TOKEN))
    invalidated = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": extrinsic_id},
        headers=auth(PARENT_TOKEN),
    )
    assert invalidated.status_code == 422
    assert "invalidated" in invalidated.json()["detail"]


@pytest.mark.safety
def test_attach_rejects_record_from_stale_camera_era(client: TestClient) -> None:
    """US-C2/C4: moving a camera (new era) invalidates its old calibrations —
    a stale-era record must never attach to a session, even explicitly."""
    _register_camera(client, "C3")  # era 1
    stale_id = _create_extrinsic(client, "C3")["id"]
    _register_camera(client, "C3")  # camera moved: era 2
    session_id = _create_session(client)

    stale = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": stale_id},
        headers=auth(PARENT_TOKEN),
    )
    assert stale.status_code == 422
    assert "camera has moved since this calibration; recalibrate" in stale.json()["detail"]

    # The reuse path never even offers the stale record: era 2 has nothing.
    reuse = client.put(
        f"/sessions/{session_id}/calibration",
        json={"reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert reuse.status_code == 422
    assert "era 2" in reuse.json()["detail"]


def test_attach_requires_exactly_one_selector(client: TestClient) -> None:
    _register_camera(client, "C3")
    calibration_id = _create_extrinsic(client, "C3")["id"]
    session_id = _create_session(client)

    neither = client.put(f"/sessions/{session_id}/calibration", json={}, headers=auth(PARENT_TOKEN))
    assert neither.status_code == 422
    assert "exactly one" in neither.json()["detail"]

    both = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": calibration_id, "reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert both.status_code == 422
    assert "exactly one" in both.json()["detail"]


def test_attach_reuse_errors(client: TestClient) -> None:
    session_id = _create_session(client)
    unregistered = client.put(
        f"/sessions/{session_id}/calibration",
        json={"reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert unregistered.status_code == 422
    assert "not registered as active" in unregistered.json()["detail"]

    _register_camera(client, "C3")
    nothing_to_reuse = client.put(
        f"/sessions/{session_id}/calibration",
        json={"reuse_latest_for_camera": "C3"},
        headers=auth(PARENT_TOKEN),
    )
    assert nothing_to_reuse.status_code == 422
    assert "no valid extrinsic calibration to reuse" in nothing_to_reuse.json()["detail"]


def test_attach_unknown_session_404_and_player_forbidden(client: TestClient) -> None:
    _register_camera(client, "C3")
    calibration_id = _create_extrinsic(client, "C3")["id"]
    missing = client.put(
        f"/sessions/{MISSING_ID}/calibration",
        json={"calibration_id": calibration_id},
        headers=auth(PARENT_TOKEN),
    )
    assert missing.status_code == 404

    session_id = _create_session(client)
    forbidden = client.put(
        f"/sessions/{session_id}/calibration",
        json={"calibration_id": calibration_id},
        headers=auth(PLAYER_TOKEN),
    )
    assert forbidden.status_code == 403


def test_player_cannot_see_guest_session_calibration(client: TestClient) -> None:
    _register_camera(client, "C3")
    calibration_id = _create_extrinsic(client, "C3")["id"]
    guest_session = _create_session(client, is_guest=True)
    client.put(
        f"/sessions/{guest_session}/calibration",
        json={"calibration_id": calibration_id},
        headers=auth(PARENT_TOKEN),
    )
    # US-L3: existence is hidden from players; parent still sees it.
    assert (
        client.get(f"/sessions/{guest_session}/calibration", headers=auth(PLAYER_TOKEN)).status_code
        == 404
    )
    assert (
        client.get(f"/sessions/{guest_session}/calibration", headers=auth(PARENT_TOKEN)).status_code
        == 200
    )


# -- POST /sessions/{id}/drift-check + verify ---------------------------------


def _drift_check(
    client: TestClient,
    session_id: str,
    *,
    dx: float = 0.0,
    dy: float = 0.0,
    names: list[str] | None = None,
    threshold_px: float | None = None,
    token: str = COACH_TOKEN,
) -> httpx.Response:
    body: dict[str, object] = {
        "camera_id": "C3",
        "observed_landmarks": _clicks(names or EIGHT_NAMES, dx=dx, dy=dy),
    }
    if threshold_px is not None:
        body["threshold_px"] = threshold_px
    return client.post(f"/sessions/{session_id}/drift-check", json=body, headers=auth(token))


def test_clean_drift_check_reports_no_drift(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)

    response = _drift_check(client, session_id)
    assert response.status_code == 200
    body = response.json()
    assert body["drifted"] is False
    assert body["threshold_px"] == 5.0
    assert body["median_px"] < 0.1
    assert body["max_px"] < 0.1
    assert set(body["per_landmark"]) == set(EIGHT_NAMES)
    assert _suspect_flag(app, session_id) is False
    assert _audit_rows(app, "calibration_suspect") == []


@pytest.mark.safety
def test_drifted_check_flags_session_suspect_and_flag_is_sticky(
    app: FastAPI, client: TestClient
) -> None:
    """US-C4: drift flags the session; a later clean check must NOT auto-clear it."""
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)

    bumped = _drift_check(client, session_id, dx=6.0, dy=8.0)  # 10 px shift everywhere
    assert bumped.status_code == 200
    body = bumped.json()
    assert body["drifted"] is True
    assert body["median_px"] == pytest.approx(10.0, abs=0.1)
    assert _suspect_flag(app, session_id) is True

    audits = _audit_rows(app, "calibration_suspect")
    assert len(audits) == 1
    assert audits[0].entity_id == session_id
    assert audits[0].detail is not None
    assert audits[0].detail["camera_id"] == "C3"

    clean = _drift_check(client, session_id)
    assert clean.json()["drifted"] is False
    assert _suspect_flag(app, session_id) is True  # sticky until explicit re-verify


def _verify(
    client: TestClient, session_id: str, action: str, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/calibration/verify",
        json={"action": action},
        headers=auth(token),
    )


def _flagged_session(app: FastAPI, client: TestClient) -> str:
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)
    _drift_check(client, session_id, dx=10.0)
    assert _suspect_flag(app, session_id) is True
    return session_id


def test_verify_accept_clears_suspect_flag_parent_only(app: FastAPI, client: TestClient) -> None:
    session_id = _flagged_session(app, client)

    forbidden = _verify(client, session_id, "accept", token=COACH_TOKEN)
    assert forbidden.status_code == 403
    assert _suspect_flag(app, session_id) is True

    response = _verify(client, session_id, "accept")
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "calibration_suspect": False}
    assert _suspect_flag(app, session_id) is False
    audits = _audit_rows(app, "calibration_accept_with_flag")
    assert len(audits) == 1
    assert audits[0].entity_id == session_id

    missing = _verify(client, MISSING_ID, "accept")
    assert missing.status_code == 404


def test_verify_requires_explicit_resolution_action(app: FastAPI, client: TestClient) -> None:
    """US-C4: 'verified' alone is meaningless — the Parent must state whether
    the flagged data was accepted or the camera was recalibrated."""
    session_id = _flagged_session(app, client)

    no_body = client.post(f"/sessions/{session_id}/calibration/verify", headers=auth(PARENT_TOKEN))
    assert no_body.status_code == 422
    unknown_action = _verify(client, session_id, "shrug")
    assert unknown_action.status_code == 422
    assert _suspect_flag(app, session_id) is True


def test_verify_recalibrated_backfills_marks_and_audits(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _flagged_session(app, client)

    def fake_recompute(db: object, session: object) -> int:
        return 3

    monkeypatch.setattr(
        "cricai_api.services.bounce_mapping.recompute_session_marks", fake_recompute
    )
    response = _verify(client, session_id, "recalibrated")
    assert response.status_code == 200
    assert response.json() == {"session_id": session_id, "calibration_suspect": False}
    assert _suspect_flag(app, session_id) is False
    audits = _audit_rows(app, "calibration_backfill")
    assert len(audits) == 1
    assert audits[0].entity_id == session_id
    assert audits[0].detail == {"marks_updated": 3}


def test_verify_recalibrated_without_marks_backfills_zero(app: FastAPI, client: TestClient) -> None:
    """End-to-end with the real service: no stored bounce marks -> zero remaps."""
    session_id = _flagged_session(app, client)
    response = _verify(client, session_id, "recalibrated")
    assert response.status_code == 200
    assert _suspect_flag(app, session_id) is False
    audits = _audit_rows(app, "calibration_backfill")
    assert len(audits) == 1
    assert audits[0].detail == {"marks_updated": 0}


@pytest.mark.safety
def test_verify_recalibrated_conflicts_when_cameras_lack_calibration(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed recalibration that cannot remap the marks must NOT clear the
    flag: suspect data would silently rejoin trend queries otherwise."""
    session_id = _flagged_session(app, client)

    def unavailable(db: object, session: object) -> int:
        raise bounce_mapping.MappingUnavailableError(["C3", "C4"])

    monkeypatch.setattr("cricai_api.services.bounce_mapping.recompute_session_marks", unavailable)
    response = _verify(client, session_id, "recalibrated")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["cameras_needing_recalibration"] == ["C3", "C4"]
    assert "recalibration incomplete" in detail["message"]
    assert _suspect_flag(app, session_id) is True  # flag survives the failed backfill
    assert _audit_rows(app, "calibration_backfill") == []


def test_drift_threshold_is_overridable_within_bounds(app: FastAPI, client: TestClient) -> None:
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)

    # 3 px shift: clean under the default 5 px, drifted under a 2 px threshold.
    default = _drift_check(client, session_id, dx=3.0)
    assert default.json()["drifted"] is False
    strict = _drift_check(client, session_id, dx=3.0, threshold_px=2.0)
    strict_body = strict.json()
    assert strict_body["drifted"] is True
    assert strict_body["threshold_px"] == 2.0

    for out_of_bounds in (0.5, 51.0):
        response = _drift_check(client, session_id, threshold_px=out_of_bounds)
        assert response.status_code == 422


def test_drift_check_requires_valid_extrinsic_calibration(client: TestClient) -> None:
    session_id = _create_session(client)

    unregistered = _drift_check(client, session_id)
    assert unregistered.status_code == 422
    assert "not registered as active" in unregistered.json()["detail"]

    _register_camera(client, "C3")
    no_calibration = _drift_check(client, session_id)
    assert no_calibration.status_code == 422
    detail = no_calibration.json()["detail"]
    assert "no valid extrinsic calibration" in detail

    # An era bump orphans the old calibration: drift-check must refuse, not
    # silently compare against the stale era.
    calibration_id = _create_extrinsic(client, "C3")["id"]
    _register_camera(client, "C3")  # era 2
    stale_era = _drift_check(client, session_id)
    assert stale_era.status_code == 422
    assert "era 2" in stale_era.json()["detail"]

    # Same for an invalidated record within the current era.
    fresh = _create_extrinsic(client, "C3")["id"]
    assert fresh != calibration_id
    client.post(f"/calibrations/{fresh}/invalidate", headers=auth(PARENT_TOKEN))
    invalidated = _drift_check(client, session_id)
    assert invalidated.status_code == 422


def test_drift_check_rejects_unusable_stored_calibration(app: FastAPI, client: TestClient) -> None:
    """A stored-but-singular matrix must surface as 422, never a 500."""
    _register_camera(client, "C3")
    session_id = _create_session(client)
    db = app.state.session_factory()
    try:
        db.add(
            Calibration(
                camera_id="C3",
                era_no=1,
                kind=CalibrationKind.EXTRINSIC,
                params={
                    "version": 1,
                    # Rows 0 and 2 coincide: the matrix is singular.
                    "matrix": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
                    "rms_px": 0.0,
                    "rms_m": 0.0,
                    "n_landmarks": 6,
                },
                valid=True,
            )
        )
        db.commit()
    finally:
        db.close()

    response = _drift_check(client, session_id)
    assert response.status_code == 422
    assert "stored calibration unusable; recalibrate" in response.json()["detail"]


def test_drift_check_needs_catalog_landmark_overlap(client: TestClient) -> None:
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)

    response = client.post(
        f"/sessions/{session_id}/drift-check",
        json={
            "camera_id": "C3",
            "observed_landmarks": [{"name": "net_pole_top", "px": [10.0, 10.0]}],
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422
    assert "no common landmarks" in response.json()["detail"]


def test_drift_check_access_control_and_missing_session(client: TestClient) -> None:
    _register_camera(client, "C3")
    _create_extrinsic(client, "C3")
    session_id = _create_session(client)

    forbidden = _drift_check(client, session_id, token=PLAYER_TOKEN)
    assert forbidden.status_code == 403

    missing = _drift_check(client, MISSING_ID)
    assert missing.status_code == 404
