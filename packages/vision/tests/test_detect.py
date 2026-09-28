"""Foundation tests for the detection scaffold (US-F2/F3)."""

import dataclasses

import pytest
from cricai_data.enums import LabelClass
from cricai_vision.detect import Detection, DetectionError, FakeDetectionProvider
from cricai_vision.track import TrackingWindow


def test_detection_rejects_out_of_range_fields() -> None:
    with pytest.raises(DetectionError, match="cx"):
        Detection(0, 0.0, LabelClass.BALL, 1.5, 0.5, 0.02, 0.02, 0.9)
    with pytest.raises(DetectionError, match="score"):
        Detection(0, 0.0, LabelClass.BALL, 0.5, 0.5, 0.02, 0.02, float("nan"))


def test_fake_provider_is_deterministic() -> None:
    kwargs = {"start_ms": 1000.0, "end_ms": 2000.0, "fps": 50.0}
    first = FakeDetectionProvider(seed=3, dropout=0.2, decoys=1).detect(**kwargs)
    second = FakeDetectionProvider(seed=3, dropout=0.2, decoys=1).detect(**kwargs)
    assert first == second


def test_fake_provider_emits_ball_flight_and_statics_per_frame() -> None:
    detections = FakeDetectionProvider().detect(start_ms=0.0, end_ms=1000.0, fps=10.0)
    frames = sorted({d.frame_no for d in detections})
    assert frames == list(range(11))
    balls = [d for d in detections if d.label is LabelClass.BALL]
    stumps = [d for d in detections if d.label is LabelClass.STUMPS]
    bats = [d for d in detections if d.label is LabelClass.BAT]
    assert len(balls) == 11  # no dropout by default
    assert len(stumps) == 11
    assert len(bats) == 11
    # Ball sweeps left to right monotonically.
    xs = [d.cx for d in sorted(balls, key=lambda d: d.frame_no)]
    assert xs == sorted(xs)
    assert xs[0] == pytest.approx(0.1)
    assert xs[-1] == pytest.approx(0.9)


def test_fake_provider_bounce_vertex_is_lowest_point() -> None:
    provider = FakeDetectionProvider(bounce_at=0.5)
    detections = provider.detect(start_ms=0.0, end_ms=1000.0, fps=20.0)
    balls = sorted((d for d in detections if d.label is LabelClass.BALL), key=lambda d: d.frame_no)
    lowest = max(balls, key=lambda d: d.cy)  # image y grows downward
    mid = balls[len(balls) // 2]
    assert lowest.frame_no == mid.frame_no
    assert lowest.cy == pytest.approx(0.8)


def test_fake_provider_dropout_removes_only_ball_detections() -> None:
    provider = FakeDetectionProvider(seed=11, dropout=0.5)
    detections = provider.detect(start_ms=0.0, end_ms=2000.0, fps=25.0)
    balls = [d for d in detections if d.label is LabelClass.BALL]
    stumps = [d for d in detections if d.label is LabelClass.STUMPS]
    assert len(stumps) == 51
    assert 0 < len(balls) < 51


def test_fake_provider_decoys_sit_low_in_frame_every_frame() -> None:
    provider = FakeDetectionProvider(decoys=2)
    detections = provider.detect(start_ms=0.0, end_ms=400.0, fps=10.0)
    decoys = [d for d in detections if d.label is LabelClass.BALL and d.cy == 0.95]
    assert len(decoys) == 2 * 5
    assert {d.cx for d in decoys} == {0.15, 0.20}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"dropout": 1.0}, "dropout"),
        ({"dropout": -0.1}, "dropout"),
        ({"bounce_at": 0.0}, "bounce_at"),
        ({"bounce_at": 1.0}, "bounce_at"),
        ({"decoys": -1}, "decoys"),
    ],
)
def test_fake_provider_rejects_bad_construction(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(DetectionError, match=match):
        FakeDetectionProvider(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "fps", "match"),
    [
        (1000.0, 1000.0, 30.0, "empty window"),
        (2000.0, 1000.0, 30.0, "empty window"),
        (0.0, 1000.0, 0.0, "fps"),
        (0.0, 1000.0, -25.0, "fps"),
    ],
)
def test_fake_provider_rejects_bad_windows(
    start_ms: float, end_ms: float, fps: float, match: str
) -> None:
    with pytest.raises(DetectionError, match=match):
        FakeDetectionProvider().detect(start_ms=start_ms, end_ms=end_ms, fps=fps)


def test_frame_grid_matches_tracking_window_at_half_integer_start() -> None:
    """start_ms=4010 at fps=50 lands on slot 200.5: per-frame rounding used to
    emit duplicate/skipped frame numbers (round-half-to-even) and desync the
    provider from TrackingWindow's grid (review detect.py:100)."""
    window = TrackingWindow(start_ms=4010.0, end_ms=4130.0, fps=50.0, width=1920, height=1080)
    detections = FakeDetectionProvider().detect(start_ms=4010.0, end_ms=4130.0, fps=50.0)
    assert sorted({d.frame_no for d in detections}) == list(
        range(window.first_frame, window.last_frame + 1)
    )


def test_detections_are_frozen() -> None:
    detection = Detection(0, 0.0, LabelClass.BALL, 0.5, 0.5, 0.02, 0.02, 0.9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        detection.cx = 0.6  # type: ignore[misc]


def test_single_frame_window_uses_start_position() -> None:
    detections = FakeDetectionProvider().detect(start_ms=0.0, end_ms=10.0, fps=50.0)
    balls = [d for d in detections if d.label is LabelClass.BALL]
    assert [d.frame_no for d in balls] == [0]
    assert balls[0].cx == pytest.approx(0.1)
