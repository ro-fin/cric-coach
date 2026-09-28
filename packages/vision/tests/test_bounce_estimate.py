"""US-F4 bounce-vertex estimation: refinement, confidence degradation, honesty."""

from typing import Any

import pytest
from cricai_data.enums import LabelClass
from cricai_vision.bounce_estimate import (
    BRIDGED_CONFIDENCE_FACTOR,
    LONG_GAP_CONFIDENCE_FACTOR,
    MIN_POINTS,
    UNREFINED_CONFIDENCE_FACTOR,
    VERTEX_WINDOW_MS,
    BounceResult,
    EstimatedBounce,
    TrackPayloadError,
    estimate_bounce,
    parse_track_payload,
)
from cricai_vision.detect import Detection
from cricai_vision.extrinsics import PlaneCalibration
from cricai_vision.track import TrackingWindow, track_ball

#: Pixel->pitch homography scaling px (500, 400) to a plausible (5.0, 0.2) m.
SCALE_CAL = PlaneCalibration(
    matrix=((0.01, 0.0, 0.0), (0.0, 0.0005, 0.0), (0.0, 0.0, 1.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)

#: Third row maps every pixel to the horizon (w == 0): always unmappable.
HORIZON_CAL = PlaneCalibration(
    matrix=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 0.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)


def _point(
    ts_ms: float,
    px_y: float,
    *,
    px_x: float = 500.0,
    bridged: bool = False,
    frame_no: int | None = None,
    score: float = 0.9,
) -> dict[str, Any]:
    return {
        "frame_no": frame_no if frame_no is not None else round(ts_ms / 30.0),
        "ts_ms": ts_ms,
        "px_x": px_x,
        "px_y": px_y,
        "score": score,
        "bridged": bridged,
    }


def _flight_points(
    *,
    apex_ts: float = 300.0,
    step_ms: float = 30.0,
    pre: int = 6,
    post: int = 6,
    bridged_ts: frozenset[float] = frozenset(),
) -> list[dict[str, Any]]:
    """Descent to the px_y maximum at ``apex_ts``, then ascent (image y grows
    downward); px_x sweeps linearly like a real delivery."""
    return [
        _point(
            apex_ts + k * step_ms,
            400.0 - 8.0 * abs(k),
            px_x=500.0 + 4.0 * k,
            bridged=(apex_ts + k * step_ms) in bridged_ts,
        )
        for k in range(-pre, post + 1)
    ]


def _segments(
    boundary_ms: float = 300.0, *, pre_conf: float = 0.9, post_conf: float = 0.8
) -> list[dict[str, Any]]:
    return [
        {"kind": "pre_bounce", "start_ms": 0.0, "end_ms": boundary_ms, "confidence": pre_conf},
        {
            "kind": "post_bounce",
            "start_ms": boundary_ms,
            "end_ms": boundary_ms + 500.0,
            "confidence": post_conf,
        },
    ]


def _payload(
    points: list[dict[str, Any]] | None = None,
    segments: list[dict[str, Any]] | None = None,
    flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "points": points if points is not None else _flight_points(),
        "segments": segments if segments is not None else _segments(),
        "flags": flags if flags is not None else {"identity_risk": False, "long_gap": False},
    }


# ---------------------------------------------------------------------------
# Vertex location & pitch mapping
# ---------------------------------------------------------------------------


def test_vertex_at_velocity_sign_change_with_pitch_mapping() -> None:
    result = estimate_bounce(_payload(), homography=SCALE_CAL)
    assert result.reason is None
    estimate = result.estimate
    assert estimate is not None
    assert (estimate.ts_ms, estimate.px_x, estimate.px_y) == (300.0, 500.0, 400.0)
    assert estimate.frame_no == 10
    assert estimate.pitch_x == pytest.approx(5.0)
    assert estimate.pitch_y == pytest.approx(0.2)
    assert estimate.confidence == pytest.approx(0.8)  # min(pre 0.9, post 0.8), undegraded


def test_without_homography_estimate_stays_in_pixels() -> None:
    result = estimate_bounce(_payload())
    assert result.estimate is not None
    assert result.estimate.pitch_x is None
    assert result.estimate.pitch_y is None


def test_sign_change_refines_an_offset_segment_boundary() -> None:
    """Boundary 90 ms after the true apex: the sign change wins (US-F4)."""
    result = estimate_bounce(_payload(segments=_segments(390.0)))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 300.0  # the apex, not the boundary
    assert result.estimate.confidence == pytest.approx(0.8)  # refined: no penalty


def test_nearest_of_several_sign_changes_wins() -> None:
    """A pre-pitch bobble makes two sign changes; the boundary-nearest wins."""
    points = [
        _point(200.0, 100.0),
        _point(250.0, 130.0),  # first local maximum (ts 250)
        _point(300.0, 120.0),
        _point(350.0, 140.0),  # second local maximum (ts 350)
        _point(400.0, 110.0),
    ]
    result = estimate_bounce(_payload(points=points, segments=_segments(320.0)))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 350.0  # |350-320| = 30 < |250-320| = 70


def test_unsorted_points_are_sorted_before_analysis() -> None:
    points = list(reversed(_flight_points()))
    result = estimate_bounce(_payload(points=points))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 300.0


def test_parsed_payload_is_accepted_directly_and_deterministic() -> None:
    parsed = parse_track_payload(_payload())
    from_parsed = estimate_bounce(parsed, homography=SCALE_CAL)
    from_raw = estimate_bounce(_payload(), homography=SCALE_CAL)
    assert from_parsed == from_raw
    assert estimate_bounce(_payload(), homography=SCALE_CAL) == from_raw


# ---------------------------------------------------------------------------
# Long-gap phase splits: several same-kind segments (pinned US-F3 behavior)
# ---------------------------------------------------------------------------


def _detection(frame_no: int, px_x: float, px_y: float) -> Detection:
    return Detection(
        frame_no=frame_no,
        ts_ms=frame_no * 10.0,
        label=LabelClass.BALL,
        cx=px_x / 1920.0,
        cy=px_y / 1080.0,
        w=0.02,
        h=0.02,
        score=0.9,
    )


def test_long_gap_split_payload_from_the_tracker_parses_and_estimates() -> None:
    """A >5-frame occlusion during the descent legally splits the pre_bounce
    phase, so the f3 tracker emits TWO pre_bounce segments (pinned behavior);
    the parser and the estimator must accept that payload, not reject it as
    malformed and lose the ball from the pitch map."""
    positions: dict[int, tuple[float, float]] = {}
    for f in range(11):  # descend
        positions[f] = (100.0 + 20.0 * f, 200.0 + 15.0 * f)
    for f in range(18, 26):  # 7-frame occlusion, then keep descending to f=25
        positions[f] = (100.0 + 20.0 * f, 200.0 + 15.0 * f)
    for f in range(26, 34):  # rebound after the bounce at f=25
        positions[f] = (100.0 + 20.0 * f, 575.0 - 12.0 * (f - 25))
    window = TrackingWindow(start_ms=0.0, end_ms=330.0, fps=100.0, width=1920, height=1080)
    result = track_ball([_detection(f, *positions[f]) for f in sorted(positions)], window)
    assert [s.kind for s in result.segments] == ["pre_bounce", "pre_bounce", "post_bounce"]
    assert result.long_gap
    payload = result.to_payload()
    track = parse_track_payload(payload)  # accepted, never "malformed"
    assert len(track.segments_of("pre_bounce")) == 2
    bounce = estimate_bounce(payload)
    assert bounce.estimate is not None
    assert bounce.estimate.ts_ms == 250.0  # the true vertex, after the gap
    assert bounce.estimate.px_y == 575.0


def test_boundary_uses_last_pre_bounce_and_first_post_bounce_segments() -> None:
    """With a split pre_bounce phase the bounce sits between the LAST
    pre_bounce window and the FIRST post_bounce window — a decoy sign change
    near the stale first-segment boundary must not win."""
    points = [
        _point(0.0, 100.0),
        _point(50.0, 130.0),  # pre-gap bobble: a decoy local maximum
        _point(100.0, 120.0),
        _point(400.0, 200.0),
        _point(450.0, 240.0),  # the true bounce vertex
        _point(500.0, 210.0),
    ]
    segments = [
        {"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 100.0, "confidence": 0.9},
        {"kind": "pre_bounce", "start_ms": 400.0, "end_ms": 450.0, "confidence": 0.85},
        {"kind": "post_bounce", "start_ms": 450.0, "end_ms": 500.0, "confidence": 0.8},
    ]
    flags = {"identity_risk": False, "long_gap": True}
    result = estimate_bounce(_payload(points=points, segments=segments, flags=flags))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 450.0  # boundary (450+450)/2, not (100+450)/2
    assert result.estimate.confidence == pytest.approx(0.8 * LONG_GAP_CONFIDENCE_FACTOR)


def test_split_post_bounce_boundary_uses_the_first_post_window() -> None:
    """The symmetric half of the boundary rule: a >5-frame occlusion during
    the rise splits the post_bounce phase, and the bounce sits before the
    FIRST post_bounce window by start_ms — never the last one, and never
    whichever happens to come first in payload order. A decoy sign change
    inside the second window must not steal the vertex
    (audit test_bounce_estimate.py:210)."""
    points = [
        _point(400.0, 200.0),
        _point(450.0, 240.0),  # the true bounce vertex
        _point(500.0, 210.0),  # rising in the first post_bounce window
        _point(800.0, 130.0),  # still rising after the occlusion gap
        _point(850.0, 160.0),  # post-gap bobble: a decoy local maximum
        _point(900.0, 120.0),
    ]
    segments = [  # the split window deliberately listed first: order must not matter
        {"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 450.0, "confidence": 0.9},
        {"kind": "post_bounce", "start_ms": 800.0, "end_ms": 900.0, "confidence": 0.85},
        {"kind": "post_bounce", "start_ms": 450.0, "end_ms": 500.0, "confidence": 0.8},
    ]
    flags = {"identity_risk": False, "long_gap": True}
    result = estimate_bounce(_payload(points=points, segments=segments, flags=flags))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 450.0  # boundary (450+450)/2, not (450+800)/2
    assert result.estimate.px_y == 240.0
    # min(pre 0.9, FIRST post 0.8) with the long-gap penalty; the last window's
    # 0.85 confidence never enters, and the vertex is a refined sign change.
    assert result.estimate.confidence == pytest.approx(0.8 * LONG_GAP_CONFIDENCE_FACTOR)


# ---------------------------------------------------------------------------
# Confidence degradation
# ---------------------------------------------------------------------------


def test_no_sign_change_falls_back_to_boundary_with_degraded_confidence() -> None:
    """Monotone descent through the window: boundary point, unrefined penalty."""
    points = [_point(ts, 100.0 + ts / 10.0) for ts in (180.0, 240.0, 300.0, 360.0, 420.0)]
    result = estimate_bounce(_payload(points=points))
    assert result.estimate is not None
    assert result.estimate.ts_ms == 300.0  # nearest the boundary
    assert result.estimate.confidence == pytest.approx(0.8 * UNREFINED_CONFIDENCE_FACTOR)


def test_bridged_points_near_the_vertex_degrade_confidence() -> None:
    one = estimate_bounce(_payload(points=_flight_points(bridged_ts=frozenset({270.0}))))
    two = estimate_bounce(_payload(points=_flight_points(bridged_ts=frozenset({270.0, 330.0}))))
    assert one.estimate is not None and two.estimate is not None
    assert one.estimate.confidence == pytest.approx(0.8 * BRIDGED_CONFIDENCE_FACTOR)
    assert two.estimate.confidence == pytest.approx(0.8 * BRIDGED_CONFIDENCE_FACTOR**2)
    assert two.estimate.confidence < one.estimate.confidence


def test_bridged_points_far_from_the_vertex_do_not_degrade() -> None:
    far_ts = 300.0 + VERTEX_WINDOW_MS + 30.0  # outside the vertex window
    result = estimate_bounce(_payload(points=_flight_points(bridged_ts=frozenset({far_ts}))))
    assert result.estimate is not None
    assert result.estimate.confidence == pytest.approx(0.8)


def test_long_gap_flag_degrades_confidence() -> None:
    result = estimate_bounce(_payload(flags={"identity_risk": True, "long_gap": True}))
    assert result.estimate is not None
    assert result.estimate.confidence == pytest.approx(0.8 * LONG_GAP_CONFIDENCE_FACTOR)


# ---------------------------------------------------------------------------
# Honest no-bounce outcomes (never fabricate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["pre_bounce", "post_bounce"])
def test_missing_bounce_segment_returns_reason_not_a_guess(missing: str) -> None:
    segments = [s for s in _segments() if s["kind"] != missing]
    segments.append({"kind": "post_contact", "start_ms": 800.0, "end_ms": 900.0, "confidence": 0.9})
    result = estimate_bounce(_payload(segments=segments))
    assert result.estimate is None
    assert result.reason is not None and "full toss or tracking failure" in result.reason


def test_too_few_points_returns_reason() -> None:
    points = _flight_points()[: MIN_POINTS - 1]
    result = estimate_bounce(_payload(points=points))
    assert result.estimate is None
    assert result.reason is not None and "too few tracked points" in result.reason


def test_no_points_near_the_boundary_returns_reason() -> None:
    result = estimate_bounce(_payload(segments=_segments(2000.0)))
    assert result.estimate is None
    assert result.reason is not None and "no tracked points within" in result.reason


def test_unmappable_vertex_pixel_returns_reason() -> None:
    result = estimate_bounce(_payload(), homography=HORIZON_CAL)
    assert result.estimate is None
    assert result.reason is not None and "unmappable" in result.reason


def test_result_invariant_exactly_one_of_estimate_and_reason() -> None:
    estimate = EstimatedBounce(frame_no=1, ts_ms=1.0, px_x=1.0, px_y=1.0, confidence=0.5)
    with pytest.raises(ValueError, match="exactly one"):
        BounceResult(estimate, "also a reason")
    with pytest.raises(ValueError, match="exactly one"):
        BounceResult(None, None)


# ---------------------------------------------------------------------------
# Payload validation (pinned US-F3 contract)
# ---------------------------------------------------------------------------


def test_parse_reads_flags_and_sorts_points() -> None:
    payload = _payload(flags={"identity_risk": True, "long_gap": False})
    payload["points"] = list(reversed(payload["points"]))
    track = parse_track_payload(payload)
    assert track.identity_risk is True
    assert track.long_gap is False
    assert [p.ts_ms for p in track.points] == sorted(p.ts_ms for p in track.points)
    assert track.segments_of("post_contact") == ()


def test_parse_defaults_missing_flags_and_bridged() -> None:
    payload = _payload()
    del payload["flags"]
    for point in payload["points"]:
        del point["bridged"]
    track = parse_track_payload(payload)
    assert track.identity_risk is False
    assert track.long_gap is False
    assert all(point.bridged is False for point in track.points)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda _p: "not a dict", "must be an object"),
        (lambda p: p.update(points={"not": "a list"}) or p, "'points' must be a list"),
        (lambda p: p.update(points=["not a dict", *p["points"][1:]]) or p, r"points\[0\]"),
        (
            lambda p: p["points"][0].update(frame_no=True) or p,
            "frame_no must be an integer",
        ),
        (
            lambda p: p["points"][1].update(ts_ms=float("nan")) or p,
            "ts_ms must be a finite number",
        ),
        (lambda p: p["points"][1].update(px_y=None) or p, "px_y must be a finite number"),
        (lambda p: p["points"][2].update(bridged="yes") or p, "bridged must be a boolean"),
        (
            lambda p: p["points"].append(dict(p["points"][0])) or p,
            "duplicate point timestamp",
        ),
        (lambda p: p.update(segments="post_bounce") or p, "'segments' must be a list"),
        (lambda p: p.update(segments=[42]) or p, r"segments\[0\] must be an object"),
        (
            lambda p: p["segments"][0].update(kind="mid_air") or p,
            "kind must be one of",
        ),
        (
            lambda p: p["segments"][0].update(start_ms=400.0) or p,
            r"segments\[0\] is empty",
        ),
        (
            lambda p: p["segments"][1].update(confidence=1.2) or p,
            r"confidence must be within \[0, 1\]",
        ),
        (lambda p: p.update(flags=[1, 2]) or p, "'flags' must be an object"),
        (
            lambda p: p.update(flags={"long_gap": "yes"}) or p,
            "flags.long_gap must be a boolean",
        ),
        (
            lambda p: p.update(flags={"identity_risk": 1}) or p,
            "flags.identity_risk must be a boolean",
        ),
    ],
)
def test_malformed_payloads_raise(mutate: Any, match: str) -> None:
    payload = mutate(_payload())
    with pytest.raises(TrackPayloadError, match=match):
        parse_track_payload(payload)
