"""US-F3 tracker tests: Kalman association, gap bridging, segments, identity.

Synthetic trajectories only: :class:`~cricai_vision.detect.FakeDetectionProvider`
scenes plus hand-built paths with injected dropouts/decoys — never real models.
"""

import itertools
import math
from dataclasses import replace

import pytest
from cricai_data.enums import LabelClass
from cricai_vision.detect import Detection, FakeDetectionProvider
from cricai_vision.extrinsics import PlaneCalibration
from cricai_vision.track import (
    MAX_BRIDGE_FRAMES,
    TRACKER_VERSION,
    TrackerConfig,
    TrackError,
    TrackingWindow,
    TrackPoint,
    _segment_confidence,
    track_ball,
)

WIDTH = 1920
HEIGHT = 1080

#: 100 fps -> frame_ms == 10.0 exactly, so hand-built grids are penny-perfect.
FPS = 100.0


def _window(n_frames: int, *, start_ms: float = 0.0) -> TrackingWindow:
    return TrackingWindow(
        start_ms=start_ms,
        end_ms=start_ms + (n_frames - 1) * 10.0,
        fps=FPS,
        width=WIDTH,
        height=HEIGHT,
    )


def _ball(frame_no: int, x_px: float, y_px: float, *, score: float = 0.9) -> Detection:
    return Detection(
        frame_no=frame_no,
        ts_ms=frame_no * 10.0,
        label=LabelClass.BALL,
        cx=x_px / WIDTH,
        cy=y_px / HEIGHT,
        w=0.02,
        h=0.02,
        score=score,
    )


def _path(frames: list[int], positions: dict[int, tuple[float, float]]) -> list[Detection]:
    return [_ball(f, *positions[f]) for f in frames]


def _linear(frames: list[int], x0: float, dx: float, y0: float, dy: float) -> list[Detection]:
    return [_ball(f, x0 + dx * f, y0 + dy * f) for f in frames]


def _fake_balls(provider: FakeDetectionProvider, **kwargs: float) -> list[Detection]:
    return [d for d in provider.detect(**kwargs) if d.label is LabelClass.BALL]


def _expected_fake_ball_px(
    provider: FakeDetectionProvider, index: int, n_frames: int
) -> tuple[float, float]:
    """The fake provider's parametric flight position, converted to pixels."""
    progress = index / (n_frames - 1)
    cx = 0.1 + 0.8 * progress
    if progress <= provider.bounce_at:
        cy = 0.3 + 0.5 * (progress / provider.bounce_at)
    else:
        cy = 0.8 - 0.4 * ((progress - provider.bounce_at) / (1.0 - provider.bounce_at))
    return (cx * WIDTH, cy * HEIGHT)


def test_tracker_version_is_pinned_provenance() -> None:
    assert TRACKER_VERSION == "cv-kalman-1"
    assert MAX_BRIDGE_FRAMES == 5


# --- clean synthetic flight -------------------------------------------------


def test_clean_flight_full_coverage_and_bounce_split() -> None:
    provider = FakeDetectionProvider()
    window = TrackingWindow(start_ms=0.0, end_ms=4000.0, fps=30.0, width=WIDTH, height=HEIGHT)
    result = track_ball(_fake_balls(provider, start_ms=0.0, end_ms=4000.0, fps=30.0), window)
    assert result.coverage == 1.0
    assert len(result.points) == window.n_frames
    assert all(not p.bridged for p in result.points)
    assert result.confidence == pytest.approx(0.9)  # coverage 1.0 x score 0.9
    assert not result.identity_risk
    assert not result.long_gap
    assert not result.pitch_mapped
    # Bounce split at the vertical-velocity sign change; the vertex (lowest
    # point, max y) ends pre_bounce and the next point opens post_bounce.
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce"]
    vertex = max(result.points, key=lambda p: p.px_y)
    pre, post = result.segments
    assert pre.start_ms == result.points[0].ts_ms
    assert pre.end_ms == vertex.ts_ms
    assert post.start_ms > vertex.ts_ms
    assert post.end_ms == result.points[-1].ts_ms
    assert pre.confidence == pytest.approx(0.9)
    assert post.confidence == pytest.approx(0.9)


def test_track_is_deterministic() -> None:
    window = TrackingWindow(start_ms=0.0, end_ms=2000.0, fps=25.0, width=WIDTH, height=HEIGHT)
    balls = _fake_balls(
        FakeDetectionProvider(seed=11, dropout=0.3, decoys=1),
        start_ms=0.0,
        end_ms=2000.0,
        fps=25.0,
    )
    assert track_ball(balls, window) == track_ball(balls, window)


# --- decoy / identity stress ------------------------------------------------


def test_multi_ball_decoy_stress_zero_identity_errors() -> None:
    """ST (US-F3 AC): with two decoy balls in the net, every non-bridged point
    is exactly the delivered ball — the tracker never jumps identity."""
    provider = FakeDetectionProvider(decoys=2)
    window = TrackingWindow(start_ms=0.0, end_ms=4000.0, fps=30.0, width=WIDTH, height=HEIGHT)
    result = track_ball(_fake_balls(provider, start_ms=0.0, end_ms=4000.0, fps=30.0), window)
    assert result.coverage == 1.0
    for index, point in enumerate(result.points):
        expected = _expected_fake_ball_px(provider, index, window.n_frames)
        assert point.px_x == pytest.approx(expected[0])
        assert point.px_y == pytest.approx(expected[1])
    assert not result.identity_risk  # net decoys sit far from the flight


def test_decoys_with_dropout_still_never_steal_identity() -> None:
    provider = FakeDetectionProvider(seed=11, dropout=0.3, decoys=2)
    window = TrackingWindow(start_ms=0.0, end_ms=2000.0, fps=25.0, width=WIDTH, height=HEIGHT)
    balls = _fake_balls(provider, start_ms=0.0, end_ms=2000.0, fps=25.0)
    flight_px = {d.frame_no: (d.cx * WIDTH, d.cy * HEIGHT) for d in balls if d.cy != 0.95}
    result = track_ball(balls, window)
    for point in result.points:
        if point.bridged:
            continue
        expected = flight_px[point.frame_no]
        assert (point.px_x, point.px_y) == pytest.approx(expected)
    assert not result.identity_risk


def test_persistent_near_track_second_ball_flags_identity_risk() -> None:
    """A second ball inside the ambiguity radius for many frames is genuinely
    ambiguous: flagged, yet still never accepted (outside the gate)."""
    frames = list(range(21))
    detections = _linear(frames, x0=100.0, dx=20.0, y0=500.0, dy=0.0)
    detections += [_ball(f, 300.0, 590.0, score=0.8) for f in frames]  # 90 px off-path
    result = track_ball(detections, _window(21))
    assert result.identity_risk
    assert all(p.px_y == pytest.approx(500.0) for p in result.points if not p.bridged)


def test_brief_ambiguity_below_persistence_is_not_flagged() -> None:
    frames = list(range(21))
    detections = _linear(frames, x0=100.0, dx=20.0, y0=500.0, dy=0.0)
    detections += [_ball(f, 300.0, 590.0, score=0.8) for f in (9, 10)]  # 2 < 3 frames
    result = track_ball(detections, _window(21))
    assert not result.identity_risk


def test_static_decoys_only_yield_empty_track() -> None:
    """No moving ball in the window: an honest empty result, never a decoy track."""
    frames = list(range(11))
    detections = [_ball(f, 300.0, 900.0, score=0.8) for f in frames]
    detections += [_ball(f, 400.0, 900.0, score=0.8) for f in frames]
    result = track_ball(detections, _window(11))
    assert result.points == ()
    assert result.segments == ()
    assert result.coverage == 0.0
    assert result.confidence == 0.0
    assert not result.identity_risk
    assert not result.long_gap


def test_no_detections_yield_empty_track() -> None:
    result = track_ball([], _window(11))
    assert result.points == ()
    assert result.coverage == 0.0


def test_single_detection_is_below_min_path() -> None:
    result = track_ball([_ball(0, 500.0, 500.0)], _window(11))
    assert result.points == ()


# --- gap bridging -----------------------------------------------------------


def test_gap_of_five_frames_is_bridged_by_prediction() -> None:
    """Occlusion <= 5 frames is filled (bridged=true, score 0) on the grid."""
    frames = [0, 1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    detections = _linear(frames, x0=100.0, dx=8.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(21))
    assert result.coverage == 1.0
    assert not result.long_gap
    bridged = [p for p in result.points if p.bridged]
    assert [p.frame_no for p in bridged] == [6, 7, 8, 9, 10]
    for point in bridged:
        assert point.score == 0.0
        assert point.ts_ms == point.frame_no * 10.0
        assert point.px_x == pytest.approx(100.0 + 8.0 * point.frame_no)
        assert point.px_y == pytest.approx(200.0 + 15.0 * point.frame_no)
    # Detected-frame density honestly discounts the bridged stretch.
    (segment,) = result.segments
    assert segment.kind == "pre_bounce"
    assert segment.confidence == pytest.approx((16 / 21) * 0.9)


def test_gap_of_six_frames_is_never_hallucinated() -> None:
    """US-F3 AC: longer gaps are flagged and split segments, not filled."""
    frames = [0, 1, 2, 3, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    detections = _linear(frames, x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(21))
    assert result.long_gap
    assert not any(p.bridged for p in result.points)
    assert [p.frame_no for p in result.points] == frames
    assert result.coverage == pytest.approx(15 / 21)
    # Same phase on both sides of the gap -> two pre_bounce segments.
    assert [s.kind for s in result.segments] == ["pre_bounce", "pre_bounce"]
    first, second = result.segments
    assert first.end_ms == 50.0
    assert second.start_ms == 120.0


def test_bounce_inside_long_gap_is_inferred_from_ascent() -> None:
    """A run that resumes ascending while still pre_bounce opens post_bounce."""
    descend = _linear(list(range(11)), x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    ascend = [_ball(f, 100.0 + 20.0 * f, 380.0 - 10.0 * (f - 18)) for f in range(18, 29)]
    result = track_ball(descend + ascend, _window(29))
    assert result.long_gap
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce"]
    assert result.segments[1].start_ms == 180.0


def test_phase_persists_across_long_gap_after_bounce() -> None:
    """post_bounce before a long gap stays post_bounce when the track resumes."""
    positions: dict[int, tuple[float, float]] = {}
    for f in range(11):  # descend to the vertex at frame 10
        positions[f] = (100.0 + 20.0 * f, 200.0 + 15.0 * f)
    for f in range(11, 16):  # ascend after the bounce
        positions[f] = (100.0 + 20.0 * f, 350.0 - 12.0 * (f - 10))
    for f in range(23, 29):  # long gap, then keep ascending
        positions[f] = (100.0 + 20.0 * f, 290.0 - 12.0 * (f - 15))
    frames = sorted(positions)
    result = track_ball(_path(frames, positions), _window(29))
    assert result.long_gap
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce", "post_bounce"]


def test_single_point_run_after_long_gap_forms_own_segment() -> None:
    frames = [*range(11), 20]
    detections = _linear(frames, x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(21))
    assert result.long_gap
    last = result.segments[-1]
    assert last.start_ms == last.end_ms == 200.0
    assert last.confidence == pytest.approx(0.9)


# --- segments: bounce + contact ---------------------------------------------


def test_bounce_and_contact_reversal_yield_three_segments() -> None:
    """pre_bounce -> post_bounce at the vy sign flip -> post_contact after the
    horizontal direction reversal near the batter."""
    positions: dict[int, tuple[float, float]] = {}
    for f in range(16):  # descend
        positions[f] = (100.0 + 20.0 * f, 200.0 + 15.0 * f)
    for f in range(16, 23):  # rise toward the batter
        positions[f] = (100.0 + 20.0 * f, 425.0 - 12.0 * (f - 15))
    for f in range(23, 31):  # struck: x reverses
        positions[f] = (540.0 - 25.0 * (f - 22), 341.0 - 5.0 * (f - 22))
    frames = sorted(positions)
    result = track_ball(_path(frames, positions), _window(31))
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce", "post_contact"]
    pre, post_bounce, post_contact = result.segments
    assert pre.end_ms == 150.0  # vertex point stays in pre_bounce
    assert post_bounce.start_ms == 160.0
    assert post_bounce.end_ms == 220.0
    assert post_contact.start_ms == 230.0
    assert result.coverage == 1.0


def test_full_toss_contact_fires_without_a_bounce() -> None:
    positions: dict[int, tuple[float, float]] = {}
    for f in range(11):
        positions[f] = (100.0 + 20.0 * f, 300.0 + 2.0 * f)
    for f in range(11, 21):
        positions[f] = (300.0 - 20.0 * (f - 10), 320.0 + 2.0 * f)
    frames = sorted(positions)
    result = track_ball(_path(frames, positions), _window(21))
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_contact"]


def test_near_zero_delta_straddling_the_impact_still_opens_post_bounce() -> None:
    """When the frame grid straddles the impact, the spanning delta can cancel
    to ~0 px; the flip must still fire on the next significant ascent instead
    of locking the track in pre_bounce forever (review track.py:517)."""
    positions: dict[int, tuple[float, float]] = {}
    for f in range(11):  # descend at +15 px/frame
        positions[f] = (100.0 + 20.0 * f, 200.0 + 15.0 * f)
    positions[11] = (320.0, 350.3)  # impact splits the interval: dy = +0.3
    for f in range(12, 21):  # rebound at -10 px/frame
        positions[f] = (100.0 + 20.0 * f, 350.3 - 10.0 * (f - 11))
    frames = sorted(positions)
    result = track_ball(_path(frames, positions), _window(21))
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce"]
    assert result.segments[1].start_ms == 120.0  # first significant ascent point


def test_bounce_inside_bridged_gap_still_opens_post_bounce() -> None:
    """A bounce hidden inside a bridged (<= 5 frame) gap with near-symmetric
    entry/exit heights yields ~0 deltas across the fill; the following ascent
    must still open post_bounce (review track.py:517)."""
    descend = _linear(list(range(11)), x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    ascend = [_ball(f, 100.0 + 20.0 * f, 350.0 - 10.0 * (f - 14)) for f in range(14, 25)]
    result = track_ball(descend + ascend, _window(25))
    assert not result.long_gap  # 3 missing frames: bridged, not split
    assert [s.kind for s in result.segments] == ["pre_bounce", "post_bounce"]
    assert result.segments[1].start_ms == 150.0


def test_subthreshold_vertical_jitter_never_opens_post_bounce() -> None:
    """Pixel jitter within min_bounce_speed_px on a flat trajectory must never
    fabricate a bounce boundary for US-F4 (review test_track.py:465)."""
    detections = [_ball(f, 100.0 + 20.0 * f, 500.0 + (0.3 if f % 2 else 0.0)) for f in range(21)]
    result = track_ball(detections, _window(21))
    assert [s.kind for s in result.segments] == ["pre_bounce"]


def test_ascent_without_prior_descent_stays_pre_bounce() -> None:
    """The flip needs a significant descending delta first: a track that only
    rises (after a sub-threshold first step) never opens post_bounce."""
    ys = [500.0, 499.7] + [499.7 - 8.0 * i for i in range(1, 20)]
    detections = [_ball(f, 100.0 + 20.0 * f, ys[f]) for f in range(len(ys))]
    result = track_ball(detections, _window(len(ys)))
    assert [s.kind for s in result.segments] == ["pre_bounce"]


def test_jitter_blips_do_not_fake_a_contact() -> None:
    """A single sub-threshold backward step (or a flat one) never reverses."""
    xs = [100.0]
    for dx in (20.0, 20.0, 0.0, 20.0, -8.0, 20.0, 20.0, 20.0, 20.0, 20.0):
        xs.append(xs[-1] + dx)
    detections = [_ball(f, xs[f], 200.0 + 15.0 * f) for f in range(len(xs))]
    result = track_ball(detections, _window(len(xs)))
    assert [s.kind for s in result.segments] == ["pre_bounce"]


def test_all_bridged_sliver_has_zero_confidence() -> None:
    bridged = [
        TrackPoint(frame_no=f, ts_ms=f * 10.0, px_x=0.0, px_y=0.0, score=0.0, bridged=True)
        for f in (3, 4)
    ]
    assert _segment_confidence(bridged) == 0.0


# --- pitch enrichment -------------------------------------------------------

#: Millimeter-per-pixel style scale map: pitch = px / 100 (w == 1 everywhere).
SCALE_CAL = PlaneCalibration(
    matrix=((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 1.0)),
    rms_px=0.5,
    rms_m=0.01,
    n_landmarks=8,
)


def test_homography_enriches_points_with_pitch_xy() -> None:
    detections = _linear(list(range(11)), x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(11), calibration=SCALE_CAL)
    assert result.pitch_mapped
    for point in result.points:
        assert point.pitch_x == pytest.approx(point.px_x / 100.0)
        assert point.pitch_y == pytest.approx(point.px_y / 100.0)


def test_point_on_mapping_horizon_keeps_null_pitch() -> None:
    horizon_cal = PlaneCalibration(
        matrix=((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (-0.01, 0.0, 1.0)),
        rms_px=0.5,
        rms_m=0.01,
        n_landmarks=8,
    )
    detections = _linear(list(range(11)), x0=60.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(11), calibration=horizon_cal)
    assert result.pitch_mapped
    on_horizon = [p for p in result.points if p.px_x == pytest.approx(100.0)]
    assert on_horizon and on_horizon[0].pitch_x is None
    mapped = [p for p in result.points if p.px_x != pytest.approx(100.0)]
    assert all(p.pitch_x is not None for p in mapped)


def test_without_homography_points_stay_pixel_only() -> None:
    detections = _linear(list(range(11)), x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(11))
    assert not result.pitch_mapped
    assert all(p.pitch_x is None and p.pitch_y is None for p in result.points)


def test_empty_result_reports_whether_mapping_was_available() -> None:
    assert track_ball([], _window(11), calibration=SCALE_CAL).pitch_mapped
    assert not track_ball([], _window(11)).pitch_mapped


# --- payload contract -------------------------------------------------------


def test_payload_matches_pinned_contract() -> None:
    detections = _linear([0, 1, 2, 3, 4, 5, 9, 10], x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(11))
    payload = result.to_payload()
    assert set(payload) == {"points", "segments", "flags"}
    assert payload["flags"] == {
        "identity_risk": False,
        "long_gap": False,
        "pitch_mapped": False,
    }
    for point in payload["points"]:
        assert set(point) == {"frame_no", "ts_ms", "px_x", "px_y", "score", "bridged"}
    for segment in payload["segments"]:
        assert set(segment) == {"kind", "start_ms", "end_ms", "confidence"}
        assert segment["kind"] in {"pre_bounce", "post_bounce", "post_contact"}
    assert [p["bridged"] for p in payload["points"]].count(True) == 3  # frames 6-8


def test_payload_includes_pitch_keys_only_when_mapped() -> None:
    detections = _linear(list(range(11)), x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    mapped = track_ball(detections, _window(11), calibration=SCALE_CAL).to_payload()
    for point in mapped["points"]:
        assert set(point) == {
            "frame_no",
            "ts_ms",
            "px_x",
            "px_y",
            "score",
            "bridged",
            "pitch_x",
            "pitch_y",
        }


# --- window math ------------------------------------------------------------


def test_window_frame_grid_mirrors_fake_provider() -> None:
    window = TrackingWindow(
        start_ms=10_000.0, end_ms=14_000.0, fps=30.0, width=WIDTH, height=HEIGHT
    )
    balls = _fake_balls(FakeDetectionProvider(), start_ms=10_000.0, end_ms=14_000.0, fps=30.0)
    assert len(balls) == window.n_frames
    assert balls[0].frame_no == window.first_frame
    assert balls[-1].frame_no == window.last_frame
    assert window.frame_ts_ms(window.first_frame) == pytest.approx(10_000.0)
    assert window.frame_ts_ms(window.last_frame) == pytest.approx(
        10_000.0 + (window.n_frames - 1) * window.frame_ms
    )


def test_half_integer_start_boundary_matches_provider_grid() -> None:
    """fps=50 puts start_ms=4010 on frame slot 200.5: the provider must emit
    the window's grid (round(start) + index), or a clean flight gains phantom
    ambiguity/bridging — or fails outright (review detect.py:100)."""
    window = TrackingWindow(start_ms=4010.0, end_ms=8010.0, fps=50.0, width=WIDTH, height=HEIGHT)
    balls = _fake_balls(FakeDetectionProvider(), start_ms=4010.0, end_ms=8010.0, fps=50.0)
    assert [d.frame_no for d in balls] == list(range(window.first_frame, window.last_frame + 1))
    result = track_ball(balls, window)
    assert result.coverage == 1.0
    assert not result.identity_risk
    assert all(not p.bridged for p in result.points)
    # The other window parity used to round the last detection past the edge.
    other = TrackingWindow(start_ms=4010.0, end_ms=4990.0, fps=50.0, width=WIDTH, height=HEIGHT)
    other_balls = _fake_balls(FakeDetectionProvider(), start_ms=4010.0, end_ms=4990.0, fps=50.0)
    assert track_ball(other_balls, other).coverage == 1.0  # never a TrackError


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"start_ms": 100.0, "end_ms": 100.0}, "empty window"),
        ({"start_ms": 200.0, "end_ms": 100.0}, "empty window"),
        ({"fps": 0.0}, "fps"),
        ({"fps": -30.0}, "fps"),
        ({"width": 0}, "frame size"),
        ({"height": -1}, "frame size"),
    ],
)
def test_window_rejects_bad_construction(kwargs: dict[str, float], match: str) -> None:
    base: dict[str, float | int] = {
        "start_ms": 0.0,
        "end_ms": 1000.0,
        "fps": 50.0,
        "width": WIDTH,
        "height": HEIGHT,
    }
    with pytest.raises(TrackError, match=match):
        TrackingWindow(**{**base, **kwargs})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"gate_px": 0.0}, "gate_px"),
        ({"ambiguity_radius_px": -1.0}, "ambiguity_radius_px"),
        ({"measurement_noise_px": 0.0}, "measurement_noise_px"),
        ({"process_noise_px": -2.0}, "process_noise_px"),
        ({"min_reversal_px": 0.0}, "min_reversal_px"),
        ({"reacquire_gate_factor": 0.5}, "reacquire_gate_factor"),
        ({"identity_persist_frames": 0}, "identity_persist_frames"),
        ({"min_path_px": -1.0}, "min_path_px"),
        ({"min_bounce_speed_px": -0.1}, "min_bounce_speed_px"),
    ],
)
def test_config_rejects_bad_values(kwargs: dict[str, float], match: str) -> None:
    with pytest.raises(TrackError, match=match):
        TrackerConfig(**kwargs)  # type: ignore[arg-type]


def test_rejects_non_ball_detections() -> None:
    bat = Detection(0, 0.0, LabelClass.BAT, 0.5, 0.5, 0.05, 0.12, 0.85)
    with pytest.raises(TrackError, match="ball detections only"):
        track_ball([bat], _window(11))


def test_rejects_detection_outside_window_frames() -> None:
    with pytest.raises(TrackError, match="outside window"):
        track_ball([_ball(30, 500.0, 500.0)], _window(11))
    late = replace(_ball(5, 500.0, 500.0), frame_no=-1)
    with pytest.raises(TrackError, match="outside window"):
        track_ball([late], _window(11))


def test_custom_config_is_honored() -> None:
    """A tiny ambiguity radius turns the flagged case back off (same data)."""
    frames = list(range(21))
    detections = _linear(frames, x0=100.0, dx=20.0, y0=500.0, dy=0.0)
    detections += [_ball(f, 300.0, 590.0, score=0.8) for f in frames]
    tight = TrackerConfig(ambiguity_radius_px=85.0)
    assert not track_ball(detections, _window(21), config=tight).identity_risk


def test_dropout_gaps_are_bridged_or_split_never_faked() -> None:
    """Every missing stretch between detections is either fully bridged
    (<= 5 frames) or left empty (> 5), and non-bridged points are detections."""
    provider = FakeDetectionProvider(seed=11, dropout=0.3)
    window = TrackingWindow(start_ms=0.0, end_ms=2000.0, fps=25.0, width=WIDTH, height=HEIGHT)
    balls = _fake_balls(provider, start_ms=0.0, end_ms=2000.0, fps=25.0)
    detected_frames = {d.frame_no for d in balls}
    result = track_ball(balls, window)
    tracked = sorted(p.frame_no for p in result.points)
    for prev, nxt in itertools.pairwise(tracked):
        assert nxt - prev == 1 or nxt - prev - 1 > MAX_BRIDGE_FRAMES
    for point in result.points:
        if not point.bridged:
            assert point.frame_no in detected_frames
        else:
            assert point.frame_no not in detected_frames


def test_reacquires_after_long_gap_within_widened_gate() -> None:
    """The gate widens with consecutive misses so the track resumes after a
    long occlusion instead of dying — while the gap itself stays unfilled."""
    frames = [*range(11), *range(18, 29)]
    detections = _linear(frames, x0=100.0, dx=20.0, y0=200.0, dy=15.0)
    result = track_ball(detections, _window(29))
    assert result.long_gap
    assert [p.frame_no for p in result.points] == frames
    assert math.isclose(result.coverage, 22 / 29)
