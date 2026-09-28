"""US-A2: recording quality software surface (capture QC pure functions)."""

import numpy as np
import pytest
from cricai_vision.capture_qc import (
    BALL_DIAMETER_M,
    FlickerReport,
    SyncError,
    assert_synchronized,
    detect_frame_gaps,
    flicker_score,
    max_shutter_for_smear,
    max_sync_error_ms,
    motion_smear_length_px,
    sync_offsets,
    validate_video_metadata,
)

# ---------------------------------------------------------------------------
# validate_video_metadata
# ---------------------------------------------------------------------------


def _valid_meta() -> dict[str, object]:
    return {
        "camera_id": "C1",
        "session_id": "S-2026-07-07-01",
        "start_ts": "2026-07-07T09:30:00+00:00",
        "fps": 120,
        "resolution": "1920x1080",
        "codec": "hevc",
        "duration": 1800.5,
    }


def test_valid_metadata_has_no_problems() -> None:
    assert validate_video_metadata(_valid_meta()) == []


def test_numeric_epoch_start_ts_is_accepted() -> None:
    meta = _valid_meta()
    meta["start_ts"] = 1782898200.25
    assert validate_video_metadata(meta) == []


def test_empty_metadata_reports_all_fields_in_canonical_order() -> None:
    assert validate_video_metadata({}) == [
        "camera_id",
        "session_id",
        "start_ts",
        "fps",
        "resolution",
        "codec",
        "duration",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_id", ""),
        ("camera_id", "   "),
        ("camera_id", 7),
        ("session_id", None),
        ("start_ts", ""),
        ("start_ts", float("nan")),
        ("start_ts", True),
        ("fps", 0),
        ("fps", -30),
        ("fps", "120"),
        ("fps", True),
        ("fps", float("inf")),
        ("duration", 0.0),
        ("duration", float("nan")),
        ("resolution", "1080p"),
        ("resolution", "0x1080"),
        ("resolution", "1920x"),
        ("resolution", 1080),
        ("codec", ""),
    ],
)
def test_invalid_field_is_reported(field: str, value: object) -> None:
    meta = _valid_meta()
    meta[field] = value
    assert validate_video_metadata(meta) == [field]


def test_extra_fields_are_ignored() -> None:
    meta = _valid_meta()
    meta["lens"] = "wide"
    assert validate_video_metadata(meta) == []


# ---------------------------------------------------------------------------
# sync_offsets / max_sync_error_ms / assert_synchronized
# ---------------------------------------------------------------------------


def test_sync_offsets_relative_to_earliest_camera() -> None:
    offsets = sync_offsets({"C1": 100, "C2": 101, "C3": 106}, fps=120.0)
    assert offsets["C1"] == 0.0
    assert offsets["C2"] == pytest.approx(1000.0 / 120.0)
    assert offsets["C3"] == pytest.approx(50.0)


def test_sync_offsets_rejects_empty_mapping() -> None:
    with pytest.raises(ValueError, match="at least one camera"):
        sync_offsets({}, fps=120.0)


def test_sync_offsets_rejects_non_positive_fps() -> None:
    with pytest.raises(ValueError, match="fps must be a positive number"):
        sync_offsets({"C1": 0}, fps=0.0)


def test_max_sync_error_ms_is_spread_between_extremes() -> None:
    assert max_sync_error_ms({"C1": 0.0, "C2": 8.0, "C3": 50.0}) == pytest.approx(50.0)
    assert max_sync_error_ms({"A": -5.0, "B": 5.0}) == pytest.approx(10.0)


def test_max_sync_error_ms_empty_is_zero() -> None:
    assert max_sync_error_ms({}) == 0.0


def test_assert_synchronized_passes_within_one_frame_at_120fps() -> None:
    # US-A2 AC: sync error <= 1 frame at 120 fps (~8.3 ms).
    offsets = sync_offsets({"C1": 240, "C2": 241}, fps=120.0)
    assert max_sync_error_ms(offsets) == pytest.approx(8.333, abs=1e-3)
    assert_synchronized(offsets, fps=120.0)


def test_assert_synchronized_lists_all_offending_cameras() -> None:
    with pytest.raises(SyncError, match=r"beyond 8\.333 ms: C2, C4"):
        assert_synchronized({"C1": 0.0, "C2": 9.0, "C3": 5.0, "C4": 20.0}, fps=120.0)


def test_assert_synchronized_uses_absolute_offsets() -> None:
    with pytest.raises(SyncError, match="C2"):
        assert_synchronized({"C1": 0.0, "C2": -9.0}, fps=120.0)


def test_assert_synchronized_custom_max_frames() -> None:
    assert_synchronized({"C1": 0.0, "C2": 15.0}, fps=120.0, max_frames=2.0)
    with pytest.raises(SyncError):
        assert_synchronized({"C1": 0.0, "C2": 5.0}, fps=120.0, max_frames=0.5)


def test_assert_synchronized_rejects_non_positive_max_frames() -> None:
    with pytest.raises(ValueError, match="max_frames must be a positive number"):
        assert_synchronized({"C1": 0.0}, fps=120.0, max_frames=0.0)


def test_require_positive_rejects_nan() -> None:
    with pytest.raises(ValueError, match="fps must be a positive number"):
        assert_synchronized({"C1": 0.0}, fps=float("nan"))


# ---------------------------------------------------------------------------
# flicker_score
# ---------------------------------------------------------------------------


def test_flicker_detected_for_mains_harmonic_modulation() -> None:
    # 100 Hz modulation = first harmonic of 50 Hz mains, sampled at 240 fps.
    fps = 240.0
    t = np.arange(480, dtype=np.float64) / fps
    brightness = 120.0 + 10.0 * np.sin(2.0 * np.pi * 100.0 * t)
    report = flicker_score(brightness, fps=fps)
    assert isinstance(report, FlickerReport)
    assert report.flicker_detected
    assert report.mains_band_power_ratio > 0.9
    assert report.dominant_hz == pytest.approx(100.0)


def test_no_flicker_for_constant_plus_noise() -> None:
    rng = np.random.default_rng(42)
    brightness = 120.0 + rng.normal(0.0, 1.0, size=2048)
    report = flicker_score(brightness, fps=240.0)
    assert not report.flicker_detected
    assert report.mains_band_power_ratio < 0.3


def test_constant_brightness_has_zero_ac_power() -> None:
    report = flicker_score(np.full(512, 128.0), fps=120.0)
    assert report == FlickerReport(
        dominant_hz=0.0, mains_band_power_ratio=0.0, flicker_detected=False
    )


def test_flicker_threshold_is_configurable() -> None:
    fps = 240.0
    t = np.arange(480, dtype=np.float64) / fps
    brightness = 120.0 + 10.0 * np.sin(2.0 * np.pi * 100.0 * t)
    report = flicker_score(brightness, fps=fps, threshold=1.5)
    assert report.mains_band_power_ratio > 0.9
    assert not report.flicker_detected


def test_flicker_respects_mains_frequency_argument() -> None:
    # 100 Hz banding sits in the 50 Hz mains band but not in the 60 Hz one.
    fps = 480.0
    t = np.arange(960, dtype=np.float64) / fps
    brightness = 120.0 + 10.0 * np.sin(2.0 * np.pi * 100.0 * t)
    assert flicker_score(brightness, fps=fps, mains_hz=50.0).flicker_detected
    assert not flicker_score(brightness, fps=fps, mains_hz=60.0).flicker_detected


def test_flicker_rejects_non_1d_input() -> None:
    with pytest.raises(ValueError, match="1-D time series"):
        flicker_score(np.zeros((4, 4)), fps=120.0)


def test_flicker_rejects_too_short_series() -> None:
    with pytest.raises(ValueError, match="at least two samples"):
        flicker_score(np.array([1.0]), fps=120.0)


@pytest.mark.parametrize(("fps", "mains_hz"), [(0.0, 50.0), (120.0, -50.0)])
def test_flicker_rejects_non_positive_rates(fps: float, mains_hz: float) -> None:
    with pytest.raises(ValueError, match="must be a positive number"):
        flicker_score(np.zeros(16), fps=fps, mains_hz=mains_hz)


# ---------------------------------------------------------------------------
# motion smear
# ---------------------------------------------------------------------------


def test_motion_smear_known_value() -> None:
    # 120 kph = 33.333 m/s; 1/1000 s shutter; 300 px/m -> 10 px streak.
    assert motion_smear_length_px(120.0, 1.0 / 1000.0, 300.0) == pytest.approx(10.0)


def test_shutter_smear_round_trip() -> None:
    shutter = max_shutter_for_smear(ball_speed_kph=95.0, px_per_m=850.0, max_smear_px=40.0)
    assert motion_smear_length_px(95.0, shutter, 850.0) == pytest.approx(40.0)


def test_two_ball_diameter_smear_budget() -> None:
    # US-A2 AC: no smear longer than 2 ball-diameters (~7.3 cm each).
    px_per_m = 1000.0
    max_smear_px = 2.0 * BALL_DIAMETER_M * px_per_m
    assert max_smear_px == pytest.approx(146.0)
    shutter = max_shutter_for_smear(140.0, px_per_m, max_smear_px)
    assert motion_smear_length_px(140.0, shutter, px_per_m) == pytest.approx(146.0)
    # A faster shutter keeps the streak under budget.
    assert motion_smear_length_px(140.0, shutter / 2.0, px_per_m) < max_smear_px


@pytest.mark.parametrize(
    ("speed", "shutter", "ppm"),
    [(0.0, 0.001, 300.0), (120.0, 0.0, 300.0), (120.0, 0.001, -1.0)],
)
def test_motion_smear_rejects_non_positive_args(speed: float, shutter: float, ppm: float) -> None:
    with pytest.raises(ValueError, match="must be a positive number"):
        motion_smear_length_px(speed, shutter, ppm)


@pytest.mark.parametrize(
    ("speed", "ppm", "smear"),
    [(-5.0, 300.0, 10.0), (120.0, 0.0, 10.0), (120.0, 300.0, 0.0)],
)
def test_max_shutter_rejects_non_positive_args(speed: float, ppm: float, smear: float) -> None:
    with pytest.raises(ValueError, match="must be a positive number"):
        max_shutter_for_smear(speed, ppm, smear)


# ---------------------------------------------------------------------------
# detect_frame_gaps
# ---------------------------------------------------------------------------


def test_clean_30_minute_recording_has_zero_gaps() -> None:
    # US-A2 AC: zero dropped-frame periods > 100 ms in a 30-min recording.
    fps = 120.0
    timestamps = [i / fps for i in range(int(30 * 60 * fps))]
    assert detect_frame_gaps(timestamps, fps=fps) == []


def test_single_gap_reported_with_preceding_index() -> None:
    fps = 120.0
    timestamps = [0.0, 1 / fps, 2 / fps, 2 / fps + 0.150, 2 / fps + 0.150 + 1 / fps]
    gaps = detect_frame_gaps(timestamps, fps=fps)
    assert len(gaps) == 1
    index, gap_ms = gaps[0]
    assert index == 2
    assert gap_ms == pytest.approx(150.0)


def test_multiple_gaps_reported_in_order() -> None:
    timestamps = [0.0, 0.2, 0.21, 0.22, 0.72]
    gaps = detect_frame_gaps(timestamps, fps=100.0)
    assert [index for index, _ in gaps] == [0, 3]
    assert gaps[0][1] == pytest.approx(200.0)
    assert gaps[1][1] == pytest.approx(500.0)


def test_gap_exactly_at_threshold_is_not_reported() -> None:
    # 0.125 s is exactly representable, so the comparison is exact.
    assert detect_frame_gaps([0.0, 0.125], fps=16.0, max_gap_ms=125.0) == []
    gaps = detect_frame_gaps([0.0, 0.130], fps=16.0, max_gap_ms=125.0)
    assert [index for index, _ in gaps] == [0]
    assert gaps[0][1] == pytest.approx(130.0)


@pytest.mark.parametrize("timestamps", [[], [0.5]])
def test_short_recordings_have_no_gaps(timestamps: list[float]) -> None:
    assert detect_frame_gaps(timestamps, fps=120.0) == []


def test_decreasing_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match=r"non-decreasing .*after index 1"):
        detect_frame_gaps([0.0, 0.01, 0.005, 0.02], fps=120.0)


def test_max_gap_below_frame_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="below one frame interval"):
        detect_frame_gaps([0.0, 0.2], fps=5.0, max_gap_ms=100.0)


def test_frame_gaps_reject_non_positive_parameters() -> None:
    with pytest.raises(ValueError, match="fps must be a positive number"):
        detect_frame_gaps([0.0, 0.1], fps=-120.0)
    with pytest.raises(ValueError, match="max_gap_ms must be a positive number"):
        detect_frame_gaps([0.0, 0.1], fps=120.0, max_gap_ms=0.0)
