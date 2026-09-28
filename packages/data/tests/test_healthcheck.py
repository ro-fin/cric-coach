"""US-A4: health-check framework — every failure mode reports its named check."""

import pytest
from cricai_data.healthcheck import (
    CameraStats,
    FrameStats,
    HealthCheckInputs,
    Thresholds,
    check_disk,
    check_exposure,
    check_feed,
    check_fps,
    check_sync,
    estimate_session_bytes,
    run_health_check,
)


def _camera(
    camera_id: str = "C1",
    mean_brightness: float = 128.0,
    frames_delta: int = 240,
    measured_fps: float = 240.0,
) -> CameraStats:
    return CameraStats(
        camera_id=camera_id,
        frame_stats=FrameStats(mean_brightness=mean_brightness, frames_delta=frames_delta),
        measured_fps=measured_fps,
    )


def _inputs(
    cameras: tuple[CameraStats, ...] | None = None, **overrides: float
) -> HealthCheckInputs:
    defaults: dict[str, float] = {
        "target_fps": 240.0,
        "free_disk_bytes": 100_000_000_000,
        "expected_session_bytes": 10_000_000_000,
        "max_sync_offset_ms": 2.0,
    }
    defaults.update(overrides)
    return HealthCheckInputs(
        cameras=cameras if cameras is not None else (_camera("C1"), _camera("C2")),
        target_fps=defaults["target_fps"],
        free_disk_bytes=int(defaults["free_disk_bytes"]),
        expected_session_bytes=int(defaults["expected_session_bytes"]),
        max_sync_offset_ms=defaults["max_sync_offset_ms"],
    )


def _failed_names(inputs: HealthCheckInputs, thresholds: Thresholds | None = None) -> list[str]:
    report = run_health_check(inputs, thresholds)
    assert report.passed is False
    return [result.name for result in report.results if not result.passed]


# --- check_feed -------------------------------------------------------------


def test_feed_frozen_fails() -> None:
    result = check_feed(FrameStats(mean_brightness=128.0, frames_delta=0))
    assert result.name == "feed"
    assert result.passed is False
    assert "frozen" in result.detail


def test_feed_dark_fails() -> None:
    result = check_feed(FrameStats(mean_brightness=4.9, frames_delta=100))
    assert result.passed is False
    assert "dark" in result.detail


def test_feed_frozen_reported_before_dark() -> None:
    result = check_feed(FrameStats(mean_brightness=0.0, frames_delta=0))
    assert result.passed is False
    assert "frozen" in result.detail


def test_feed_alive_passes_and_dark_threshold_is_a_parameter() -> None:
    stats = FrameStats(mean_brightness=5.0, frames_delta=1)
    assert check_feed(stats).passed is True
    assert check_feed(stats, dark_mean=10.0).passed is False


# --- check_fps --------------------------------------------------------------


def test_fps_throttled_fails() -> None:
    result = check_fps(200.0, 240.0)
    assert result.name == "fps"
    assert result.passed is False
    assert "throttled" in result.detail


def test_fps_at_tolerance_floor_passes() -> None:
    assert check_fps(228.0, 240.0, tolerance_pct=5.0).passed is True
    assert check_fps(227.9, 240.0, tolerance_pct=5.0).passed is False


def test_fps_tolerance_is_a_parameter() -> None:
    assert check_fps(228.0, 240.0, tolerance_pct=0.0).passed is False


# --- check_exposure ---------------------------------------------------------


def test_exposure_under_fails() -> None:
    result = check_exposure(10.0)
    assert result.name == "exposure"
    assert result.passed is False
    assert "under-exposed" in result.detail


def test_exposure_over_fails() -> None:
    result = check_exposure(250.0)
    assert result.passed is False
    assert "over-exposed" in result.detail


def test_exposure_band_bounds_inclusive_and_parameterised() -> None:
    assert check_exposure(20.0).passed is True
    assert check_exposure(235.0).passed is True
    assert check_exposure(100.0, lo=150.0).passed is False
    assert check_exposure(100.0, hi=50.0).passed is False


# --- check_disk -------------------------------------------------------------


def test_disk_below_headroom_fails() -> None:
    result = check_disk(free_bytes=14_999, expected_session_bytes=10_000)
    assert result.name == "disk"
    assert result.passed is False
    assert "disk full" in result.detail


def test_disk_at_exact_headroom_passes() -> None:
    assert check_disk(free_bytes=15_000, expected_session_bytes=10_000).passed is True


def test_disk_headroom_is_a_parameter() -> None:
    assert check_disk(10_000, 10_000, headroom=1.0).passed is True
    assert check_disk(10_000, 10_000, headroom=2.0).passed is False


# --- check_sync -------------------------------------------------------------


def test_sync_desync_fails() -> None:
    result = check_sync(max_offset_ms=5.0, fps=240.0)  # budget 1000/240 ~= 4.17 ms
    assert result.name == "sync"
    assert result.passed is False
    assert "desync" in result.detail


def test_sync_within_one_frame_passes() -> None:
    assert check_sync(max_offset_ms=4.0, fps=240.0).passed is True


def test_sync_max_frames_is_a_parameter() -> None:
    assert check_sync(max_offset_ms=8.0, fps=240.0, max_frames=2.0).passed is True


def test_sync_rejects_non_positive_fps() -> None:
    with pytest.raises(ValueError, match="fps must be positive"):
        check_sync(max_offset_ms=1.0, fps=0.0)


# --- estimate_session_bytes -------------------------------------------------


def test_estimate_session_bytes_exact_math() -> None:
    # 500 balls x 4 cameras x 8 s x 25 Mbps x 125_000 B/megabit = 50 GB
    assert estimate_session_bytes(500, 4, 25.0, 8.0) == 50_000_000_000
    # 1 ball, 1 camera, 8 Mbps (= 1 MB/s), 1 s -> exactly 1_000_000 bytes
    assert estimate_session_bytes(1, 1, 8.0, 1.0) == 1_000_000


def test_estimate_session_bytes_zero_is_zero() -> None:
    assert estimate_session_bytes(0, 4, 25.0, 8.0) == 0


@pytest.mark.parametrize(
    ("balls", "cameras", "bitrate_mbps", "seconds_per_ball"),
    [(-1, 4, 25.0, 8.0), (500, -1, 25.0, 8.0), (500, 4, -0.1, 8.0), (500, 4, 25.0, -0.1)],
)
def test_estimate_session_bytes_rejects_negative(
    balls: int, cameras: int, bitrate_mbps: float, seconds_per_ball: float
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        estimate_session_bytes(balls, cameras, bitrate_mbps, seconds_per_ball)


# --- run_health_check -------------------------------------------------------


def test_all_green_report() -> None:
    report = run_health_check(_inputs())
    assert report.passed is True
    assert all(result.passed for result in report.results)
    assert [result.name for result in report.results] == [
        "feed:C1",
        "fps:C1",
        "exposure:C1",
        "feed:C2",
        "fps:C2",
        "exposure:C2",
        "disk",
        "sync",
    ]


def test_covered_lens_reports_that_camera_feed() -> None:
    cameras = (_camera("C1", mean_brightness=1.0), _camera("C2"))
    assert _failed_names(_inputs(cameras)) == ["feed:C1", "exposure:C1"]


def test_frozen_feed_reports_that_camera_feed() -> None:
    cameras = (_camera("C1"), _camera("C2", frames_delta=0))
    assert _failed_names(_inputs(cameras)) == ["feed:C2"]


def test_throttled_fps_reports_that_camera_fps() -> None:
    cameras = (_camera("C1", measured_fps=120.0), _camera("C2"))
    assert _failed_names(_inputs(cameras)) == ["fps:C1"]


def test_bad_exposure_reports_that_camera_exposure() -> None:
    cameras = (_camera("C1"), _camera("C2", mean_brightness=250.0))
    assert _failed_names(_inputs(cameras)) == ["exposure:C2"]


def test_full_disk_reports_disk() -> None:
    assert _failed_names(_inputs(free_disk_bytes=1_000)) == ["disk"]


def test_desync_reports_sync() -> None:
    assert _failed_names(_inputs(max_sync_offset_ms=50.0)) == ["sync"]


def test_custom_thresholds_are_applied() -> None:
    inputs = _inputs(free_disk_bytes=10_000_000_000, expected_session_bytes=10_000_000_000)
    assert _failed_names(inputs) == ["disk"]  # default headroom 1.5
    relaxed = run_health_check(inputs, Thresholds(disk_headroom=1.0))
    assert relaxed.passed is True
