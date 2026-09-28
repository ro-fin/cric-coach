"""US-E2 pre-release metrics: exact formulas on synthetic skeletons, every null path."""

from collections.abc import Iterable

import pytest
from cricai_coaching.pre_release import (
    REASON_ANKLES_LOW,
    REASON_HEAD_BASE_LOW,
    REASON_INSUFFICIENT_FRAMES,
    REASON_LOW_AVAILABILITY,
    REASON_NO_PICKUP,
    REASON_NO_SCALE,
    REASON_NO_TRIGGER,
    REASON_NOSE_LOW,
    REASON_REFERENCE_BEFORE_TRACK,
    REASON_TRIGGER_LOW_VIS,
    REASON_WRISTS_LOW,
    Handedness,
    MetricValue,
    PreReleaseConfig,
    compute_pre_release,
)
from cricai_vision.pose import (
    LANDMARK_NAMES,
    N_LANDMARKS,
    FakePoseProvider,
    Landmark,
    PoseFrame,
    PoseTrack,
)

INDEX_TO_NAME: dict[int, str] = {index: name for name, index in LANDMARK_NAMES.items()}

EXPECTED_KEYS = {
    "stance_width_cm",
    "stance_width_px",
    "head_offset_cm",
    "head_offset_px",
    "trigger_start_ms",
    "still_at_release",
    "head_speed_at_release",
    "pickup_direction_deg",
}

#: The pinned per-value contract keys (mirrors the ball-metrics API validator).
CONTRACT_KEYS = {"value", "unit", "confidence", "reason", "proxy", "source"}

#: Known-geometry stance: ankle gap hypot(60, 80) == 100 px, mid-ankle x == 130.
STANCE: dict[str, tuple[float, float]] = {
    "nose": (140.0, 100.0),
    "left_ankle": (100.0, 400.0),
    "right_ankle": (160.0, 480.0),
    "left_hip": (120.0, 300.0),
    "right_hip": (150.0, 300.0),
}

TRIGGER_LANDMARKS = ("left_ankle", "right_ankle", "left_hip", "right_hip")


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


def make_track(frames: Iterable[PoseFrame], fps: float = 100.0) -> PoseTrack:
    return PoseTrack(
        model_name="synthetic",
        model_version="1",
        fps=fps,
        frames=tuple(frames),
        subject_confidence=0.9,
    )


def static_track(
    n_frames: int,
    points: dict[str, tuple[float, float]] | None = None,
    *,
    visibility: float = 0.9,
    vis_overrides: dict[str, float] | None = None,
) -> PoseTrack:
    return make_track(
        make_frame(f, points, visibility=visibility, vis_overrides=vis_overrides)
        for f in range(n_frames)
    )


def shifted(points: dict[str, tuple[float, float]], shift: float) -> dict[str, tuple[float, float]]:
    """Shift the trigger landmarks (ankles + hips) by ``shift`` px in x."""
    return {
        name: ((x + shift, y) if name in TRIGGER_LANDMARKS else (x, y))
        for name, (x, y) in points.items()
    }


def valued(metric: MetricValue) -> float:
    assert metric["value"] is not None
    assert "reason" not in metric
    return float(metric["value"])


def assert_null(metric: MetricValue, reason: str) -> None:
    assert metric["value"] is None
    assert metric["reason"] == reason
    assert metric["confidence"] == 0.0


# ---------------------------------------------------------------- exact geometry


def _track_with_pickup() -> PoseTrack:
    """Static stance, nose still; mid-wrist moves (+5, -5) px/frame — 45 deg pickup."""
    frames = []
    for f in range(21):
        points = dict(STANCE)
        points["left_wrist"] = (295.0 + 5.0 * f, 505.0 - 5.0 * f)
        points["right_wrist"] = (305.0 + 5.0 * f, 495.0 - 5.0 * f)
        frames.append(make_frame(f, points))
    return make_track(frames)


def test_exact_values_on_known_geometry() -> None:
    result = compute_pre_release(_track_with_pickup(), release_frame=20, px_per_cm=2.0)
    assert set(result) == EXPECTED_KEYS
    assert valued(result["stance_width_px"]) == pytest.approx(100.0)
    assert result["stance_width_px"]["unit"] == "px"
    assert result["stance_width_px"]["confidence"] == pytest.approx(0.9)
    assert valued(result["stance_width_cm"]) == pytest.approx(50.0)
    assert result["stance_width_cm"]["unit"] == "cm"
    assert valued(result["head_offset_px"]) == pytest.approx(10.0)
    assert valued(result["head_offset_cm"]) == pytest.approx(5.0)
    assert valued(result["pickup_direction_deg"]) == pytest.approx(45.0)
    assert result["pickup_direction_deg"]["unit"] == "deg"
    still = result["still_at_release"]
    assert still["value"] is True
    assert still["unit"] == "bool"
    head_speed = result["head_speed_at_release"]
    assert valued(head_speed) == pytest.approx(0.0)
    assert head_speed["unit"] == "px_per_ms"
    assert head_speed["confidence"] == still["confidence"]
    assert_null(result["trigger_start_ms"], REASON_NO_TRIGGER)
    # Every emitted entry conforms to the pinned metric-value contract (US-E2).
    for metric in result.values():
        assert set(metric) <= CONTRACT_KEYS


def test_head_offset_sign_flips_for_left_hander() -> None:
    track = static_track(21, STANCE)
    cases: tuple[tuple[Handedness, float], ...] = (("right", 10.0), ("left", -10.0))
    for handedness, expected in cases:
        config = PreReleaseConfig(handedness=handedness)
        result = compute_pre_release(track, release_frame=20, config=config)
        assert valued(result["head_offset_px"]) == pytest.approx(expected)


def test_no_calibration_scale_nulls_cm_but_keeps_px() -> None:
    result = compute_pre_release(static_track(21, STANCE), release_frame=20, px_per_cm=None)
    assert_null(result["stance_width_cm"], REASON_NO_SCALE)
    assert_null(result["head_offset_cm"], REASON_NO_SCALE)
    assert valued(result["stance_width_px"]) == pytest.approx(100.0)
    assert valued(result["head_offset_px"]) == pytest.approx(10.0)


def test_reference_offset_samples_the_configured_earlier_frame() -> None:
    wide = dict(STANCE, right_ankle=(200.0, 400.0), left_ankle=(100.0, 400.0))
    narrow = dict(STANCE, right_ankle=(160.0, 400.0), left_ankle=(100.0, 400.0))
    frames = [make_frame(f, narrow if f <= 7 else wide) for f in range(11)]
    config = PreReleaseConfig(reference_offset_frames=5)
    result = compute_pre_release(make_track(frames), release_frame=10, config=config)
    assert valued(result["stance_width_px"]) == pytest.approx(60.0)


def test_reference_frame_before_track_start_is_null() -> None:
    config = PreReleaseConfig(reference_offset_frames=15)
    result = compute_pre_release(
        static_track(11, STANCE), release_frame=10, px_per_cm=2.0, config=config
    )
    assert_null(result["stance_width_px"], REASON_REFERENCE_BEFORE_TRACK)
    assert_null(result["stance_width_cm"], REASON_REFERENCE_BEFORE_TRACK)
    assert_null(result["head_offset_px"], REASON_REFERENCE_BEFORE_TRACK)
    assert_null(result["head_offset_cm"], REASON_REFERENCE_BEFORE_TRACK)


def test_low_ankle_visibility_nulls_stance_and_head() -> None:
    track = static_track(21, STANCE, vis_overrides={"left_ankle": 0.2})
    result = compute_pre_release(track, release_frame=20, px_per_cm=2.0)
    assert_null(result["stance_width_px"], REASON_ANKLES_LOW)
    assert_null(result["stance_width_cm"], REASON_ANKLES_LOW)
    assert_null(result["head_offset_px"], REASON_HEAD_BASE_LOW)
    assert_null(result["head_offset_cm"], REASON_HEAD_BASE_LOW)


def test_confidence_is_min_of_landmark_visibilities() -> None:
    track = static_track(21, STANCE, vis_overrides={"left_ankle": 0.6, "right_ankle": 0.8})
    result = compute_pre_release(track, release_frame=20, px_per_cm=2.0)
    assert result["stance_width_px"]["confidence"] == pytest.approx(0.6)
    assert result["stance_width_cm"]["confidence"] == pytest.approx(0.6)
    assert result["head_offset_px"]["confidence"] == pytest.approx(0.6)


# ---------------------------------------------------------------- trigger timing


def _trigger_track(
    move_from: int = 10,
    *,
    blip_at: int | None = None,
    low_vis_frame: int | None = None,
) -> PoseTrack:
    """21 frames at 100 fps: trigger landmarks move +1 px/frame from ``move_from`` on."""
    frames = []
    for f in range(21):
        shift = float(max(0, f - (move_from - 1)))
        if blip_at is not None and blip_at <= f < move_from:
            shift += 2.0
        vis_overrides = {"left_ankle": 0.2} if f == low_vis_frame else None
        frames.append(make_frame(f, shifted(STANCE, shift), vis_overrides=vis_overrides))
    return make_track(frames)


def test_trigger_onset_detected_at_constructed_movement_frame() -> None:
    result = compute_pre_release(_trigger_track(move_from=10), release_frame=20)
    trigger = result["trigger_start_ms"]
    assert valued(trigger) == pytest.approx(-100.0)
    assert trigger["unit"] == "ms"
    assert trigger["confidence"] == pytest.approx(0.9)


def test_single_interval_blip_does_not_count_as_sustained() -> None:
    track = _trigger_track(move_from=10, blip_at=5)
    result = compute_pre_release(track, release_frame=20)
    assert valued(result["trigger_start_ms"]) == pytest.approx(-100.0)


def test_low_visibility_interval_resets_the_sustained_run() -> None:
    track = _trigger_track(move_from=10, low_vis_frame=11)
    result = compute_pre_release(track, release_frame=20)
    assert valued(result["trigger_start_ms"]) == pytest.approx(-70.0)


def test_trigger_null_when_all_intervals_low_visibility() -> None:
    overrides = dict.fromkeys(TRIGGER_LANDMARKS, 0.2)
    track = static_track(21, STANCE, vis_overrides=overrides)
    result = compute_pre_release(track, release_frame=20)
    assert_null(result["trigger_start_ms"], REASON_TRIGGER_LOW_VIS)


def test_trigger_null_when_no_movement() -> None:
    result = compute_pre_release(static_track(21, STANCE), release_frame=20)
    assert_null(result["trigger_start_ms"], REASON_NO_TRIGGER)


def test_insufficient_frames_before_release() -> None:
    result = compute_pre_release(static_track(5, STANCE), release_frame=2)
    assert_null(result["trigger_start_ms"], REASON_INSUFFICIENT_FRAMES)
    assert_null(result["still_at_release"], REASON_INSUFFICIENT_FRAMES)
    assert_null(result["head_speed_at_release"], REASON_INSUFFICIENT_FRAMES)
    assert_null(result["pickup_direction_deg"], REASON_INSUFFICIENT_FRAMES)


# ---------------------------------------------------------------- stillness


def test_moving_head_at_release_is_not_still() -> None:
    frames = [make_frame(f, dict(STANCE, nose=(100.0 + 2.0 * f, 100.0))) for f in range(10)]
    result = compute_pre_release(make_track(frames), release_frame=8)
    still = result["still_at_release"]
    assert still["value"] is False
    assert still["confidence"] == pytest.approx(0.9)
    head_speed = result["head_speed_at_release"]
    assert valued(head_speed) == pytest.approx(0.2)
    assert head_speed["unit"] == "px_per_ms"
    assert head_speed["confidence"] == pytest.approx(0.9)


def test_still_head_below_threshold_is_still() -> None:
    result = compute_pre_release(static_track(10, STANCE), release_frame=8)
    assert result["still_at_release"]["value"] is True
    assert valued(result["head_speed_at_release"]) == pytest.approx(0.0)


def test_low_nose_visibility_nulls_stillness() -> None:
    track = static_track(10, STANCE, vis_overrides={"nose": 0.2})
    result = compute_pre_release(track, release_frame=8)
    assert_null(result["still_at_release"], REASON_NOSE_LOW)
    assert_null(result["head_speed_at_release"], REASON_NOSE_LOW)


# ---------------------------------------------------------------- pickup direction


def test_pickup_straight_up_is_90_degrees() -> None:
    frames = []
    for f in range(21):
        points = dict(STANCE)
        points["left_wrist"] = (300.0, 500.0 - 10.0 * f)
        points["right_wrist"] = (300.0, 500.0 - 10.0 * f)
        frames.append(make_frame(f, points))
    result = compute_pre_release(make_track(frames), release_frame=20)
    assert valued(result["pickup_direction_deg"]) == pytest.approx(90.0)


def test_low_wrist_visibility_nulls_pickup() -> None:
    track = static_track(21, STANCE, vis_overrides={"right_wrist": 0.2})
    result = compute_pre_release(track, release_frame=20)
    assert_null(result["pickup_direction_deg"], REASON_WRISTS_LOW)


def test_static_wrists_null_pickup_with_reason() -> None:
    result = compute_pre_release(static_track(21, STANCE), release_frame=20)
    assert_null(result["pickup_direction_deg"], REASON_NO_PICKUP)


# ---------------------------------------------------------------- availability gate


def test_insufficient_availability_nulls_every_metric() -> None:
    track = static_track(21, STANCE, visibility=0.3)
    result = compute_pre_release(track, release_frame=20, px_per_cm=2.0)
    assert set(result) == EXPECTED_KEYS
    for metric in result.values():
        assert_null(metric, REASON_LOW_AVAILABILITY)


def test_availability_gate_applies_before_release_frame_validation() -> None:
    empty = make_track([])
    result = compute_pre_release(empty, release_frame=0)
    for metric in result.values():
        assert_null(metric, REASON_LOW_AVAILABILITY)


# ---------------------------------------------------------------- contract violations


def test_release_frame_out_of_range_raises() -> None:
    track = static_track(5, STANCE)
    with pytest.raises(ValueError, match="release_frame"):
        compute_pre_release(track, release_frame=-1)
    with pytest.raises(ValueError, match="release_frame"):
        compute_pre_release(track, release_frame=5)


def test_non_positive_px_per_cm_raises() -> None:
    with pytest.raises(ValueError, match="px_per_cm"):
        compute_pre_release(static_track(5, STANCE), release_frame=2, px_per_cm=0.0)


def test_config_rejects_out_of_range_thresholds() -> None:
    with pytest.raises(ValueError, match="must be in"):
        PreReleaseConfig(min_availability=1.5)
    with pytest.raises(ValueError, match="frame windows"):
        PreReleaseConfig(stillness_window_frames=0)
    with pytest.raises(ValueError, match="speed thresholds"):
        PreReleaseConfig(trigger_speed_px_per_ms=0.0)


# ---------------------------------------------------------------- fake provider e2e


def test_fake_provider_track_is_deterministic_and_sane() -> None:
    provider = FakePoseProvider()
    track = provider.extract([None] * 24, fps=120.0)
    first = compute_pre_release(track, release_frame=12)
    second = compute_pre_release(track, release_frame=12)
    assert first == second
    assert valued(first["stance_width_px"]) == pytest.approx(80.0)
    assert valued(first["head_offset_px"]) == pytest.approx(3.0)
    assert first["still_at_release"]["value"] is True
    assert valued(first["head_speed_at_release"]) < 0.1
    assert_null(first["trigger_start_ms"], REASON_NO_TRIGGER)
    assert_null(first["pickup_direction_deg"], REASON_NO_PICKUP)
    assert_null(first["stance_width_cm"], REASON_NO_SCALE)
