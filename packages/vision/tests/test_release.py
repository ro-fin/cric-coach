"""US-I2/I3 release detection + height: ±2 frames @ 120 fps, every null path."""

import math

import pytest
from cricai_vision.pose import LANDMARK_NAMES, N_LANDMARKS, Landmark, PoseFrame, PoseTrack
from cricai_vision.release import (
    REASON_ARM_NEVER_OVERHEAD,
    REASON_EMPTY_TRACK,
    REASON_FEET_LOW,
    REASON_HAND_BELOW_GROUND,
    REASON_NO_ARM_SWING,
    REASON_NO_RELEASE,
    REASON_NO_SCALE,
    REASON_WRIST_OCCLUDED,
    ReleaseConfig,
    ReleaseDetection,
    detect_release,
    release_height_cm,
)
from cricai_vision.triangulate import Track3D, Track3DPoint, TrackQuality3D

INDEX_TO_NAME: dict[int, str] = {index: name for name, index in LANDMARK_NAMES.items()}

FPS = 120.0
SHOULDER = (900.0, 400.0)

#: Feet planted on the pitch surface for the pose-scale height path.
FEET: dict[str, tuple[float, float]] = {
    "left_ankle": (880.0, 1000.0),
    "right_ankle": (920.0, 1000.0),
    "left_heel": (875.0, 998.0),
    "right_heel": (925.0, 998.0),
    "left_foot_index": (870.0, 996.0),
    "right_foot_index": (930.0, 996.0),
}

FOOT_NAMES = tuple(FEET)


def make_frame(
    frame_no: int,
    points: dict[str, tuple[float, float]] | None = None,
    *,
    visibility: float = 0.95,
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


def make_track(frames: list[PoseFrame], fps: float = FPS) -> PoseTrack:
    return PoseTrack(
        model_name="synthetic",
        model_version="1",
        fps=fps,
        frames=tuple(frames),
        subject_confidence=0.9,
    )


def swing_track(
    n_frames: int = 48,
    *,
    radius: float = 60.0,
    phi0_deg: float = -160.0,
    dphi_deg: float = 6.8,
    arm: str = "right",
    visibility: float = 0.95,
    vis_overrides: dict[str, float] | None = None,
    extra: dict[str, tuple[float, float]] | None = None,
    fps: float = FPS,
) -> PoseTrack:
    """Circular bowling-arm swing: the wrist arcs over the shoulder.

    The wrist sits at ``shoulder + radius * (sin phi, -cos phi)`` with ``phi``
    sweeping linearly — the arc apex (phi == 0, minimum image y) is the
    analytic release truth at frame ``-phi0_deg / dphi_deg``.
    """
    frames = []
    for i in range(n_frames):
        phi = math.radians(phi0_deg + i * dphi_deg)
        wrist = (SHOULDER[0] + radius * math.sin(phi), SHOULDER[1] - radius * math.cos(phi))
        points = {f"{arm}_shoulder": SHOULDER, f"{arm}_wrist": wrist, **FEET, **(extra or {})}
        frames.append(make_frame(i, points, visibility=visibility, vis_overrides=vis_overrides))
    return make_track(frames, fps=fps)


def make_track3d(z_m: float, *, center_ms: float, n_points: int = 11) -> Track3D:
    """Constant-height 3D ball track sampled every 8 ms around ``center_ms``."""
    half = n_points // 2
    points = tuple(
        Track3DPoint(
            frame_no=i,
            ts_ms=center_ms + (i - half) * 8.0,
            x=0.0,
            y=0.0,
            z=z_m,
            reprojection_px=0.0,
        )
        for i in range(n_points)
    )
    quality = TrackQuality3D(
        rms_reprojection_px=0.0,
        matched_fraction=1.0,
        n_frames_union=n_points,
        n_points_3d=n_points,
        n_dropped_unmatched=0,
        n_dropped_skewed=0,
        n_dropped_bridged=0,
        n_dropped_invalid=0,
        low_overlap=False,
        segments=(),
    )
    return Track3D(points=points, quality=quality)


# ---------------------------------------------------------------------------
# detect_release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phi0_deg", [-160.0, -156.6, -163.3])
def test_release_within_two_frames_of_truth_at_120fps(phi0_deg: float) -> None:
    """US-I3 AC: detection within ±2 frames @ 120 fps of the arc-apex truth."""
    dphi = 6.8
    detection = detect_release(swing_track(phi0_deg=phi0_deg, dphi_deg=dphi))
    truth = -phi0_deg / dphi
    assert detection.release_frame is not None
    assert abs(detection.release_frame - truth) <= 2
    assert detection.reason is None


def test_detection_reports_hand_position_and_track_time() -> None:
    track = swing_track()
    detection = detect_release(track)
    assert detection.release_frame is not None
    wrist = track.frames[detection.release_frame].landmark("right_wrist")
    assert detection.hand_xy == wrist.image_xy
    assert detection.release_ms == pytest.approx(detection.release_frame * 1000.0 / FPS)
    assert 0.0 < detection.confidence <= 1.0


def test_left_arm_swing_uses_the_left_wrist() -> None:
    config = ReleaseConfig(arm="left")
    assert config.wrist == "left_wrist"
    assert config.shoulder == "left_shoulder"
    detection = detect_release(swing_track(arm="left"), config=config)
    assert detection.release_frame is not None
    assert abs(detection.release_frame - 160.0 / 6.8) <= 2


def test_apex_tie_keeps_the_earliest_frame() -> None:
    frames = [
        make_frame(0, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 500.0)}),
        make_frame(1, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 300.0)}),
        make_frame(2, {"right_shoulder": SHOULDER, "right_wrist": (940.0, 300.0)}),
        make_frame(3, {"right_shoulder": SHOULDER, "right_wrist": (940.0, 500.0)}),
    ]
    detection = detect_release(make_track(frames))
    assert detection.release_frame == 1


def test_fast_swing_saturates_the_speed_confidence_factor() -> None:
    """Speed at 2x the gate or more contributes a full confidence factor."""
    frames = [
        make_frame(0, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 600.0)}),
        make_frame(1, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 300.0)}),
        make_frame(2, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 600.0)}),
    ]
    detection = detect_release(make_track(frames))
    assert detection.confidence == pytest.approx(0.95)


def test_empty_track_is_null() -> None:
    detection = detect_release(make_track([]))
    assert detection == ReleaseDetection(None, None, None, 0.0, REASON_EMPTY_TRACK)


def test_occluded_wrist_is_null_with_reason() -> None:
    detection = detect_release(swing_track(visibility=0.2))
    assert detection.release_frame is None
    assert detection.reason == REASON_WRIST_OCCLUDED


def test_arm_never_overhead_is_null_with_reason() -> None:
    frames = [
        make_frame(i, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 500.0)}) for i in range(6)
    ]
    detection = detect_release(make_track(frames))
    assert detection.reason == REASON_ARM_NEVER_OVERHEAD


def test_static_overhead_wrist_is_not_a_release() -> None:
    """Overhead but no swing (posing, not bowling): null with the swing reason."""
    frames = [
        make_frame(i, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 300.0)}) for i in range(6)
    ]
    detection = detect_release(make_track(frames))
    assert detection.reason == REASON_NO_ARM_SWING


def test_unknown_speed_never_qualifies() -> None:
    """A visible overhead frame between occluded neighbors has unknown speed —
    it never counts as a swing (no fabricated kinematics)."""
    frames = [
        make_frame(
            0,
            {"right_shoulder": SHOULDER, "right_wrist": (900.0, 500.0)},
            vis_overrides={"right_wrist": 0.1},
        ),
        make_frame(1, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 300.0)}),
        make_frame(
            2,
            {"right_shoulder": SHOULDER, "right_wrist": (900.0, 500.0)},
            vis_overrides={"right_wrist": 0.1},
        ),
    ]
    detection = detect_release(make_track(frames))
    assert detection.reason == REASON_NO_ARM_SWING


def test_single_frame_track_has_no_kinematics() -> None:
    frames = [make_frame(0, {"right_shoulder": SHOULDER, "right_wrist": (900.0, 300.0)})]
    detection = detect_release(make_track(frames))
    assert detection.reason == REASON_NO_ARM_SWING


def test_config_validation_is_loud() -> None:
    with pytest.raises(ValueError, match="min_visibility"):
        ReleaseConfig(min_visibility=1.5)
    with pytest.raises(ValueError, match="min_wrist_speed_px_per_ms"):
        ReleaseConfig(min_wrist_speed_px_per_ms=0.0)


# ---------------------------------------------------------------------------
# release_height_cm
# ---------------------------------------------------------------------------


def test_stereo_path_wins_when_the_3d_track_has_points() -> None:
    track = swing_track()
    detection = detect_release(track)
    release_ms = 5000.0
    estimate = release_height_cm(
        track,
        detection,
        stereo=(make_track3d(2.05, center_ms=release_ms), release_ms),
        px_per_cm=10.0,
    )
    assert estimate.value_cm == pytest.approx(205.0)
    assert estimate.source == "stereo"
    assert estimate.confidence == pytest.approx(1.0)
    assert estimate.reason is None


def test_stereo_miss_falls_back_to_the_pose_scale_path() -> None:
    track = swing_track()
    detection = detect_release(track)
    assert detection.hand_xy is not None
    empty_3d = Track3D(points=(), quality=make_track3d(1.0, center_ms=0.0).quality)
    estimate = release_height_cm(track, detection, stereo=(empty_3d, 5000.0), px_per_cm=10.0)
    assert estimate.source == "pose_scale"
    assert estimate.value_cm == pytest.approx((1000.0 - detection.hand_xy[1]) / 10.0)
    assert estimate.confidence == pytest.approx(detection.confidence * 0.95)


def test_stereo_miss_without_scale_keeps_the_stereo_reason() -> None:
    track = swing_track()
    detection = detect_release(track)
    empty_3d = Track3D(points=(), quality=make_track3d(1.0, center_ms=0.0).quality)
    estimate = release_height_cm(track, detection, stereo=(empty_3d, 5000.0))
    assert estimate.value_cm is None
    assert "no 3D points" in str(estimate.reason)


def test_pose_scale_path_alone() -> None:
    track = swing_track()
    detection = detect_release(track)
    assert detection.hand_xy is not None
    estimate = release_height_cm(track, detection, px_per_cm=10.0)
    assert estimate.source == "pose_scale"
    assert estimate.value_cm == pytest.approx((1000.0 - detection.hand_xy[1]) / 10.0)


def test_no_seam_at_all_is_null_with_the_scale_reason() -> None:
    track = swing_track()
    estimate = release_height_cm(track, detect_release(track))
    assert estimate.value_cm is None
    assert estimate.reason == REASON_NO_SCALE


def test_occluded_feet_null_the_pose_scale_path() -> None:
    overrides = dict.fromkeys(FOOT_NAMES, 0.1)
    track = swing_track(vis_overrides=overrides)
    estimate = release_height_cm(track, detect_release(track), px_per_cm=10.0)
    assert estimate.value_cm is None
    assert estimate.reason == REASON_FEET_LOW


def test_hand_below_ground_is_a_surfaced_geometry_bug() -> None:
    frames = [make_frame(0, FEET)]
    detection = ReleaseDetection(
        release_frame=0, release_ms=0.0, hand_xy=(900.0, 1200.0), confidence=0.9, reason=None
    )
    estimate = release_height_cm(make_track(frames), detection, px_per_cm=10.0)
    assert estimate.value_cm is None
    assert estimate.reason == REASON_HAND_BELOW_GROUND


def test_undetected_release_is_null_with_reason() -> None:
    track = swing_track()
    null_detection = ReleaseDetection(None, None, None, 0.0, REASON_EMPTY_TRACK)
    estimate = release_height_cm(track, null_detection, px_per_cm=10.0)
    assert estimate.value_cm is None
    assert estimate.reason == REASON_NO_RELEASE


def test_caller_contract_violations_are_loud() -> None:
    track = swing_track()
    detection = detect_release(track)
    with pytest.raises(ValueError, match="px_per_cm"):
        release_height_cm(track, detection, px_per_cm=0.0)
