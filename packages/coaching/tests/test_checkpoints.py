"""US-I2 action checkpoints: exact formulas on synthetic skeletons, every null path."""

import math

import pytest
from cricai_coaching.checkpoints import (
    CHECKPOINT_KEYS,
    REASON_DEGENERATE_LEG,
    REASON_DEGENERATE_TRUNK,
    REASON_FRONT_LEG_LOW,
    REASON_HEAD_FOOT_LOW,
    REASON_TRUNK_LOW,
    CheckpointConfig,
    evaluate_checkpoints,
)
from cricai_coaching.pre_release import REASON_LOW_AVAILABILITY, REASON_NO_SCALE
from cricai_vision.pose import LANDMARK_NAMES, N_LANDMARKS, Landmark, PoseFrame, PoseTrack

INDEX_TO_NAME: dict[int, str] = {index: name for name, index in LANDMARK_NAMES.items()}

#: Right-arm bowler at release: upright trunk (shoulder and hip midpoints share
#: x == 900), straight vertical front-left leg (braced, 180°), head 25 px past
#: the front ankle toward the target (+x).
RELEASE_POSE: dict[str, tuple[float, float]] = {
    "nose": (905.0, 300.0),
    "left_shoulder": (910.0, 400.0),
    "right_shoulder": (890.0, 400.0),
    "left_hip": (880.0, 700.0),
    "right_hip": (920.0, 700.0),
    "left_knee": (880.0, 850.0),
    "right_knee": (930.0, 860.0),
    "left_ankle": (880.0, 1000.0),
    "right_ankle": (950.0, 990.0),
}


def make_frame(
    frame_no: int,
    points: dict[str, tuple[float, float]] | None = None,
    *,
    visibility: float = 0.9,
    vis_overrides: dict[str, float] | None = None,
    default: tuple[float, float] = (500.0, 500.0),
) -> PoseFrame:
    """Build a full 33-landmark frame from named points; the rest sit at ``default``."""
    points = points or {}
    vis_overrides = vis_overrides or {}
    landmarks = []
    for index in range(N_LANDMARKS):
        name = INDEX_TO_NAME.get(index)
        xy = points.get(name, default) if name is not None else default
        vis = vis_overrides.get(name, visibility) if name is not None else visibility
        landmarks.append(Landmark(image_xy=xy, world_xyz=(0.0, 0.0, 0.0), visibility=vis))
    return PoseFrame(frame_no=frame_no, landmarks=tuple(landmarks))


def one_frame_track(
    points: dict[str, tuple[float, float]],
    *,
    visibility: float = 0.9,
    vis_overrides: dict[str, float] | None = None,
) -> PoseTrack:
    frame = make_frame(0, points, visibility=visibility, vis_overrides=vis_overrides)
    return PoseTrack(
        model_name="synthetic",
        model_version="1",
        fps=120.0,
        frames=(frame,),
        subject_confidence=0.9,
    )


def leg_points(knee_angle_deg: float) -> dict[str, tuple[float, float]]:
    """Front-left leg with an exact hip-knee-ankle angle: shank vertical down,
    thigh rotated ``knee_angle_deg`` from the shank about the knee."""
    knee = (880.0, 850.0)
    ankle = (880.0, 1000.0)  # straight down from the knee
    theta = math.radians(knee_angle_deg)
    thigh = 150.0
    # Angle measured from the knee->ankle direction (0, +1): rotate toward -x.
    hip = (knee[0] - thigh * math.sin(theta), knee[1] + thigh * math.cos(theta))
    return {**RELEASE_POSE, "left_knee": knee, "left_ankle": ankle, "left_hip": hip}


def test_release_pose_scores_every_checkpoint() -> None:
    metrics = evaluate_checkpoints(one_frame_track(RELEASE_POSE), release_frame=0, px_per_cm=10.0)
    assert tuple(metrics) == CHECKPOINT_KEYS
    assert metrics["brace_state"].value == "braced"
    assert metrics["falling_away_deg"].value == pytest.approx(0.0)
    assert metrics["head_offset_at_release_px"].value == pytest.approx(25.0)
    assert metrics["head_offset_at_release_cm"].value == pytest.approx(2.5)
    for metric in metrics.values():
        assert metric.value is not None
        assert metric.confidence == pytest.approx(0.9)


@pytest.mark.parametrize(
    ("knee_angle_deg", "expected"),
    [
        (180.0, "braced"),
        (170.0, "braced"),
        (155.0, "bent"),
        (145.0, "bent"),
        (120.0, "collapsed"),
    ],
)
def test_brace_bands_over_exact_knee_angles(knee_angle_deg: float, expected: str) -> None:
    metrics = evaluate_checkpoints(one_frame_track(leg_points(knee_angle_deg)), release_frame=0)
    assert metrics["brace_state"].value == expected


def test_band_edges_braced_inclusive_collapsed_exclusive() -> None:
    """An exact right angle (float-exact geometry: perpendicular thigh/shank)
    lands ON a band edge: braced at its inclusive edge, bent at collapsed's."""
    points = {
        **RELEASE_POSE,
        "left_hip": (730.0, 850.0),
        "left_knee": (880.0, 850.0),
        "left_ankle": (880.0, 1000.0),
    }
    track = one_frame_track(points)
    at_braced_edge = CheckpointConfig(braced_min_deg=90.0, collapsed_max_deg=80.0)
    metrics = evaluate_checkpoints(track, release_frame=0, config=at_braced_edge)
    assert metrics["brace_state"].value == "braced"
    at_collapsed_edge = CheckpointConfig(braced_min_deg=170.0, collapsed_max_deg=90.0)
    metrics = evaluate_checkpoints(track, release_frame=0, config=at_collapsed_edge)
    assert metrics["brace_state"].value == "bent"


def test_left_arm_bowler_uses_the_right_front_leg() -> None:
    """A left-arm bowler lands on the right foot: bend the RIGHT knee, see it."""
    points = {
        **RELEASE_POSE,
        "right_hip": (1000.0, 700.0),  # thigh leaning hard: collapsed right knee
        "right_knee": (930.0, 860.0),
        "right_ankle": (930.0, 1000.0),
    }
    config = CheckpointConfig(arm="left")
    assert config.front_side == "right"
    metrics = evaluate_checkpoints(one_frame_track(points), release_frame=0, config=config)
    assert metrics["brace_state"].value != "braced"


def test_occluded_front_leg_is_null_with_reason() -> None:
    track = one_frame_track(RELEASE_POSE, vis_overrides={"left_knee": 0.1})
    metrics = evaluate_checkpoints(track, release_frame=0)
    assert metrics["brace_state"].value is None
    assert metrics["brace_state"].reason == REASON_FRONT_LEG_LOW
    assert metrics["brace_state"].confidence == 0.0


def test_coincident_leg_landmarks_are_degenerate() -> None:
    points = {**RELEASE_POSE, "left_hip": (880.0, 850.0), "left_ankle": (880.0, 850.0)}
    points["left_knee"] = (880.0, 850.0)
    metrics = evaluate_checkpoints(one_frame_track(points), release_frame=0)
    assert metrics["brace_state"].reason == REASON_DEGENERATE_LEG


def test_falling_away_measures_the_signed_trunk_lean() -> None:
    """Shoulders shifted 100 px toward +x over a 100 px rise = 45° falling away
    for a right-arm bowler; the sign flips for a left-arm action."""
    points = {
        **RELEASE_POSE,
        "left_shoulder": (1010.0, 600.0),
        "right_shoulder": (990.0, 600.0),
        "left_hip": (910.0, 700.0),
        "right_hip": (890.0, 700.0),
    }
    track = one_frame_track(points)
    right = evaluate_checkpoints(track, release_frame=0)
    assert right["falling_away_deg"].value == pytest.approx(45.0)
    left = evaluate_checkpoints(track, release_frame=0, config=CheckpointConfig(arm="left"))
    assert left["falling_away_deg"].value == pytest.approx(-45.0)


def test_occluded_trunk_is_null_with_reason() -> None:
    track = one_frame_track(RELEASE_POSE, vis_overrides={"right_shoulder": 0.2})
    metrics = evaluate_checkpoints(track, release_frame=0)
    assert metrics["falling_away_deg"].reason == REASON_TRUNK_LOW


def test_coincident_trunk_midpoints_are_degenerate() -> None:
    points = {
        **RELEASE_POSE,
        "left_shoulder": (910.0, 700.0),
        "right_shoulder": (890.0, 700.0),
        "left_hip": (910.0, 700.0),
        "right_hip": (890.0, 700.0),
    }
    metrics = evaluate_checkpoints(one_frame_track(points), release_frame=0)
    assert metrics["falling_away_deg"].reason == REASON_DEGENERATE_TRUNK


def test_head_offset_sign_follows_the_target_direction() -> None:
    mirrored = CheckpointConfig(target_toward_positive_x=False)
    metrics = evaluate_checkpoints(one_frame_track(RELEASE_POSE), release_frame=0, config=mirrored)
    assert metrics["head_offset_at_release_px"].value == pytest.approx(-25.0)


def test_head_offset_cm_needs_a_scale() -> None:
    metrics = evaluate_checkpoints(one_frame_track(RELEASE_POSE), release_frame=0)
    assert metrics["head_offset_at_release_px"].value == pytest.approx(25.0)
    assert metrics["head_offset_at_release_cm"].value is None
    assert metrics["head_offset_at_release_cm"].reason == REASON_NO_SCALE


def test_occluded_head_nulls_both_offset_twins() -> None:
    track = one_frame_track(RELEASE_POSE, vis_overrides={"nose": 0.1})
    metrics = evaluate_checkpoints(track, release_frame=0, px_per_cm=10.0)
    assert metrics["head_offset_at_release_px"].reason == REASON_HEAD_FOOT_LOW
    assert metrics["head_offset_at_release_cm"].reason == REASON_HEAD_FOOT_LOW


def test_low_availability_nulls_every_checkpoint() -> None:
    track = one_frame_track(RELEASE_POSE, visibility=0.2)
    metrics = evaluate_checkpoints(track, release_frame=0, px_per_cm=10.0)
    assert set(metrics) == set(CHECKPOINT_KEYS)
    for metric in metrics.values():
        assert metric.value is None
        assert metric.reason == REASON_LOW_AVAILABILITY
        assert metric.confidence == 0.0


def test_caller_contract_violations_are_loud() -> None:
    track = one_frame_track(RELEASE_POSE)
    with pytest.raises(ValueError, match="px_per_cm"):
        evaluate_checkpoints(track, release_frame=0, px_per_cm=-1.0)
    with pytest.raises(ValueError, match="release_frame 5 outside"):
        evaluate_checkpoints(track, release_frame=5)


def test_config_validation_is_loud() -> None:
    with pytest.raises(ValueError, match="min_visibility"):
        CheckpointConfig(min_visibility=2.0)
    with pytest.raises(ValueError, match="brace bands"):
        CheckpointConfig(braced_min_deg=130.0, collapsed_max_deg=140.0)
