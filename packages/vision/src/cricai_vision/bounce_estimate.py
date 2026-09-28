"""Bounce-vertex estimation from a tracked ball trajectory (US-F4).

Pure function over the pinned US-F3 track payload
(``sessions/{sid}/balls/{n}/track-{camera}.json``: ``points`` are PIXEL
coordinates on one camera, ``segments`` carry ``pre_bounce``/``post_bounce``/
``post_contact`` windows with confidence, ``flags`` carry ``identity_risk``/
``long_gap``):

* The bounce is the ``pre_bounce``/``post_bounce`` segment boundary refined by
  the vertical-velocity sign change nearest that boundary (image y grows
  downward, so the ball descends with ``vy > 0`` and rebounds with ``vy < 0``;
  the vertex is the local px_y maximum between the two). A long occlusion gap
  legally splits one phase into several same-kind segments (pinned US-F3
  behavior), so the boundary sits between the LAST ``pre_bounce`` window and
  the FIRST ``post_bounce`` window.
* Confidence starts at the weaker of the two segment confidences and degrades
  with every bridged (gap-interpolated) point near the vertex, with the
  track-level ``long_gap`` flag, and when no sign change refines the boundary.
* No ``pre_bounce``/``post_bounce`` boundary (a full toss, or tracking failed
  before the pitch) returns ``None`` WITH a reason — a bounce is never
  fabricated (US-F4 AC).

Pitch mapping is optional: a provided pixel->pitch homography
(:class:`~cricai_vision.extrinsics.PlaneCalibration`, the US-C2 plane fit over
the :mod:`cricai_vision.geometry` frame) maps the vertex pixel to pitch-frame
meters; without one the estimate stays in pixel coordinates.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from cricai_vision.extrinsics import ExtrinsicsError, PlaneCalibration, pixel_to_pitch_xy

#: Estimator version recorded in ``bounce_estimates.tracker_version`` provenance.
ESTIMATOR_VERSION: Final = "bounce-est-1"

#: Points this close (video-timeline ms) to the segment boundary are searched
#: for the vertical-velocity sign change; bridged points this close to the
#: chosen vertex degrade confidence.
VERTEX_WINDOW_MS: Final = 150.0

#: Fewest tracked points that can bracket a velocity sign change.
MIN_POINTS: Final = 3

#: Confidence multiplier per bridged (interpolated) point near the vertex.
BRIDGED_CONFIDENCE_FACTOR: Final = 0.85

#: Confidence multiplier when the track carries the ``long_gap`` flag.
LONG_GAP_CONFIDENCE_FACTOR: Final = 0.7

#: Confidence multiplier when no sign change refines the segment boundary.
UNREFINED_CONFIDENCE_FACTOR: Final = 0.8

#: Segment kinds pinned by the US-F3 track payload contract.
SEGMENT_KINDS: Final = ("pre_bounce", "post_bounce", "post_contact")


class TrackPayloadError(ValueError):
    """Malformed track payload (violates the pinned US-F3 contract)."""


@dataclass(frozen=True)
class TrackPoint:
    """One tracked ball position (pixel coordinates on one camera)."""

    frame_no: int
    ts_ms: float
    px_x: float
    px_y: float
    score: float
    bridged: bool


@dataclass(frozen=True)
class TrackSegment:
    """One flight segment window with the tracker's confidence in it."""

    kind: str
    start_ms: float
    end_ms: float
    confidence: float


@dataclass(frozen=True)
class TrackPayload:
    """Validated track payload: points sorted by ``ts_ms``, segments, flags."""

    points: tuple[TrackPoint, ...]
    segments: tuple[TrackSegment, ...]
    identity_risk: bool
    long_gap: bool

    def segments_of(self, kind: str) -> tuple[TrackSegment, ...]:
        """All segments of one kind, in payload order (a long occlusion gap
        splits one flight phase into several same-kind segments, US-F3)."""
        return tuple(segment for segment in self.segments if segment.kind == kind)


@dataclass(frozen=True)
class EstimatedBounce:
    """The located bounce vertex; pitch coordinates only when a homography
    was provided (otherwise the estimate stays in pixel coordinates)."""

    frame_no: int
    ts_ms: float
    px_x: float
    px_y: float
    confidence: float
    pitch_x: float | None = None
    pitch_y: float | None = None


@dataclass(frozen=True)
class BounceResult:
    """Either an estimate or the reason none exists — exactly one is set."""

    estimate: EstimatedBounce | None
    reason: str | None

    def __post_init__(self) -> None:
        if (self.estimate is None) == (self.reason is None):
            raise ValueError("exactly one of estimate/reason must be set")


def _finite(raw: Mapping[str, Any], key: str, where: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TrackPayloadError(f"{where}.{key} must be a finite number, got {value!r}")
    return float(value)


def _flag(flags: Mapping[str, Any], key: str) -> bool:
    value = flags.get(key, False)
    if not isinstance(value, bool):
        raise TrackPayloadError(f"flags.{key} must be a boolean, got {value!r}")
    return value


def _parse_point(raw: object, index: int) -> TrackPoint:
    where = f"points[{index}]"
    if not isinstance(raw, Mapping):
        raise TrackPayloadError(f"{where} must be an object")
    frame_no = raw.get("frame_no")
    if isinstance(frame_no, bool) or not isinstance(frame_no, int):
        raise TrackPayloadError(f"{where}.frame_no must be an integer, got {frame_no!r}")
    bridged = raw.get("bridged", False)
    if not isinstance(bridged, bool):
        raise TrackPayloadError(f"{where}.bridged must be a boolean, got {bridged!r}")
    return TrackPoint(
        frame_no=frame_no,
        ts_ms=_finite(raw, "ts_ms", where),
        px_x=_finite(raw, "px_x", where),
        px_y=_finite(raw, "px_y", where),
        score=_finite(raw, "score", where),
        bridged=bridged,
    )


def _parse_segment(raw: object, index: int) -> TrackSegment:
    where = f"segments[{index}]"
    if not isinstance(raw, Mapping):
        raise TrackPayloadError(f"{where} must be an object")
    kind = raw.get("kind")
    if kind not in SEGMENT_KINDS:
        raise TrackPayloadError(f"{where}.kind must be one of {SEGMENT_KINDS}, got {kind!r}")
    start_ms = _finite(raw, "start_ms", where)
    end_ms = _finite(raw, "end_ms", where)
    if end_ms < start_ms:
        raise TrackPayloadError(f"{where} is empty: start_ms={start_ms}, end_ms={end_ms}")
    confidence = _finite(raw, "confidence", where)
    if not 0.0 <= confidence <= 1.0:
        raise TrackPayloadError(f"{where}.confidence must be within [0, 1], got {confidence}")
    return TrackSegment(kind=str(kind), start_ms=start_ms, end_ms=end_ms, confidence=confidence)


def _require_list(raw: object, name: str) -> Sequence[object]:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise TrackPayloadError(f"track payload field {name!r} must be a list")
    return raw


def parse_track_payload(raw: object) -> TrackPayload:
    """Validate a raw (JSON-decoded) track payload against the US-F3 contract.

    Points are returned sorted by ``ts_ms``; duplicate timestamps, non-finite
    numbers, unknown segment kinds and malformed flags all raise
    :class:`TrackPayloadError` — a payload violating the pinned contract is a
    caller bug, never a silent no-bounce. Several segments of the SAME kind
    are legal: a long occlusion gap splits a phase (pinned US-F3 behavior).
    """
    if not isinstance(raw, Mapping):
        raise TrackPayloadError(f"track payload must be an object, got {type(raw).__name__}")
    raw_points = _require_list(raw.get("points"), "points")
    points = sorted(
        (_parse_point(point, i) for i, point in enumerate(raw_points)),
        key=lambda point: point.ts_ms,
    )
    for previous, current in itertools.pairwise(points):
        if current.ts_ms <= previous.ts_ms:
            raise TrackPayloadError(f"duplicate point timestamp ts_ms={current.ts_ms}")
    raw_segments = _require_list(raw.get("segments"), "segments")
    segments = tuple(_parse_segment(segment, i) for i, segment in enumerate(raw_segments))
    flags = raw.get("flags", {})
    if not isinstance(flags, Mapping):
        raise TrackPayloadError("track payload field 'flags' must be an object")
    return TrackPayload(
        points=tuple(points),
        segments=segments,
        identity_risk=_flag(flags, "identity_risk"),
        long_gap=_flag(flags, "long_gap"),
    )


def _vertical_velocity(points: Sequence[TrackPoint], gap: int) -> float:
    """px_y velocity across the gap between points ``gap`` and ``gap + 1``."""
    a, b = points[gap], points[gap + 1]
    return (b.px_y - a.px_y) / (b.ts_ms - a.ts_ms)


def _vertex_index(points: Sequence[TrackPoint], boundary_ms: float) -> tuple[int, bool] | None:
    """Vertex point index near the boundary, plus whether a sign change refined it.

    Candidates are points inside :data:`VERTEX_WINDOW_MS` of the boundary whose
    incoming velocity is downward (``> 0``) and outgoing velocity upward
    (``< 0``); the one nearest the boundary wins (lowest index on an exact
    tie). Without any strict sign change the boundary-nearest point is used
    unrefined; without any point near the boundary there is nothing honest to
    return (``None`` — the track lost the ball around the pitch).
    """
    window = [
        i for i, point in enumerate(points) if abs(point.ts_ms - boundary_ms) <= VERTEX_WINDOW_MS
    ]
    if not window:
        return None
    candidates = [
        i
        for i in window
        if 0 < i < len(points) - 1
        and _vertical_velocity(points, i - 1) > 0.0
        and _vertical_velocity(points, i) < 0.0
    ]
    if candidates:
        return min(candidates, key=lambda i: abs(points[i].ts_ms - boundary_ms)), True
    return min(window, key=lambda i: abs(points[i].ts_ms - boundary_ms)), False


def _confidence(
    track: TrackPayload,
    pre: TrackSegment,
    post: TrackSegment,
    vertex: TrackPoint,
    refined: bool,
) -> float:
    """Weakest segment confidence, degraded by bridged-near-vertex/long-gap/unrefined."""
    confidence = min(pre.confidence, post.confidence)
    bridged_near = sum(
        1
        for point in track.points
        if point.bridged and abs(point.ts_ms - vertex.ts_ms) <= VERTEX_WINDOW_MS
    )
    confidence *= BRIDGED_CONFIDENCE_FACTOR**bridged_near
    if track.long_gap:
        confidence *= LONG_GAP_CONFIDENCE_FACTOR
    if not refined:
        confidence *= UNREFINED_CONFIDENCE_FACTOR
    return confidence


def estimate_bounce(payload: object, *, homography: PlaneCalibration | None = None) -> BounceResult:
    """Locate the bounce vertex of one tracked ball (US-F4).

    ``payload`` is a raw JSON-decoded track payload (validated via
    :func:`parse_track_payload`) or an already-parsed :class:`TrackPayload`.
    With a ``homography`` the vertex pixel is mapped to pitch-frame meters
    (:func:`cricai_vision.extrinsics.pixel_to_pitch_xy`); an unmappable vertex
    (mapping horizon) returns ``None``-with-reason rather than a wild point.
    Returns ``None``-with-reason — never a fabricated bounce — when the track
    has no ``pre_bounce``/``post_bounce`` boundary (full toss / tracking
    failure), too few points, or no points near the boundary.
    """
    track = payload if isinstance(payload, TrackPayload) else parse_track_payload(payload)
    if len(track.points) < MIN_POINTS:
        return BounceResult(
            None, f"too few tracked points ({len(track.points)}) to locate a bounce vertex"
        )
    pre_segments = track.segments_of("pre_bounce")
    post_segments = track.segments_of("post_bounce")
    if not pre_segments or not post_segments:
        return BounceResult(
            None,
            "no pre_bounce/post_bounce boundary in the track (full toss or tracking failure)",
        )
    # Long-gap splits repeat a kind: the bounce sits between the LAST
    # pre_bounce window and the FIRST post_bounce window.
    pre = max(pre_segments, key=lambda segment: segment.end_ms)
    post = min(post_segments, key=lambda segment: segment.start_ms)
    boundary_ms = (pre.end_ms + post.start_ms) / 2.0
    located = _vertex_index(track.points, boundary_ms)
    if located is None:
        return BounceResult(
            None,
            f"no tracked points within {VERTEX_WINDOW_MS:g} ms of the bounce boundary",
        )
    index, refined = located
    vertex = track.points[index]
    pitch_x: float | None = None
    pitch_y: float | None = None
    if homography is not None:
        try:
            pitch_x, pitch_y = pixel_to_pitch_xy(homography, (vertex.px_x, vertex.px_y))
        except ExtrinsicsError as exc:
            return BounceResult(None, f"bounce pixel is unmappable through the homography: {exc}")
    return BounceResult(
        EstimatedBounce(
            frame_no=vertex.frame_no,
            ts_ms=vertex.ts_ms,
            px_x=vertex.px_x,
            px_y=vertex.px_y,
            confidence=_confidence(track, pre, post, vertex, refined),
            pitch_x=pitch_x,
            pitch_y=pitch_y,
        ),
        None,
    )
