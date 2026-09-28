"""Ball tracking (US-F3): constant-velocity Kalman filter + gap bridging.

Consumes ball-class :class:`~cricai_vision.detect.Detection` streams (the
caller filters labels; decoy balls in the net are still ball class) and
produces one :class:`TrackResult` per ball window:

* **Association** — per frame, the nearest detection within a gating distance
  of the Kalman prediction extends the track; everything else (decoy balls,
  identity jumps) is rejected. The gate widens with consecutive misses (up to
  ``reacquire_gate_factor``) so the track re-acquires after occlusion without
  ever accepting a far-away second ball.
* **Seeding** — every detection not already claimed by an earlier candidate
  seeds a greedy candidate track; the candidate with the greatest pixel path
  length wins (a delivered ball traverses the frame; balls lying in the net
  are static). A best path below ``min_path_px`` means no ball was tracked
  and an honest empty result is returned.
* **Gap bridging** — interior gaps of at most :data:`MAX_BRIDGE_FRAMES`
  missing frames are filled by constant-velocity interpolation between the
  surrounding detections (``bridged=true``, score 0). Longer gaps are never
  filled (US-F3 AC: flagged rather than hallucinated): they split segments
  and set the ``long_gap`` flag.
* **Identity risk** — frames where another ball detection sits within
  ``ambiguity_radius_px`` of the accepted track point are ambiguous; when at
  least ``identity_persist_frames`` such frames occur (a persistent second
  ball near the trajectory) the ``identity_risk`` flag is set. Static decoys
  far from the flight never trigger it.
* **Segments** — a phase machine labels points pre_bounce → post_bounce (at
  the vertical-velocity sign flip; image y grows downward) → post_contact
  (after a sustained horizontal direction reversal — the ball coming back off
  the bat). The phase persists across long gaps, and a run that starts
  already ascending while still pre_bounce implies the bounce happened inside
  the gap. A reversal can also fire before any bounce (full toss).
* **Pitch enrichment** — when the caller provides the camera's pixel→pitch
  homography (:class:`~cricai_vision.extrinsics.PlaneCalibration`, US-C2),
  every point gains ``pitch_x/pitch_y`` meters; raw pixels are mapped as-is
  (intrinsic undistortion of track points is deferred — bounce classification
  itself is US-F4's). Without one, points stay pixel-only and the
  ``pitch_mapped`` flag is false.

``TrackResult.to_payload`` emits the pinned cross-group contract stored at
``sessions/{sid}/balls/{n}/track-{camera}.json`` (f3 writes; f4/f5 read).
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Literal

import numpy as np
import numpy.typing as npt
from cricai_data.enums import LabelClass

from cricai_vision import extrinsics
from cricai_vision.detect import Detection

#: Provenance identifier recorded on ``ball_tracks`` rows (tracker_version).
TRACKER_VERSION: Final = "cv-kalman-1"

#: US-F3 AC pin: occlusion gaps of at most this many missing frames are
#: bridged by prediction; longer gaps split segments and set ``long_gap``.
MAX_BRIDGE_FRAMES: Final = 5

SegmentKind = Literal["pre_bounce", "post_bounce", "post_contact"]

PRE_BOUNCE: Final[SegmentKind] = "pre_bounce"
POST_BOUNCE: Final[SegmentKind] = "post_bounce"
POST_CONTACT: Final[SegmentKind] = "post_contact"


class TrackError(ValueError):
    """Invalid tracking input (bad window, non-ball detections, bad config)."""


@dataclass(frozen=True)
class TrackingWindow:
    """One ball-event clip window on one camera (video-timeline ms).

    ``first_frame``/``n_frames`` mirror the frame grid a
    :class:`~cricai_vision.detect.DetectionProvider` emits for the same
    window, so coverage is measured against exactly the frames the detector
    saw. ``width``/``height`` convert the provider's normalized boxes to the
    pixel coordinates the payload contract pins.
    """

    start_ms: float
    end_ms: float
    fps: float
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise TrackError(f"empty window: start_ms={self.start_ms}, end_ms={self.end_ms}")
        if self.fps <= 0:
            raise TrackError(f"fps must be positive, got {self.fps!r}")
        if self.width <= 0 or self.height <= 0:
            raise TrackError(f"frame size must be positive, got {self.width}x{self.height}")

    @property
    def frame_ms(self) -> float:
        return 1000.0 / self.fps

    @property
    def first_frame(self) -> int:
        return round(self.start_ms / self.frame_ms)

    @property
    def n_frames(self) -> int:
        return int((self.end_ms - self.start_ms) / self.frame_ms) + 1

    @property
    def last_frame(self) -> int:
        return self.first_frame + self.n_frames - 1

    def frame_ts_ms(self, frame_no: int) -> float:
        """Video-timeline timestamp of one frame slot of this window."""
        return self.start_ms + (frame_no - self.first_frame) * self.frame_ms


@dataclass(frozen=True)
class TrackerConfig:
    """Association/segmentation knobs; defaults sized for 1080p footage."""

    gate_px: float = 80.0
    reacquire_gate_factor: float = 3.0
    ambiguity_radius_px: float = 160.0
    identity_persist_frames: int = 3
    measurement_noise_px: float = 2.0
    process_noise_px: float = 3.0
    min_path_px: float = 40.0
    min_bounce_speed_px: float = 0.5
    min_reversal_px: float = 10.0

    def __post_init__(self) -> None:
        strictly_positive = (
            ("gate_px", self.gate_px),
            ("ambiguity_radius_px", self.ambiguity_radius_px),
            ("measurement_noise_px", self.measurement_noise_px),
            ("process_noise_px", self.process_noise_px),
            ("min_reversal_px", self.min_reversal_px),
        )
        for name, value in strictly_positive:
            if value <= 0:
                raise TrackError(f"{name} must be > 0, got {value!r}")
        if self.reacquire_gate_factor < 1.0:
            raise TrackError(
                f"reacquire_gate_factor must be >= 1, got {self.reacquire_gate_factor!r}"
            )
        if self.identity_persist_frames < 1:
            raise TrackError(
                f"identity_persist_frames must be >= 1, got {self.identity_persist_frames!r}"
            )
        if self.min_path_px < 0:
            raise TrackError(f"min_path_px must be >= 0, got {self.min_path_px!r}")
        if self.min_bounce_speed_px < 0:
            raise TrackError(f"min_bounce_speed_px must be >= 0, got {self.min_bounce_speed_px!r}")


@dataclass(frozen=True)
class TrackPoint:
    """One tracked ball position on one frame (pixels on this camera)."""

    frame_no: int
    ts_ms: float
    px_x: float
    px_y: float
    score: float
    bridged: bool
    pitch_x: float | None = None
    pitch_y: float | None = None

    def to_payload(self, *, pitch_mapped: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "frame_no": self.frame_no,
            "ts_ms": self.ts_ms,
            "px_x": self.px_x,
            "px_y": self.px_y,
            "score": self.score,
            "bridged": self.bridged,
        }
        if pitch_mapped:
            payload["pitch_x"] = self.pitch_x
            payload["pitch_y"] = self.pitch_y
        return payload


@dataclass(frozen=True)
class TrackSegment:
    """One flight phase with per-segment confidence (US-F3)."""

    kind: SegmentKind
    start_ms: float
    end_ms: float
    confidence: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class TrackResult:
    """One ball's tracked trajectory on one camera.

    ``coverage`` is the fraction of the window's frame slots holding a point
    (detected or bridged); ``confidence`` scales the mean detection score by
    coverage. Both are 0.0 for an empty track (no ball found).
    """

    points: tuple[TrackPoint, ...]
    segments: tuple[TrackSegment, ...]
    coverage: float
    confidence: float
    identity_risk: bool
    long_gap: bool
    pitch_mapped: bool

    @property
    def flags(self) -> dict[str, bool]:
        return {
            "identity_risk": self.identity_risk,
            "long_gap": self.long_gap,
            "pitch_mapped": self.pitch_mapped,
        }

    def segments_payload(self) -> list[dict[str, Any]]:
        return [segment.to_payload() for segment in self.segments]

    def to_payload(self) -> dict[str, Any]:
        """Pinned US-F3 payload (``sessions/{sid}/balls/{n}/track-{camera}.json``)."""
        return {
            "points": [point.to_payload(pitch_mapped=self.pitch_mapped) for point in self.points],
            "segments": self.segments_payload(),
            "flags": self.flags,
        }


@dataclass(frozen=True)
class _Obs:
    """A ball detection in pixel coordinates, keyed for candidate claiming."""

    key: int
    frame_no: int
    ts_ms: float
    x: float
    y: float
    score: float


@dataclass(frozen=True)
class _Candidate:
    """One greedy candidate track and its selection/risk statistics."""

    accepted: tuple[_Obs, ...]
    path_px: float
    ambiguous_frames: int


_MEASURE: Final[npt.NDArray[np.float64]] = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])


class _Kalman:
    """Constant-velocity Kalman filter in pixel coordinates (dt = 1 frame)."""

    def __init__(self, seed: _Obs, config: TrackerConfig) -> None:
        r2 = config.measurement_noise_px**2
        v0 = config.gate_px**2  # a speed of one gate per frame is plausible at init
        q = config.process_noise_px**2
        self._state: npt.NDArray[np.float64] = np.array([seed.x, seed.y, 0.0, 0.0])
        self._cov: npt.NDArray[np.float64] = np.diag([r2, r2, v0, v0])
        self._r: npt.NDArray[np.float64] = np.eye(2) * r2
        self._q: npt.NDArray[np.float64] = np.array(
            [
                [0.25 * q, 0.0, 0.5 * q, 0.0],
                [0.0, 0.25 * q, 0.0, 0.5 * q],
                [0.5 * q, 0.0, q, 0.0],
                [0.0, 0.5 * q, 0.0, q],
            ]
        )
        self._f: npt.NDArray[np.float64] = np.array(
            [
                [1.0, 0.0, 1.0, 0.0],
                [0.0, 1.0, 0.0, 1.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )

    def predict(self) -> None:
        self._state = self._f @ self._state
        self._cov = self._f @ self._cov @ self._f.T + self._q

    @property
    def position(self) -> tuple[float, float]:
        return (float(self._state[0]), float(self._state[1]))

    def update(self, obs: _Obs) -> None:
        innovation = np.array([obs.x, obs.y]) - _MEASURE @ self._state
        residual_cov = _MEASURE @ self._cov @ _MEASURE.T + self._r
        gain = self._cov @ _MEASURE.T @ np.linalg.inv(residual_cov)
        self._state = self._state + gain @ innovation
        self._cov = (np.eye(4) - gain @ _MEASURE) @ self._cov


def _to_observations(detections: Sequence[Detection], window: TrackingWindow) -> list[_Obs]:
    """Validate + convert normalized detections to pixel observations, sorted."""
    staged: list[tuple[int, float, float, float, float]] = []
    for detection in detections:
        if detection.label is not LabelClass.BALL:
            raise TrackError(
                f"tracker consumes ball detections only, got {detection.label} "
                f"on frame {detection.frame_no} (the caller filters labels)"
            )
        if not window.first_frame <= detection.frame_no <= window.last_frame:
            raise TrackError(
                f"detection frame {detection.frame_no} outside window frames "
                f"[{window.first_frame}, {window.last_frame}]"
            )
        staged.append(
            (
                detection.frame_no,
                detection.cx * window.width,
                detection.cy * window.height,
                detection.ts_ms,
                detection.score,
            )
        )
    staged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        _Obs(key=key, frame_no=frame_no, ts_ms=ts_ms, x=x, y=y, score=score)
        for key, (frame_no, x, y, ts_ms, score) in enumerate(staged)
    ]


def _associate(
    frame_obs: Sequence[_Obs],
    predicted: tuple[float, float],
    missed: int,
    config: TrackerConfig,
) -> _Obs | None:
    """Nearest in-gate observation; the gate widens with consecutive misses."""
    gate = config.gate_px * min(1.0 + 0.5 * missed, config.reacquire_gate_factor)
    px, py = predicted
    best: _Obs | None = None
    best_dist = math.inf
    for obs in frame_obs:
        dist = math.hypot(obs.x - px, obs.y - py)
        if dist <= gate and dist < best_dist:
            best, best_dist = obs, dist
    return best


def _ambiguous(frame_obs: Sequence[_Obs], chosen: _Obs, config: TrackerConfig) -> int:
    """1 when another same-frame detection sits near the accepted point."""
    near = any(
        obs.key != chosen.key
        and math.hypot(obs.x - chosen.x, obs.y - chosen.y) <= config.ambiguity_radius_px
        for obs in frame_obs
    )
    return 1 if near else 0


def _run_candidate(
    seed: _Obs,
    by_frame: dict[int, list[_Obs]],
    window: TrackingWindow,
    config: TrackerConfig,
) -> _Candidate:
    """Greedy Kalman association from one seed detection to the window end."""
    kalman = _Kalman(seed, config)
    accepted = [seed]
    ambiguous = _ambiguous(by_frame[seed.frame_no], seed, config)
    path_px = 0.0
    missed = 0
    last = seed
    for frame_no in range(seed.frame_no + 1, window.last_frame + 1):
        kalman.predict()
        best = _associate(by_frame.get(frame_no, ()), kalman.position, missed, config)
        if best is None:
            missed += 1
            continue
        kalman.update(best)
        ambiguous += _ambiguous(by_frame[frame_no], best, config)
        path_px += math.hypot(best.x - last.x, best.y - last.y)
        accepted.append(best)
        last = best
        missed = 0
    return _Candidate(tuple(accepted), path_px, ambiguous)


def _best_candidate(
    obs_list: Sequence[_Obs],
    by_frame: dict[int, list[_Obs]],
    window: TrackingWindow,
    config: TrackerConfig,
) -> _Candidate | None:
    """Seed a candidate per unclaimed detection; the longest pixel path wins.

    A delivered ball traverses the frame while decoy balls lying in the net
    are static, so path length separates them without ever trusting scores.
    Ties keep the earliest seed (deterministic).
    """
    claimed: set[int] = set()
    best: _Candidate | None = None
    for obs in obs_list:
        if obs.key in claimed:
            continue
        candidate = _run_candidate(obs, by_frame, window, config)
        claimed.update(accepted.key for accepted in candidate.accepted)
        if best is None or (candidate.path_px, len(candidate.accepted)) > (
            best.path_px,
            len(best.accepted),
        ):
            best = candidate
    return best


def _point(obs: _Obs) -> TrackPoint:
    return TrackPoint(obs.frame_no, obs.ts_ms, obs.x, obs.y, obs.score, bridged=False)


def _bridge(prev: _Obs, nxt: _Obs, window: TrackingWindow) -> list[TrackPoint]:
    """Constant-velocity fill for a short occlusion gap (US-F3 AC, <= 5 frames)."""
    span = nxt.frame_no - prev.frame_no
    out: list[TrackPoint] = []
    for frame_no in range(prev.frame_no + 1, nxt.frame_no):
        fraction = (frame_no - prev.frame_no) / span
        out.append(
            TrackPoint(
                frame_no=frame_no,
                ts_ms=window.frame_ts_ms(frame_no),
                px_x=prev.x + (nxt.x - prev.x) * fraction,
                px_y=prev.y + (nxt.y - prev.y) * fraction,
                score=0.0,
                bridged=True,
            )
        )
    return out


def _build_points(
    accepted: Sequence[_Obs], window: TrackingWindow
) -> tuple[list[TrackPoint], set[int], bool]:
    """Points (accepted + bridged), indices starting a new run, long_gap flag.

    Gaps of more than :data:`MAX_BRIDGE_FRAMES` missing frames are never
    filled — the next point starts a new run and ``long_gap`` is set.
    """
    points: list[TrackPoint] = [_point(accepted[0])]
    run_starts: set[int] = set()
    long_gap = False
    prev = accepted[0]
    for obs in accepted[1:]:
        missing = obs.frame_no - prev.frame_no - 1
        if 0 < missing <= MAX_BRIDGE_FRAMES:
            points.extend(_bridge(prev, obs, window))
        elif missing > MAX_BRIDGE_FRAMES:
            long_gap = True
            run_starts.add(len(points))
        points.append(_point(obs))
        prev = obs
    return points, run_starts, long_gap


def _runs(points: Sequence[TrackPoint], run_starts: set[int]) -> list[list[TrackPoint]]:
    runs: list[list[TrackPoint]] = [[]]
    for index, point in enumerate(points):
        if index in run_starts:
            runs.append([])
        runs[-1].append(point)
    return runs


class _PhaseMachine:
    """pre_bounce -> post_bounce -> post_contact over point-pair velocities.

    Bounce: vertical velocity flips from descending (+y, image coordinates)
    to ascending — the last significant delta (``|dy| > min_bounce_speed_px``)
    was descending and the current one ascends. Near-zero deltas straddling
    the impact (descent and rebound cancelling inside one frame interval or
    a bridged gap) neither flip the phase nor mask the flip, so the vertex
    stays in pre_bounce and the first significant ascent opens post_bounce.
    Contact: sustained horizontal reversal against the track's dominant
    direction (total >= ``min_reversal_px`` without an intervening forward
    step); it may fire before any bounce (full toss). State persists across
    long-gap runs; a run starting already ascending while still pre_bounce
    means the ball bounced inside the gap.
    """

    def __init__(self, config: TrackerConfig) -> None:
        self._config = config
        self.phase: SegmentKind = PRE_BOUNCE
        self._dominant = 0.0
        self._travel = 0.0
        self._reversal = 0.0
        self._last_significant_dy: float | None = None

    def start_run(self, first_pair_dy: float | None) -> None:
        self._last_significant_dy = None
        if (
            self.phase == PRE_BOUNCE
            and first_pair_dy is not None
            and first_pair_dy < -self._config.min_bounce_speed_px
        ):
            self.phase = POST_BOUNCE  # already rising: the bounce happened in the gap

    def step(self, dx: float, dy: float) -> None:
        """Advance past one intra-run point pair; may advance the phase."""
        self._update_dominant(dx)
        if (
            self.phase == PRE_BOUNCE
            and self._last_significant_dy is not None
            and self._last_significant_dy > self._config.min_bounce_speed_px
            and dy < -self._config.min_bounce_speed_px
        ):
            self.phase = POST_BOUNCE
        self._check_contact(dx)
        if abs(dy) > self._config.min_bounce_speed_px:
            self._last_significant_dy = dy

    def _update_dominant(self, dx: float) -> None:
        if self._dominant == 0.0:
            self._travel += dx
            if abs(self._travel) >= self._config.min_reversal_px:
                self._dominant = math.copysign(1.0, self._travel)

    def _check_contact(self, dx: float) -> None:
        if self.phase == POST_CONTACT or self._dominant == 0.0:
            return
        if dx * self._dominant < 0:
            self._reversal += abs(dx)
            if self._reversal >= self._config.min_reversal_px:
                self.phase = POST_CONTACT
        elif dx * self._dominant > 0:
            self._reversal = 0.0


def _label_run(run: Sequence[TrackPoint], machine: _PhaseMachine) -> list[SegmentKind]:
    """Phase label per point of one run, advancing the shared machine."""
    deltas = [(b.px_x - a.px_x, b.px_y - a.px_y) for a, b in itertools.pairwise(run)]
    machine.start_run(deltas[0][1] if deltas else None)
    labels = [machine.phase]
    for dx, dy in deltas:
        machine.step(dx, dy)
        labels.append(machine.phase)
    return labels


def _segment_confidence(points: Sequence[TrackPoint]) -> float:
    """Detected-frame density times mean detection score, in [0, 1]."""
    detected = [point for point in points if not point.bridged]
    if not detected:
        return 0.0  # a sliver carried entirely by bridged prediction
    span = points[-1].frame_no - points[0].frame_no + 1
    mean_score = sum(point.score for point in detected) / len(detected)
    return (len(detected) / span) * mean_score


def _run_segments(run: Sequence[TrackPoint], labels: Sequence[SegmentKind]) -> list[TrackSegment]:
    """Group one run's consecutively same-labeled points into segments."""
    segments: list[TrackSegment] = []
    start = 0
    for index in range(1, len(run) + 1):
        if index == len(run) or labels[index] != labels[start]:
            chunk = run[start:index]
            segments.append(
                TrackSegment(
                    kind=labels[start],
                    start_ms=chunk[0].ts_ms,
                    end_ms=chunk[-1].ts_ms,
                    confidence=_segment_confidence(chunk),
                )
            )
            start = index
    return segments


def _enrich_pitch(
    points: Sequence[TrackPoint], calibration: extrinsics.PlaneCalibration
) -> tuple[TrackPoint, ...]:
    """Map every point's raw pixel through the pixel->pitch homography (US-C2).

    A point on the mapping horizon keeps ``pitch_x/pitch_y = None`` — never a
    fabricated coordinate.
    """
    enriched: list[TrackPoint] = []
    for point in points:
        try:
            pitch_x, pitch_y = extrinsics.pixel_to_pitch_xy(calibration, (point.px_x, point.px_y))
        except extrinsics.ExtrinsicsError:
            enriched.append(point)
            continue
        enriched.append(replace(point, pitch_x=pitch_x, pitch_y=pitch_y))
    return tuple(enriched)


def _empty_result(pitch_mapped: bool) -> TrackResult:
    return TrackResult(
        points=(),
        segments=(),
        coverage=0.0,
        confidence=0.0,
        identity_risk=False,
        long_gap=False,
        pitch_mapped=pitch_mapped,
    )


def track_ball(
    detections: Sequence[Detection],
    window: TrackingWindow,
    *,
    calibration: extrinsics.PlaneCalibration | None = None,
    config: TrackerConfig | None = None,
) -> TrackResult:
    """Track one ball through its window of ball-class detections.

    Raises :class:`TrackError` for non-ball detections, detections outside
    the window's frame grid, or an invalid window/config. Returns an honest
    empty result (coverage/confidence 0) when no moving ball is found.
    """
    config = config if config is not None else TrackerConfig()
    obs_list = _to_observations(detections, window)
    by_frame: dict[int, list[_Obs]] = defaultdict(list)
    for obs in obs_list:
        by_frame[obs.frame_no].append(obs)
    best = _best_candidate(obs_list, by_frame, window, config)
    if best is None or best.path_px < config.min_path_px:
        return _empty_result(calibration is not None)
    points, run_starts, long_gap = _build_points(best.accepted, window)
    machine = _PhaseMachine(config)
    segments: list[TrackSegment] = []
    for run in _runs(points, run_starts):
        segments.extend(_run_segments(run, _label_run(run, machine)))
    scores = [point.score for point in points if not point.bridged]
    coverage = len(points) / window.n_frames
    confidence = coverage * (sum(scores) / len(scores))
    enriched = tuple(points) if calibration is None else _enrich_pitch(points, calibration)
    return TrackResult(
        points=enriched,
        segments=tuple(segments),
        coverage=coverage,
        confidence=confidence,
        identity_risk=best.ambiguous_frames >= config.identity_persist_frames,
        long_gap=long_gap,
        pitch_mapped=calibration is not None,
    )
