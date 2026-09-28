"""US-I4/US-I5 trajectory analytics: target scoring, turn/apex/dip honesty."""

import math
from typing import Any

import pytest
from cricai_data.enums import Handedness, Length, Line
from cricai_vision.trajectory import (
    DEFAULT_MIN_BOUNCE_CONFIDENCE,
    BouncePoint,
    DeliveryScore,
    FlightConfig,
    FlightPoint,
    FlightValue,
    TargetZone,
    TrajectoryError,
    accuracy_scorecard,
    apex_m,
    dip_flag,
    distance_to_target_m,
    flight_measurements,
    is_target_hit,
    parse_flight_points,
    score_delivery,
    turn_cm,
)
from cricai_vision.zones import ZoneConfig

CONFIG = ZoneConfig()

#: A "good length, off-stump line" declared target (the US-I4 story example).
OFF_GOOD = TargetZone(key="t1", line=Line.OFF, length=Length.GOOD)

#: Bounce squarely inside OFF_GOOD: x in [5, 8), y in [0.1143, 0.40).
HIT_XY = (6.0, 0.2)

#: Bounce on a FULL length, same line: misses OFF_GOOD by length.
MISS_XY = (3.0, 0.2)


def _point(
    ts_ms: float,
    *,
    px_y: float = 0.0,
    px_x: float = 0.0,
    pitch: tuple[float, float] | None = None,
    bridged: bool = False,
) -> FlightPoint:
    return FlightPoint(
        ts_ms=ts_ms,
        px_x=px_x,
        px_y=px_y,
        bridged=bridged,
        pitch_x=pitch[0] if pitch is not None else None,
        pitch_y=pitch[1] if pitch is not None else None,
    )


def _bowled_track(
    *,
    turn_per_m: float = 0.0,
    post_far_x: float = 3.0,
    bridged_pre: int = 0,
) -> tuple[list[FlightPoint], float]:
    """A bowling-end delivery: pitch_x DECREASES 15 -> bounce at 8 -> post_far_x.

    Pre-bounce ground line is y = 0.3; after the bounce the ball deviates
    laterally by ``turn_per_m`` meters per meter of travel. Returns (points,
    bounce_ts_ms).
    """
    points: list[FlightPoint] = []
    ts = 0.0
    for i, x in enumerate([15.0, 14.0, 13.0, 12.0, 11.0, 10.0, 9.0, 8.0]):
        points.append(_point(ts, pitch=(x, 0.3), bridged=i < bridged_pre))
        ts += 20.0
    bounce_ts = points[-1].ts_ms
    x = 7.5
    while x >= post_far_x:
        points.append(_point(ts, pitch=(x, 0.3 + turn_per_m * (8.0 - x))))
        ts += 20.0
        x -= 0.5
    return points, bounce_ts


# --------------------------------------------------------------------------
# parse_flight_points
# --------------------------------------------------------------------------


def _raw_point(ts_ms: float, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"ts_ms": ts_ms, "px_x": 100.0, "px_y": 200.0}
    raw.update(overrides)
    return raw


class TestParseFlightPoints:
    def test_parses_and_sorts_by_timestamp(self) -> None:
        payload = {
            "points": [
                _raw_point(40.0, pitch_x=7.0, pitch_y=0.2, bridged=True),
                _raw_point(20.0),
            ]
        }
        points = parse_flight_points(payload)
        assert [point.ts_ms for point in points] == [20.0, 40.0]
        assert points[0].pitch_x is None and points[0].pitch_y is None
        assert points[1] == FlightPoint(40.0, 100.0, 200.0, True, 7.0, 0.2)

    def test_null_pitch_pair_parses_as_unmapped(self) -> None:
        payload = {"points": [_raw_point(20.0, pitch_x=None, pitch_y=None)]}
        assert parse_flight_points(payload)[0].pitch_x is None

    def test_rejects_non_mapping_payload(self) -> None:
        with pytest.raises(TrajectoryError, match="must be an object"):
            parse_flight_points([1, 2])

    def test_rejects_missing_points_list(self) -> None:
        with pytest.raises(TrajectoryError, match="'points' must be a list"):
            parse_flight_points({"points": "nope"})

    def test_rejects_non_mapping_point(self) -> None:
        with pytest.raises(TrajectoryError, match=r"points\[0\] must be an object"):
            parse_flight_points({"points": ["nope"]})

    def test_rejects_non_boolean_bridged(self) -> None:
        with pytest.raises(TrajectoryError, match="bridged must be a boolean"):
            parse_flight_points({"points": [_raw_point(20.0, bridged=1)]})

    @pytest.mark.parametrize("bad", [None, True, "x", math.inf])
    def test_rejects_non_finite_required_field(self, bad: Any) -> None:
        raw = _raw_point(20.0)
        raw["ts_ms"] = bad
        with pytest.raises(TrajectoryError, match="ts_ms must be a finite number"):
            parse_flight_points({"points": [raw]})

    def test_rejects_non_finite_pitch_coordinate(self) -> None:
        with pytest.raises(TrajectoryError, match="pitch_x must be a finite number"):
            parse_flight_points({"points": [_raw_point(20.0, pitch_x=math.nan, pitch_y=0.2)]})

    def test_rejects_half_present_pitch_pair(self) -> None:
        with pytest.raises(TrajectoryError, match="both pitch coordinates or neither"):
            parse_flight_points({"points": [_raw_point(20.0, pitch_x=7.0)]})

    def test_rejects_duplicate_timestamps(self) -> None:
        with pytest.raises(TrajectoryError, match="duplicate point timestamp"):
            parse_flight_points({"points": [_raw_point(20.0), _raw_point(20.0)]})


# --------------------------------------------------------------------------
# FlightValue / FlightConfig invariants
# --------------------------------------------------------------------------


class TestValueAndConfigInvariants:
    def test_null_flight_value_requires_reason(self) -> None:
        with pytest.raises(TrajectoryError, match="requires a reason"):
            FlightValue(value=None, confidence=0.0)

    def test_flight_value_confidence_bounds(self) -> None:
        with pytest.raises(TrajectoryError, match=r"confidence must be in \[0, 1\]"):
            FlightValue(value=1.0, confidence=1.5)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"turn_eval_distance_m": 0.0},
            {"min_post_reach_m": math.inf},
            {"min_fit_points": 1},
            {"min_descent_points": 4},
            {"late_fraction": 1.0},
        ],
    )
    def test_config_validation(self, overrides: dict[str, Any]) -> None:
        with pytest.raises(TrajectoryError):
            FlightConfig(**overrides)


# --------------------------------------------------------------------------
# US-I5: turn after pitching
# --------------------------------------------------------------------------


class TestTurn:
    def test_straight_delivery_measures_no_turn(self) -> None:
        points, bounce_ts = _bowled_track(turn_per_m=0.0)
        result = turn_cm(points, bounce_ts)
        assert result.value == pytest.approx(0.0, abs=1e-9)
        assert result.confidence == pytest.approx(1.0)
        assert result.reason is None

    def test_leg_break_deviation_measured_at_two_meters(self) -> None:
        # 0.075 m of lateral deviation per meter of travel -> 15 cm at 2 m.
        points, bounce_ts = _bowled_track(turn_per_m=0.075)
        result = turn_cm(points, bounce_ts)
        assert result.value == pytest.approx(15.0, abs=1e-6)

    def test_direction_is_inferred_not_assumed(self) -> None:
        # The mirrored delivery (pitch_x increasing) measures the same turn.
        points, bounce_ts = _bowled_track(turn_per_m=0.075)
        mirrored = [
            FlightPoint(
                ts_ms=point.ts_ms,
                px_x=point.px_x,
                px_y=point.px_y,
                bridged=point.bridged,
                pitch_x=20.12 - float(point.pitch_x or 0.0),
                pitch_y=point.pitch_y,
            )
            for point in points
        ]
        result = turn_cm(mirrored, bounce_ts)
        assert result.value == pytest.approx(15.0, abs=1e-6)

    def test_short_post_reach_scales_confidence_and_evaluates_at_reach(self) -> None:
        # Post points reach only 1 m past the bounce: deviation 7.5 cm there.
        points, bounce_ts = _bowled_track(turn_per_m=0.075, post_far_x=7.0)
        result = turn_cm(points, bounce_ts)
        assert result.value == pytest.approx(7.5, abs=1e-6)
        assert result.confidence == pytest.approx(0.5)  # 1 m of a 2 m evaluation window

    def test_bridged_points_reduce_confidence(self) -> None:
        points, bounce_ts = _bowled_track(bridged_pre=2)
        result = turn_cm(points, bounce_ts)
        used = 8 + 10  # pre + post mapped points
        assert result.confidence == pytest.approx((used - 2) / used)

    def test_pixel_only_track_is_null_with_reason(self) -> None:
        points = [_point(ts, px_y=300.0) for ts in (0.0, 20.0, 40.0, 60.0)]
        result = turn_cm(points, 40.0)
        assert result.value is None
        assert "not pitch-mapped" in str(result.reason)

    def test_too_few_pre_points_is_null(self) -> None:
        points, bounce_ts = _bowled_track()
        sliced = points[7:]  # only the bounce point remains pre-bounce
        result = turn_cm(sliced, bounce_ts)
        assert result.value is None
        assert "too few pitch-mapped points" in str(result.reason)

    def test_too_few_post_points_is_null(self) -> None:
        points, bounce_ts = _bowled_track()
        sliced = points[:9]  # eight pre points, one post point
        result = turn_cm(sliced, bounce_ts)
        assert result.value is None
        assert "too few pitch-mapped points" in str(result.reason)

    def test_short_pre_reach_is_null(self) -> None:
        points, bounce_ts = _bowled_track()
        sliced = points[6:]  # pre reach 1 m... then shrink below the 1 m floor
        squeezed = [
            FlightPoint(
                ts_ms=point.ts_ms,
                px_x=point.px_x,
                px_y=point.px_y,
                bridged=point.bridged,
                pitch_x=8.0 + (float(point.pitch_x or 0.0) - 8.0) * 0.5,
                pitch_y=point.pitch_y,
            )
            for point in sliced
        ]
        result = turn_cm(squeezed, bounce_ts)
        assert result.value is None
        assert "pre-bounce mapped ground path too short" in str(result.reason)

    def test_short_post_reach_is_null(self) -> None:
        config = FlightConfig(min_post_reach_m=1.5)
        points, bounce_ts = _bowled_track(post_far_x=7.0)  # post reach 1 m
        result = turn_cm(points, bounce_ts, config=config)
        assert result.value is None
        assert "post-bounce mapped ground path too short" in str(result.reason)


# --------------------------------------------------------------------------
# US-I5: apex height
# --------------------------------------------------------------------------


def _arc_points(
    py_values: list[float], *, bridged_first: bool = False, step_ms: float = 20.0
) -> list[FlightPoint]:
    return [
        _point(i * step_ms, px_y=py, bridged=bridged_first and i == 0)
        for i, py in enumerate(py_values)
    ]


class TestApex:
    def test_no_calibrated_scale_is_null_with_reason(self) -> None:
        points = _arc_points([300.0, 280.0, 300.0, 340.0])
        result = apex_m(points, 60.0, vertical_scale_m_per_px=None)
        assert result.value is None
        assert "no calibrated vertical scale" in str(result.reason)

    @pytest.mark.parametrize("bad", [0.0, -0.01, math.nan])
    def test_invalid_scale_is_a_loud_error(self, bad: float) -> None:
        points = _arc_points([300.0, 280.0, 300.0])
        with pytest.raises(TrajectoryError, match="vertical_scale_m_per_px"):
            apex_m(points, 40.0, vertical_scale_m_per_px=bad)

    def test_measures_height_above_the_bounce(self) -> None:
        # Highest image point 268 px, bounce at 340 px: 72 px * 0.01 m/px.
        points = _arc_points([300.0, 280.0, 268.0, 275.0, 300.0, 340.0])
        result = apex_m(points, 100.0, vertical_scale_m_per_px=0.01)
        assert result.value == pytest.approx(0.72)
        assert result.confidence == pytest.approx(1.0)

    def test_bridged_points_reduce_confidence(self) -> None:
        points = _arc_points([300.0, 280.0, 268.0, 275.0, 340.0], bridged_first=True)
        result = apex_m(points, 80.0, vertical_scale_m_per_px=0.01)
        assert result.confidence == pytest.approx(4 / 5)

    def test_too_few_pre_points_is_null(self) -> None:
        points = _arc_points([300.0, 280.0])
        result = apex_m(points, 20.0, vertical_scale_m_per_px=0.01)
        assert result.value is None
        assert "too few tracked points" in str(result.reason)

    def test_apex_at_window_edge_is_null(self) -> None:
        # Monotonic descent: the true peak happened before tracking started.
        points = _arc_points([268.0, 280.0, 300.0, 340.0])
        result = apex_m(points, 60.0, vertical_scale_m_per_px=0.01)
        assert result.value is None
        assert "not bracketed" in str(result.reason)

    def test_bounce_outside_window_is_null(self) -> None:
        points = _arc_points([300.0, 280.0, 268.0, 275.0])
        result = apex_m(points, 999.0, vertical_scale_m_per_px=0.01)
        assert result.value is None
        assert "outside the tracked window" in str(result.reason)

    def test_apex_not_above_bounce_is_null(self) -> None:
        # The interpolated bounce pixel sits ABOVE the pre-bounce minimum.
        points = _arc_points([400.0, 390.0, 395.0, 370.0])
        result = apex_m(points, 56.0, vertical_scale_m_per_px=0.01)
        assert result.value is None
        assert "not above the bounce point" in str(result.reason)


# --------------------------------------------------------------------------
# US-I5: dip (reference-arc residual test on synthetic trajectories)
# --------------------------------------------------------------------------


def _parabolic_descent(
    n: int, *, late_extra_px: float = 0.0, n_late: int = 3
) -> tuple[list[FlightPoint], float]:
    """An arc-like descent (px_y accelerating down) with optional extra late drop."""
    points = []
    for i in range(n):
        py = 268.0 + 0.5 * (i * 10.0) ** 2 / 100.0
        if late_extra_px and i >= n - n_late:
            py += late_extra_px * (i - (n - n_late) + 1)
        points.append(_point(i * 10.0, px_y=py))
    return points, points[-1].ts_ms


class TestDip:
    def test_plain_arc_descent_never_trips_the_flag(self) -> None:
        points, bounce_ts = _parabolic_descent(12)
        result = dip_flag(points, bounce_ts)
        assert result.value is False
        assert result.confidence == pytest.approx(1.0)

    def test_extra_late_drop_trips_the_flag(self) -> None:
        points, bounce_ts = _parabolic_descent(12, late_extra_px=8.0)
        result = dip_flag(points, bounce_ts)
        assert result.value is True

    def test_no_pre_bounce_points_is_null(self) -> None:
        points, _ = _parabolic_descent(12)
        result = dip_flag(points, -1.0)
        assert result.value is None
        assert "no tracked points before the bounce" in str(result.reason)

    def test_short_descent_is_null(self) -> None:
        points, bounce_ts = _parabolic_descent(6)
        result = dip_flag(points, bounce_ts)
        assert result.value is None
        assert "too few descending points" in str(result.reason)

    def test_too_few_early_points_is_null(self) -> None:
        config = FlightConfig(late_fraction=0.75)
        points, bounce_ts = _parabolic_descent(8)
        result = dip_flag(points, bounce_ts, config=config)
        assert result.value is None
        assert "too few early descent points" in str(result.reason)


class TestFlightMeasurements:
    def test_combines_all_three_quantities(self) -> None:
        points, bounce_ts = _bowled_track(turn_per_m=0.075)
        measured = flight_measurements(points, bounce_ts, vertical_scale_m_per_px=None)
        assert measured.turn_cm.value == pytest.approx(15.0, abs=1e-6)
        assert measured.apex_m.value is None  # honest: no calibrated scale
        assert measured.dip_flag.value is False  # a flat pixel path shows no late drop


# --------------------------------------------------------------------------
# US-I4: target predicate, miss distance, delivery scoring
# --------------------------------------------------------------------------


class TestTargetScoring:
    def test_hit_inside_the_declared_zone(self) -> None:
        score = score_delivery(CONFIG, ball_no=1, target=OFF_GOOD, bounce=BouncePoint(*HIT_XY, 0.9))
        assert score.hit is True
        assert score.distance_m == 0.0
        assert score.confidence == 0.9
        assert score.target_key == "t1"

    def test_miss_reports_distance_to_the_zone(self) -> None:
        score = score_delivery(
            CONFIG, ball_no=2, target=OFF_GOOD, bounce=BouncePoint(*MISS_XY, 0.9)
        )
        assert score.hit is False
        assert score.distance_m == pytest.approx(2.0)  # 3.0 m -> the 5.0 m band edge

    def test_length_edge_belongs_to_the_farther_band(self) -> None:
        # x = 5.0 is exactly the FULL/GOOD edge: half-open bands say GOOD.
        on_edge = BouncePoint(5.0, 0.2, 0.9)
        edge_hit = score_delivery(CONFIG, ball_no=3, target=OFF_GOOD, bounce=on_edge)
        assert edge_hit.hit is True
        assert edge_hit.distance_m == 0.0  # the lower edge is inside the band
        full_target = TargetZone(key="t2", line=Line.OFF, length=Length.FULL)
        edge_score = score_delivery(CONFIG, ball_no=3, target=full_target, bounce=on_edge)
        assert edge_score.hit is False
        # The shared edge belongs to the farther band, so the miss distance is
        # positive (one float below the edge) — never a "missed by 0.0 m".
        assert edge_score.distance_m is not None
        assert 0.0 < edge_score.distance_m < 1e-12

    def test_line_edge_belongs_to_the_more_off_side_channel(self) -> None:
        # y = 0.1143 is exactly the MIDDLE/OFF edge: OFF wins (US-C5 rule).
        on_edge = BouncePoint(6.0, 0.1143, 0.9)
        edge_hit = score_delivery(CONFIG, ball_no=4, target=OFF_GOOD, bounce=on_edge)
        assert edge_hit.hit is True
        assert edge_hit.distance_m == 0.0  # the lower edge is inside the channel

    def test_band_edge_miss_never_reports_zero_distance(self) -> None:
        """US-I4 edge consistency: hit=False never pairs with distance 0.0.

        classify uses half-open [lo, hi) bands, so a bounce exactly on the
        target zone's FAR edge is outside the zone; distance_to_target_m must
        agree (the same half-open region), or a manually marked round-number
        bounce lands as an internally contradictory "missed by 0.0 m" record.
        """
        far_length_edge = BouncePoint(8.0, 0.2, 1.0)  # GOOD/SHORT edge -> SHORT
        far_line_edge = BouncePoint(6.0, 0.40, 1.0)  # OFF/OUTSIDE_OFF edge
        for bounce in (far_length_edge, far_line_edge):
            score = score_delivery(CONFIG, ball_no=9, target=OFF_GOOD, bounce=bounce)
            assert score.hit is False
            assert score.distance_m is not None
            assert score.distance_m > 0.0

    def test_left_hander_mirrors_the_lateral_axis(self) -> None:
        mirrored = BouncePoint(6.0, -0.2, 0.9)
        score = score_delivery(
            CONFIG, ball_no=5, target=OFF_GOOD, bounce=mirrored, handedness=Handedness.LEFT
        )
        assert score.hit is True
        assert score.distance_m == 0.0

    def test_no_declared_target_is_unscored(self) -> None:
        score = score_delivery(CONFIG, ball_no=6, target=None, bounce=BouncePoint(*HIT_XY, 0.9))
        assert score.hit is None
        assert score.target_key is None
        assert "no declared target" in str(score.reason)
        assert score.pitch_x == HIT_XY[0]  # coordinates survive for the pitch map

    def test_no_bounce_is_unscored(self) -> None:
        score = score_delivery(CONFIG, ball_no=7, target=OFF_GOOD, bounce=None)
        assert score.hit is None
        assert score.confidence == 0.0
        assert "no bounce estimate" in str(score.reason)

    def test_off_pitch_bounce_is_unscored_but_measured(self) -> None:
        score = score_delivery(
            CONFIG, ball_no=8, target=OFF_GOOD, bounce=BouncePoint(-1.0, 0.2, 0.9)
        )
        assert score.hit is None
        assert "outside the classifiable pitch area" in str(score.reason)
        assert score.distance_m == pytest.approx(6.0)  # still an honest miss distance

    def test_is_target_hit_requires_line_and_length(self) -> None:
        assert is_target_hit(Line.OFF, Length.GOOD, OFF_GOOD) is True
        assert is_target_hit(Line.OFF, Length.FULL, OFF_GOOD) is False
        assert is_target_hit(Line.MIDDLE, Length.GOOD, OFF_GOOD) is False

    def test_distance_rejects_non_finite_points(self) -> None:
        with pytest.raises(TrajectoryError, match="invalid bounce point"):
            distance_to_target_m(CONFIG, math.nan, 0.2, OFF_GOOD)

    def test_unscored_delivery_requires_reason(self) -> None:
        with pytest.raises(TrajectoryError, match="requires a reason"):
            DeliveryScore(
                ball_no=1,
                target_key="t1",
                hit=None,
                confidence=0.5,
                pitch_x=None,
                pitch_y=None,
                distance_m=None,
                reason=None,
            )

    def test_delivery_score_confidence_bounds(self) -> None:
        with pytest.raises(TrajectoryError, match=r"confidence must be in \[0, 1\]"):
            DeliveryScore(
                ball_no=1,
                target_key="t1",
                hit=True,
                confidence=1.5,
                pitch_x=6.0,
                pitch_y=0.2,
                distance_m=0.0,
                reason=None,
            )


# --------------------------------------------------------------------------
# US-I4: accuracy scorecard (ST: 60-ball block vs a manual count)
# --------------------------------------------------------------------------


def _sixty_ball_block() -> list[DeliveryScore]:
    """A 60-delivery target block with every honesty category present."""
    scores: list[DeliveryScore] = []
    for ball_no in range(1, 41):  # scored, confident: alternate hit/miss
        xy = HIT_XY if ball_no % 2 == 0 else MISS_XY
        scores.append(
            score_delivery(CONFIG, ball_no=ball_no, target=OFF_GOOD, bounce=BouncePoint(*xy, 0.9))
        )
    for ball_no in range(41, 51):  # on target but the bounce is not confident
        scores.append(
            score_delivery(
                CONFIG, ball_no=ball_no, target=OFF_GOOD, bounce=BouncePoint(*HIT_XY, 0.3)
            )
        )
    for ball_no in range(51, 56):  # full tosses / tracking failures
        scores.append(score_delivery(CONFIG, ball_no=ball_no, target=OFF_GOOD, bounce=None))
    for ball_no in range(56, 61):  # bowled with no declared target
        scores.append(
            score_delivery(CONFIG, ball_no=ball_no, target=None, bounce=BouncePoint(*HIT_XY, 0.9))
        )
    return scores


class TestScorecard:
    def test_sixty_ball_block_matches_the_manual_count(self) -> None:
        scores = _sixty_ball_block()
        manual_hits = sum(
            1
            for score in scores
            if score.target_key is not None
            and score.hit is True
            and score.confidence >= DEFAULT_MIN_BOUNCE_CONFIDENCE
        )
        manual_attempts = sum(
            1
            for score in scores
            if score.target_key is not None
            and score.hit is not None
            and score.confidence >= DEFAULT_MIN_BOUNCE_CONFIDENCE
        )
        card = accuracy_scorecard(scores)
        assert card.total == 60
        assert card.untargeted == 5
        assert len(card.targets) == 1
        row = card.targets[0]
        assert row.target_key == "t1"
        assert row.attempts == manual_attempts == 40  # the denominator is explicit
        assert row.hits == manual_hits == 20
        assert row.hit_rate == pytest.approx(0.5)
        assert row.low_confidence == 10
        assert row.unscored == 5
        assert len(row.scatter) == row.attempts  # scatter shows exactly the denominator

    def test_scatter_points_carry_the_bounce_geometry(self) -> None:
        card = accuracy_scorecard(_sixty_ball_block())
        first = card.targets[0].scatter[0]
        assert (first.pitch_x, first.pitch_y) == MISS_XY
        assert first.hit is False
        assert first.distance_m == pytest.approx(2.0)

    def test_no_confident_attempts_yields_no_rate(self) -> None:
        scores = [
            score_delivery(CONFIG, ball_no=1, target=OFF_GOOD, bounce=BouncePoint(*HIT_XY, 0.1))
        ]
        row = accuracy_scorecard(scores).targets[0]
        assert row.attempts == 0
        assert row.hit_rate is None  # never a silent 0%

    def test_targets_keep_first_appearance_order(self) -> None:
        other = TargetZone(key="t9", line=Line.MIDDLE, length=Length.YORKER)
        scores = [
            score_delivery(CONFIG, ball_no=1, target=other, bounce=BouncePoint(*HIT_XY, 0.9)),
            score_delivery(CONFIG, ball_no=2, target=OFF_GOOD, bounce=BouncePoint(*HIT_XY, 0.9)),
        ]
        card = accuracy_scorecard(scores)
        assert [row.target_key for row in card.targets] == ["t9", "t1"]

    def test_payload_shape_for_report_consumers(self) -> None:
        payload = accuracy_scorecard(_sixty_ball_block()).to_payload()
        assert payload["min_confidence"] == DEFAULT_MIN_BOUNCE_CONFIDENCE
        assert payload["total"] == 60
        assert payload["untargeted"] == 5
        row = payload["targets"][0]
        assert row["attempts"] == 40 and row["hits"] == 20
        scatter_point = row["scatter"][0]
        assert set(scatter_point) == {"ball_no", "pitch_x", "pitch_y", "hit", "distance_m"}

    def test_min_confidence_bounds_are_validated(self) -> None:
        with pytest.raises(TrajectoryError, match="min_confidence"):
            accuracy_scorecard([], min_confidence=1.5)


# The US-I5 banned-claim module lint lives in the SAF suite at
# packages/coaching/tests/test_banned_claims_saf.py: ONE canonical banned-list
# (cricai_coaching.content_lint.BANNED_SPIN_CLAIMS) scans this module's
# strings, so the lint can never drift behind the canonical list again.
