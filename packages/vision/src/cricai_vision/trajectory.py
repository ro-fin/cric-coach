"""Bowling-end trajectory analytics: targets and flight geometry (US-I4/US-I5).

Everything here is GEOMETRY measured from tracked trajectories in the
canonical pitch frame (:mod:`cricai_vision.geometry`: origin at the striker's
middle stump, +x toward the bowler's end at ``x = PITCH_LENGTH_M``, +y toward
the off side for a right-hand batter). A leg-spin delivery is bowled FROM the
bowler's end, so pitch_x DECREASES along the flight; the math infers travel
direction from the data rather than assuming it.

US-I4 — line/length vs a declared target:
    Bounce points reuse the US-C5 zone vocabulary (:mod:`cricai_vision.zones`,
    the same config the bounce pipeline classifies with). A delivery hits its
    target when its bounce classifies into the declared line AND length;
    :func:`distance_to_target_m` gives the miss distance to the same half-open
    zone bands the classifier assigns with (consistent boundary semantics: a
    band-edge bounce is outside the zone for BOTH the hit predicate and the
    distance). :func:`accuracy_scorecard` aggregates per-target hit rates over
    ONLY the deliveries with a confident bounce — the denominator is explicit,
    never inflated (US-I4 AC) — plus the zone scatter behind each rate.

US-I5 — turn, flight and dip (honest measurements):
    * :func:`turn_cm` — lateral deviation after pitching: a straight-line fit
      of the pre-bounce ground path is extrapolated past the bounce and
      compared against the post-bounce fit at up to
      ``turn_eval_distance_m`` (2 m) beyond it. Positive values deviate
      toward +y (the off side for a right-hand batter). Needs pitch-mapped
      points on both sides of the bounce: a single-camera pixel-only track
      yields null-with-reason, never a fabricated number.
    * :func:`apex_m` — greatest height between release and bounce, from the
      pixel gap between the flight's highest image point and the bounce
      point, scaled by a calibrated vertical meters-per-pixel factor. No
      calibrated scale means null-with-reason.
    * :func:`dip_flag` — late-descent steepening: a quadratic reference arc is
      fit to the EARLY descent and the LATE points are compared against its
      extrapolation; the flag is set when the ball drops materially below the
      arc (fit-residual test, US-I5). Scale-free, so it works on pixel-only
      tracks.

No claim in this module goes beyond what the trajectory shows: wording is
strictly geometric (turn, drift, dip, bounce) per the backlog honesty rule.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast

import numpy as np
from cricai_data.enums import Handedness, Length, Line

from cricai_vision.zones import ZoneConfig, ZoneConfigError, classify

#: Analytics version recorded as provenance on derived metrics.
TRAJECTORY_VERSION: Final = "flight-geom-1"

#: Deliveries whose bounce confidence is below this are excluded from the
#: accuracy denominator by default (US-I4 AC: confident bounces only).
DEFAULT_MIN_BOUNCE_CONFIDENCE: Final = 0.5


class TrajectoryError(ValueError):
    """Invalid trajectory-analytics input (malformed points, bad config)."""


# --------------------------------------------------------------------------
# Shared value shape: nullable-with-reason, never silent zeros
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlightValue:
    """One measured flight quantity, or the reason it cannot be measured."""

    value: float | bool | None
    confidence: float
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.reason:
            raise TrajectoryError("null flight value requires a reason (never silent)")
        if not 0.0 <= self.confidence <= 1.0:
            raise TrajectoryError(f"confidence must be in [0, 1], got {self.confidence}")


def _null(reason: str) -> FlightValue:
    return FlightValue(value=None, confidence=0.0, reason=reason)


# --------------------------------------------------------------------------
# Flight points: the pinned US-F3 track payload, with optional pitch mapping
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlightPoint:
    """One tracked ball position: camera pixels plus optional pitch meters."""

    ts_ms: float
    px_x: float
    px_y: float
    bridged: bool
    pitch_x: float | None
    pitch_y: float | None


def _finite(raw: Mapping[str, Any], key: str, where: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TrajectoryError(f"{where}.{key} must be a finite number, got {value!r}")
    return float(value)


def _optional_finite(raw: Mapping[str, Any], key: str, where: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    return _finite(raw, key, where)


def _parse_point(raw: object, index: int) -> FlightPoint:
    where = f"points[{index}]"
    if not isinstance(raw, Mapping):
        raise TrajectoryError(f"{where} must be an object")
    bridged = raw.get("bridged", False)
    if not isinstance(bridged, bool):
        raise TrajectoryError(f"{where}.bridged must be a boolean, got {bridged!r}")
    pitch_x = _optional_finite(raw, "pitch_x", where)
    pitch_y = _optional_finite(raw, "pitch_y", where)
    if (pitch_x is None) != (pitch_y is None):
        raise TrajectoryError(f"{where} must carry both pitch coordinates or neither")
    return FlightPoint(
        ts_ms=_finite(raw, "ts_ms", where),
        px_x=_finite(raw, "px_x", where),
        px_y=_finite(raw, "px_y", where),
        bridged=bridged,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
    )


def parse_flight_points(payload: object) -> tuple[FlightPoint, ...]:
    """Validate a raw (JSON-decoded) US-F3 track payload into flight points.

    Points come back sorted by ``ts_ms``. ``pitch_x``/``pitch_y`` ride along
    when the track was pitch-mapped (US-C2 homography); a point must carry
    both or neither. Malformed payloads raise :class:`TrajectoryError` — a
    contract violation is a caller bug, never a silent no-measurement.
    """
    if not isinstance(payload, Mapping):
        raise TrajectoryError(f"track payload must be an object, got {type(payload).__name__}")
    raw_points = payload.get("points")
    if not isinstance(raw_points, Sequence) or isinstance(raw_points, str | bytes):
        raise TrajectoryError("track payload field 'points' must be a list")
    points = sorted(
        (_parse_point(point, i) for i, point in enumerate(raw_points)),
        key=lambda point: point.ts_ms,
    )
    for previous, current in itertools.pairwise(points):
        if current.ts_ms <= previous.ts_ms:
            raise TrajectoryError(f"duplicate point timestamp ts_ms={current.ts_ms}")
    return tuple(points)


# --------------------------------------------------------------------------
# Flight configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlightConfig:
    """Thresholds for the US-I5 flight measurements (validated, pinned)."""

    turn_eval_distance_m: float = 2.0  # US-I5: deviation over the first 2 m after bounce
    min_pre_reach_m: float = 1.0  # mapped ground path required before the bounce
    min_post_reach_m: float = 0.5  # mapped ground path required after the bounce
    min_fit_points: int = 2  # fewest mapped points per side of the bounce
    min_apex_points: int = 3  # fewest pre-bounce points that can bracket an apex
    min_descent_points: int = 8  # fewest descent points for the dip residual test
    min_early_points: int = 4  # fewest early points for the reference arc fit
    late_fraction: float = 0.25  # trailing share of the descent tested against the arc
    dip_floor_px: float = 3.0  # noise floor on the late residual (pixels)
    dip_sigma: float = 3.0  # late residual must exceed this multiple of the early RMS

    def __post_init__(self) -> None:
        positive = (
            ("turn_eval_distance_m", self.turn_eval_distance_m),
            ("min_pre_reach_m", self.min_pre_reach_m),
            ("min_post_reach_m", self.min_post_reach_m),
            ("dip_floor_px", self.dip_floor_px),
            ("dip_sigma", self.dip_sigma),
        )
        for name, value in positive:
            if not math.isfinite(value) or value <= 0:
                raise TrajectoryError(f"{name} must be a finite value > 0, got {value!r}")
        counts = (
            ("min_fit_points", self.min_fit_points, 2),
            ("min_apex_points", self.min_apex_points, 3),
            ("min_descent_points", self.min_descent_points, 5),
            ("min_early_points", self.min_early_points, 3),
        )
        for name, count, floor in counts:
            if count < floor:
                raise TrajectoryError(f"{name} must be >= {floor}, got {count!r}")
        if not 0.0 < self.late_fraction < 1.0:
            raise TrajectoryError(
                f"late_fraction must be within (0, 1), got {self.late_fraction!r}"
            )


DEFAULT_FLIGHT_CONFIG: Final = FlightConfig()


# --------------------------------------------------------------------------
# US-I5: turn after pitching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _GroundPoint:
    """A pitch-mapped point's ground coordinates (both meters present)."""

    ts_ms: float
    x: float
    y: float
    bridged: bool


def _ground_points(points: Sequence[FlightPoint]) -> list[_GroundPoint]:
    ground: list[_GroundPoint] = []
    for point in points:
        if point.pitch_x is not None and point.pitch_y is not None:
            ground.append(_GroundPoint(point.ts_ms, point.pitch_x, point.pitch_y, point.bridged))
    return ground


def _detected_fraction(points: Sequence[FlightPoint | _GroundPoint]) -> float:
    return sum(1 for point in points if not point.bridged) / len(points)


def _line_fit(points: Sequence[_GroundPoint]) -> tuple[float, float]:
    """Least-squares ground-path line y = a*x + b over mapped points."""
    xs = np.array([point.x for point in points], dtype=np.float64)
    ys = np.array([point.y for point in points], dtype=np.float64)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def _bounce_pitch_x(
    pre: Sequence[_GroundPoint], post: Sequence[_GroundPoint], ts_ms: float
) -> float:
    """pitch_x at the bounce time, interpolated between its bracketing points."""
    before, after = pre[-1], post[0]
    fraction = (ts_ms - before.ts_ms) / (after.ts_ms - before.ts_ms)
    return before.x + (after.x - before.x) * fraction


def turn_cm(
    points: Sequence[FlightPoint],
    bounce_ts_ms: float,
    *,
    config: FlightConfig = DEFAULT_FLIGHT_CONFIG,
) -> FlightValue:
    """Lateral deviation after pitching, in cm (US-I5) — geometry only.

    The pre-bounce ground path (pitch y vs x over the mapped points) is fit
    with a line and extrapolated beyond the bounce; the post-bounce fit is
    evaluated at the same down-pitch distance (up to
    ``config.turn_eval_distance_m`` past the bounce, or as far as the mapped
    points reach). The signed difference is the turn: positive toward +y (the
    off side for a right-hand batter in the canonical frame). Null-with-reason
    whenever the track cannot support the measurement — a pixel-only track,
    too few mapped points on either side, or too little ground reach.
    """
    mapped = _ground_points(points)
    if not mapped:
        return _null(
            "track is not pitch-mapped (single-camera pixel-only track); "
            "lateral deviation needs pitch coordinates"
        )
    pre = [point for point in mapped if point.ts_ms <= bounce_ts_ms]
    post = [point for point in mapped if point.ts_ms > bounce_ts_ms]
    if len(pre) < config.min_fit_points or len(post) < config.min_fit_points:
        return _null(
            f"too few pitch-mapped points around the bounce "
            f"(pre={len(pre)}, post={len(post)}, need {config.min_fit_points} each)"
        )
    direction = math.copysign(1.0, pre[-1].x - pre[0].x)
    bounce_x = _bounce_pitch_x(pre, post, bounce_ts_ms)
    pre_reach = max(direction * (bounce_x - point.x) for point in pre)
    post_reach = max(direction * (point.x - bounce_x) for point in post)
    if pre_reach < config.min_pre_reach_m:
        return _null(
            f"pre-bounce mapped ground path too short "
            f"({pre_reach:.2f} m, need {config.min_pre_reach_m:g} m)"
        )
    if post_reach < config.min_post_reach_m:
        return _null(
            f"post-bounce mapped ground path too short "
            f"({post_reach:.2f} m, need {config.min_post_reach_m:g} m)"
        )
    eval_x = bounce_x + direction * min(config.turn_eval_distance_m, post_reach)
    pre_slope, pre_intercept = _line_fit(pre)
    post_slope, post_intercept = _line_fit(post)
    deviation_m = (post_slope * eval_x + post_intercept) - (pre_slope * eval_x + pre_intercept)
    confidence = _detected_fraction([*pre, *post]) * min(
        1.0, post_reach / config.turn_eval_distance_m
    )
    return FlightValue(value=deviation_m * 100.0, confidence=confidence)


# --------------------------------------------------------------------------
# US-I5: apex height (needs a calibrated vertical scale)
# --------------------------------------------------------------------------


def _px_y_at(points: Sequence[FlightPoint], ts_ms: float) -> float | None:
    """px_y interpolated at ``ts_ms``; None when outside the tracked window."""
    if not points[0].ts_ms <= ts_ms <= points[-1].ts_ms:
        return None
    ts = [point.ts_ms for point in points]
    py = [point.px_y for point in points]
    return float(np.interp(ts_ms, ts, py))


def apex_m(
    points: Sequence[FlightPoint],
    bounce_ts_ms: float,
    *,
    vertical_scale_m_per_px: float | None,
    config: FlightConfig = DEFAULT_FLIGHT_CONFIG,
) -> FlightValue:
    """Greatest height between release and bounce, in meters (US-I5).

    The pixel gap between the flight's highest image point (smallest ``px_y``
    before the bounce) and the ball's image position at the bounce is scaled
    by a calibrated vertical meters-per-pixel factor (side-on view). Without a
    calibrated scale the height is null-with-reason — a single-camera
    pixel-only view cannot measure height honestly. The apex must be bracketed
    by tracked points on both sides, or the true peak may lie outside the
    window.
    """
    if vertical_scale_m_per_px is None:
        return _null(
            "no calibrated vertical scale for this camera; "
            "apex height cannot be measured from pixels alone"
        )
    if not math.isfinite(vertical_scale_m_per_px) or vertical_scale_m_per_px <= 0:
        raise TrajectoryError(
            f"vertical_scale_m_per_px must be a finite value > 0, got {vertical_scale_m_per_px!r}"
        )
    pre = [point for point in points if point.ts_ms <= bounce_ts_ms]
    if len(pre) < config.min_apex_points:
        return _null(
            f"too few tracked points before the bounce to locate the apex "
            f"({len(pre)}, need {config.min_apex_points})"
        )
    apex_index = min(range(len(pre)), key=lambda i: pre[i].px_y)
    if apex_index in (0, len(pre) - 1):
        return _null("apex is not bracketed by the tracked window (peak sits at the edge)")
    ground_px_y = _px_y_at(points, bounce_ts_ms)
    if ground_px_y is None:
        return _null("bounce time is outside the tracked window")
    height_px = ground_px_y - pre[apex_index].px_y
    if height_px <= 0.0:
        return _null("apex is not above the bounce point in the image (view unsuitable)")
    return FlightValue(
        value=height_px * vertical_scale_m_per_px,
        confidence=_detected_fraction(pre),
    )


# --------------------------------------------------------------------------
# US-I5: dip — late-descent steepening vs a reference arc
# --------------------------------------------------------------------------


def dip_flag(
    points: Sequence[FlightPoint],
    bounce_ts_ms: float,
    *,
    config: FlightConfig = DEFAULT_FLIGHT_CONFIG,
) -> FlightValue:
    """Whether the descent steepened late in flight (US-I5) — a residual test.

    A quadratic reference arc is fit to the EARLY portion of the descent
    (apex to bounce) and extrapolated under the LATE points. The flag is set
    when the late points drop below the arc by more than
    ``max(dip_floor_px, dip_sigma * early_fit_rms)`` — a plain arc-like
    descent never trips it, an extra late drop does. Scale-free (pixel
    residuals against a pixel fit), so pixel-only tracks qualify.
    """
    pre = [point for point in points if point.ts_ms <= bounce_ts_ms]
    if not pre:
        return _null("no tracked points before the bounce")
    apex_index = min(range(len(pre)), key=lambda i: pre[i].px_y)
    descent = pre[apex_index:]
    if len(descent) < config.min_descent_points:
        return _null(
            f"too few descending points before the bounce to test late dip "
            f"({len(descent)}, need {config.min_descent_points})"
        )
    n_late = max(2, math.ceil(config.late_fraction * len(descent)))
    early, late = descent[:-n_late], descent[-n_late:]
    if len(early) < config.min_early_points:
        return _null(
            f"too few early descent points to fit the reference arc "
            f"({len(early)}, need {config.min_early_points})"
        )
    ts_early = np.array([point.ts_ms for point in early], dtype=np.float64)
    py_early = np.array([point.px_y for point in early], dtype=np.float64)
    coefficients = np.polyfit(ts_early, py_early, 2)
    early_rms = float(np.sqrt(np.mean((py_early - np.polyval(coefficients, ts_early)) ** 2)))
    ts_late = np.array([point.ts_ms for point in late], dtype=np.float64)
    py_late = np.array([point.px_y for point in late], dtype=np.float64)
    late_residual = float(np.mean(py_late - np.polyval(coefficients, ts_late)))
    threshold = max(config.dip_floor_px, config.dip_sigma * early_rms)
    return FlightValue(
        value=late_residual > threshold,
        confidence=_detected_fraction(descent),
    )


@dataclass(frozen=True)
class FlightMeasurements:
    """The three US-I5 flight quantities for one delivery on one camera."""

    turn_cm: FlightValue
    apex_m: FlightValue
    dip_flag: FlightValue


def flight_measurements(
    points: Sequence[FlightPoint],
    bounce_ts_ms: float,
    *,
    vertical_scale_m_per_px: float | None,
    config: FlightConfig = DEFAULT_FLIGHT_CONFIG,
) -> FlightMeasurements:
    """All three US-I5 measurements over one track (each honestly nullable)."""
    return FlightMeasurements(
        turn_cm=turn_cm(points, bounce_ts_ms, config=config),
        apex_m=apex_m(
            points, bounce_ts_ms, vertical_scale_m_per_px=vertical_scale_m_per_px, config=config
        ),
        dip_flag=dip_flag(points, bounce_ts_ms, config=config),
    )


# --------------------------------------------------------------------------
# US-I4: target hit/miss, miss distance, accuracy scorecard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetZone:
    """A declared line/length target (one ``bowling_targets`` row's zone)."""

    key: str
    line: Line
    length: Length


@dataclass(frozen=True)
class BouncePoint:
    """A resolved bounce for scoring: pitch coordinates plus confidence."""

    pitch_x: float
    pitch_y: float
    confidence: float


@dataclass(frozen=True)
class DeliveryScore:
    """One delivery scored against its declared target (US-I4).

    ``hit`` is None-with-``reason`` when the delivery cannot be scored (no
    declared target, no bounce, or an unclassifiable bounce); ``distance_m``
    is the miss distance to the target zone whenever the bounce landed
    somewhere measurable.
    """

    ball_no: int
    target_key: str | None
    hit: bool | None
    confidence: float
    pitch_x: float | None
    pitch_y: float | None
    distance_m: float | None
    reason: str | None

    def __post_init__(self) -> None:
        if (self.hit is None) and not self.reason:
            raise TrajectoryError("an unscored delivery requires a reason (never silent)")
        if not 0.0 <= self.confidence <= 1.0:
            raise TrajectoryError(f"confidence must be in [0, 1], got {self.confidence}")


def is_target_hit(line: Line, length: Length, target: TargetZone) -> bool:
    """The US-I4 hit predicate: the bounce classifies into the declared zone."""
    return line is target.line and length is target.length


def distance_to_target_m(
    config: ZoneConfig,
    pitch_x_m: float,
    pitch_y_m: float,
    target: TargetZone,
    handedness: Handedness = Handedness.RIGHT,
) -> float:
    """Miss distance (m) from a bounce point to the target zone (US-I4).

    Measured to the SAME half-open ``[lo, hi)`` bands
    :func:`cricai_vision.zones.classify` assigns with: a bounce exactly on a
    zone's FAR edge belongs to the farther band (the US-C5 edge rule), so its
    distance here is positive — one float below the edge, an infinitesimal
    but honest miss, never a contradictory "missed by 0.0 m" next to
    ``hit=False``. (The one asymmetry left is classify's own US-C5 clamp: up
    to ``clamp_epsilon_m`` of off-pitch calibration error still classifies,
    so such a clamped HIT may carry a small true positive distance.) Line
    channels are declared in right-hand terms (canonical frame): a left-hand
    batter mirrors the lateral axis, exactly as
    :func:`cricai_vision.zones.classify_line` does.
    """
    if not math.isfinite(pitch_x_m) or not math.isfinite(pitch_y_m):
        raise TrajectoryError(f"invalid bounce point: ({pitch_x_m}, {pitch_y_m})")
    lateral = pitch_y_m if handedness is Handedness.RIGHT else -pitch_y_m
    x_lo, x_hi = config.length_bands_m[target.length]
    y_lo, y_hi = config.line_channels_m[target.line]
    # Half-open bands: the upper edge is exterior, so the nearest in-zone
    # point sits one representable float inside it (nextafter toward lo).
    nearest_x = min(max(pitch_x_m, x_lo), math.nextafter(x_hi, x_lo))
    nearest_y = min(max(lateral, y_lo), math.nextafter(y_hi, y_lo))
    return math.hypot(pitch_x_m - nearest_x, lateral - nearest_y)


def score_delivery(
    config: ZoneConfig,
    *,
    ball_no: int,
    target: TargetZone | None,
    bounce: BouncePoint | None,
    handedness: Handedness = Handedness.RIGHT,
) -> DeliveryScore:
    """Score one delivery against its declared target (US-I4).

    Reuses the US-C5 zone classifier — the same config vocabulary the bounce
    pipeline writes with — so target scoring can never disagree with the
    pitch map. Honest outcomes: no declared target, no bounce, or a bounce
    outside the classifiable pitch area score as None-with-reason, never a
    guessed miss.
    """
    confidence = bounce.confidence if bounce is not None else 0.0
    pitch_x = bounce.pitch_x if bounce is not None else None
    pitch_y = bounce.pitch_y if bounce is not None else None
    if target is None:
        return DeliveryScore(
            ball_no=ball_no,
            target_key=None,
            hit=None,
            confidence=confidence,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            distance_m=None,
            reason="no declared target for this delivery",
        )
    if bounce is None:
        return DeliveryScore(
            ball_no=ball_no,
            target_key=target.key,
            hit=None,
            confidence=0.0,
            pitch_x=None,
            pitch_y=None,
            distance_m=None,
            reason="no bounce estimate for this delivery (full toss or tracking failure)",
        )
    distance = distance_to_target_m(config, bounce.pitch_x, bounce.pitch_y, target, handedness)
    try:
        line, length = classify(config, bounce.pitch_x, bounce.pitch_y, handedness)
    except ZoneConfigError as exc:
        return DeliveryScore(
            ball_no=ball_no,
            target_key=target.key,
            hit=None,
            confidence=confidence,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            distance_m=distance,
            reason=f"bounce is outside the classifiable pitch area: {exc}",
        )
    return DeliveryScore(
        ball_no=ball_no,
        target_key=target.key,
        hit=is_target_hit(line, length, target),
        confidence=confidence,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        distance_m=distance,
        reason=None,
    )


@dataclass(frozen=True)
class ScatterPoint:
    """One confident scored bounce behind a target's hit rate (zone scatter)."""

    ball_no: int
    pitch_x: float
    pitch_y: float
    hit: bool
    distance_m: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "ball_no": self.ball_no,
            "pitch_x": self.pitch_x,
            "pitch_y": self.pitch_y,
            "hit": self.hit,
            "distance_m": self.distance_m,
        }


@dataclass(frozen=True)
class TargetAccuracy:
    """One target's scorecard row: hit rate over an explicit denominator."""

    target_key: str
    attempts: int  # the denominator: confident, scored deliveries only
    hits: int
    hit_rate: float | None  # None when there is no denominator — never 0%-by-default
    low_confidence: int  # scored but below the confidence gate (excluded, shown)
    unscored: int  # no bounce / unclassifiable (excluded, shown)
    scatter: tuple[ScatterPoint, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "attempts": self.attempts,
            "hits": self.hits,
            "hit_rate": self.hit_rate,
            "low_confidence": self.low_confidence,
            "unscored": self.unscored,
            "scatter": [point.to_payload() for point in self.scatter],
        }


@dataclass(frozen=True)
class Scorecard:
    """Per-target accuracy over one block/session of scored deliveries."""

    min_confidence: float
    targets: tuple[TargetAccuracy, ...]
    untargeted: int  # deliveries bowled with no declared target
    total: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "min_confidence": self.min_confidence,
            "targets": [target.to_payload() for target in self.targets],
            "untargeted": self.untargeted,
            "total": self.total,
        }


@dataclass
class _Tally:
    """Mutable per-target accumulator behind :func:`accuracy_scorecard`."""

    attempts: int = 0
    hits: int = 0
    low_confidence: int = 0
    unscored: int = 0
    scatter: list[ScatterPoint] = field(default_factory=list)


def accuracy_scorecard(
    scores: Sequence[DeliveryScore],
    *,
    min_confidence: float = DEFAULT_MIN_BOUNCE_CONFIDENCE,
) -> Scorecard:
    """Aggregate delivery scores into per-target hit rates (US-I4).

    The denominator (``attempts``) counts ONLY deliveries scored with a
    confident bounce (``confidence >= min_confidence``); every excluded
    delivery is still shown (``low_confidence``/``unscored``/``untargeted``)
    so the accuracy percentage can never quietly inflate. Targets keep the
    order of first appearance.
    """
    if not 0.0 <= min_confidence <= 1.0:
        raise TrajectoryError(f"min_confidence must be in [0, 1], got {min_confidence}")
    tallies: dict[str, _Tally] = {}
    untargeted = 0
    for score in scores:
        if score.target_key is None:
            untargeted += 1
            continue
        tally = tallies.setdefault(score.target_key, _Tally())
        if score.hit is None:
            tally.unscored += 1
        elif score.confidence < min_confidence:
            tally.low_confidence += 1
        else:
            tally.attempts += 1
            tally.hits += 1 if score.hit else 0
            tally.scatter.append(
                ScatterPoint(
                    ball_no=score.ball_no,
                    # Scored deliveries always carry a bounce and its distance.
                    pitch_x=cast(float, score.pitch_x),
                    pitch_y=cast(float, score.pitch_y),
                    hit=score.hit,
                    distance_m=cast(float, score.distance_m),
                )
            )
    targets = tuple(
        TargetAccuracy(
            target_key=key,
            attempts=tally.attempts,
            hits=tally.hits,
            hit_rate=tally.hits / tally.attempts if tally.attempts else None,
            low_confidence=tally.low_confidence,
            unscored=tally.unscored,
            scatter=tuple(tally.scatter),
        )
        for key, tally in tallies.items()
    )
    return Scorecard(
        min_confidence=min_confidence,
        targets=targets,
        untargeted=untargeted,
        total=len(scores),
    )
