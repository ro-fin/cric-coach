"""US-C2 foundation: coordinate-frame contract, landmarks, synthetic pinhole camera."""

import numpy as np
import pytest
from cricai_vision.geometry import (
    BALL_DIAMETER_M,
    PITCH_LENGTH_M,
    PITCH_WIDTH_M,
    POPPING_CREASE_OFFSET_M,
    PinholeCamera,
    landmark_catalog,
    make_overhead_camera,
)


def test_pitch_constants_match_law() -> None:
    assert PITCH_LENGTH_M == 20.12
    assert PITCH_WIDTH_M == 3.05
    assert POPPING_CREASE_OFFSET_M == 1.22
    assert pytest.approx(0.073) == BALL_DIAMETER_M


def test_landmark_catalog_frame_contract() -> None:
    lm = landmark_catalog()
    origin = lm["striker_middle_stump_base"]
    assert origin.xy == (0.0, 0.0)
    # +x toward bowler
    assert lm["bowler_middle_stump_base"].x == PITCH_LENGTH_M
    # +y toward off side for RH batter — striker's off stump on +y
    assert lm["striker_off_stump_base"].y > 0
    assert lm["striker_leg_stump_base"].y < 0
    # popping crease in front of stumps
    assert lm["striker_popping_crease_center"].x == POPPING_CREASE_OFFSET_M
    assert lm["bowler_popping_crease_center"].x == PITCH_LENGTH_M - POPPING_CREASE_OFFSET_M
    # off/leg labels consistent across ends (striker's perspective)
    assert lm["bowler_off_stump_base"].y > 0
    assert lm["bowler_popping_crease_off"].y > 0


def test_landmark_names_are_unique_and_sufficient_for_homography() -> None:
    lm = landmark_catalog()
    assert len(lm) >= 6  # US-C2: operator clicks >= 6 landmarks
    xs = {p.xy for p in lm.values()}
    assert len(xs) == len(lm)  # no duplicate positions


def test_pinhole_projection_center_ray() -> None:
    camera_matrix = np.array([[1000.0, 0, 640], [0, 1000.0, 360], [0, 0, 1]])
    rotation = np.eye(3)
    translation = np.array([0.0, 0.0, 5.0])  # world origin 5m in front
    cam = PinholeCamera(camera_matrix, rotation, translation)
    uv = cam.project(np.array([[0.0, 0.0, 0.0]]))
    assert uv[0] == pytest.approx([640.0, 360.0])
    # a point 1m right in world x maps fx/z pixels right
    uv2 = cam.project(np.array([[1.0, 0.0, 0.0]]))
    assert uv2[0][0] == pytest.approx(640.0 + 1000.0 / 5.0 * 1.0)


def test_pinhole_rejects_points_behind_camera() -> None:
    cam = PinholeCamera(np.eye(3), np.eye(3), np.array([0.0, 0.0, -1.0]))
    with pytest.raises(ValueError, match="behind"):
        cam.project(np.array([[0.0, 0.0, 0.0]]))


def test_overhead_camera_sees_whole_pitch() -> None:
    cam = make_overhead_camera()
    lm = landmark_catalog()
    points = np.array([[p.x, p.y] for p in lm.values()])
    uv = cam.project_pitch_xy(points)
    assert np.all(np.isfinite(uv))
    # both ends project to distinct pixels (no degenerate collapse)
    spread = uv.max(axis=0) - uv.min(axis=0)
    assert spread[0] > 50
    assert spread[1] > 50


def test_project_pitch_xy_matches_full_projection() -> None:
    cam = make_overhead_camera(height_m=5.0)
    xy = np.array([[10.0, 1.0]])
    xyz = np.array([[10.0, 1.0, 0.0]])
    assert cam.project_pitch_xy(xy) == pytest.approx(cam.project(xyz))
