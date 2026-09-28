"""Release-frame detection and release-point measurement (US-I2/I3).

Deterministic geometry over a bowler :class:`~cricai_vision.pose.PoseTrack`
(C5 side-on view; extracted under the :mod:`cricai_vision.bowler_pose` prior):

* :func:`detect_release` — the release frame from wrist/arm kinematics. The
  bowling wrist traces an arc over the shoulder; release is taken at the arc's
  apex (the highest visible wrist position) among frames where the arm is
  actually swinging (wrist speed at or above the gate) and the wrist is above
  the shoulder. The ±2 frames @ 120 fps agreement with hand-labeled truth
  (US-I3 AC) is verified on synthetic kinematics here and re-verified on the
  deferred real-footage benchmark — the apex proxy is an initial target, not a
  final claim.
* :func:`release_height_cm` — release height above the pitch surface, in cm.
  Two seams, calibrated path first: a triangulated 3D ball track
  (:func:`cricai_vision.triangulate.release_height`, pitch-frame z in meters)
  when the stereo rig produced one; else the pose+scale path — wrist height
  above the ground reference (the lowest visible foot landmark; the front foot
  is planted at release) divided by the calibration ``px_per_cm``. Every miss
  is null-with-reason, never a fabricated height (US-I2 AC).

``hand_xy`` (the wrist image position at the release frame) is the per-ball
release-point primitive: the worker persists it via ball_metrics (contract #6)
and US-I3's consistency chart (i5's report) computes scatter statistics from
it — the scatter math itself does not live here.

Confidence values are deterministic heuristics (monotone in landmark
visibility and, for heights, the upstream estimate quality) — not calibrated
probabilities.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from cricai_vision.pose import PoseFrame, PoseTrack
from cricai_vision.triangulate import ReleaseHeight, Track3D
from cricai_vision.triangulate import release_height as release_height_3d

#: Null reasons (US-I2/I3: nullable-with-reason, never silent zeros).
REASON_EMPTY_TRACK: Final = "empty pose track"
REASON_WRIST_OCCLUDED: Final = "bowling wrist/shoulder below visibility threshold in every frame"
REASON_ARM_NEVER_OVERHEAD: Final = "bowling wrist never above the shoulder"
REASON_NO_ARM_SWING: Final = "no overhead frame reaches the arm-swing speed gate"
REASON_NO_RELEASE: Final = "no release frame detected"
REASON_NO_SCALE: Final = "no calibration scale"  # matches the US-E2 reason string
REASON_FEET_LOW: Final = "foot landmarks below visibility threshold at release"
REASON_HAND_BELOW_GROUND: Final = "implausible release height (hand at or below ground reference)"

#: Wrist speed (px/ms) at or above which the arm counts as swinging. At 120 fps
#: this is ~4 px/frame — a walked-back run-up stays below it, a delivery arc is
#: far above it. Initial target; tune on the release-frame benchmark (US-I3).
DEFAULT_MIN_WRIST_SPEED_PX_PER_MS: Final = 0.5

#: Speed multiple of the gate at which the speed confidence factor saturates.
_SPEED_SATURATION: Final = 2.0

BowlingArm = Literal["right", "left"]


@dataclass(frozen=True)
class ReleaseConfig:
    """Knobs for release detection: bowling arm, visibility and swing gates."""

    arm: BowlingArm = "right"
    min_visibility: float = 0.5
    min_wrist_speed_px_per_ms: float = DEFAULT_MIN_WRIST_SPEED_PX_PER_MS

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_visibility <= 1.0:
            raise ValueError(f"min_visibility must be in [0, 1], got {self.min_visibility}")
        if self.min_wrist_speed_px_per_ms <= 0:
            raise ValueError(
                f"min_wrist_speed_px_per_ms must be positive, got {self.min_wrist_speed_px_per_ms}"
            )

    @property
    def wrist(self) -> str:
        return f"{self.arm}_wrist"

    @property
    def shoulder(self) -> str:
        return f"{self.arm}_shoulder"


DEFAULT_RELEASE_CONFIG: Final = ReleaseConfig()


@dataclass(frozen=True)
class ReleaseDetection:
    """One ball's detected release (or the null path, with its reason).

    ``release_frame`` indexes the pose track's frames; ``release_ms`` is
    track-relative (``frame / fps * 1000``) — callers anchor it to the session
    timeline via the ball event's window start. ``hand_xy`` is the bowling
    wrist's image position (px) at the release frame — the US-I3 scatter
    primitive.
    """

    release_frame: int | None
    release_ms: float | None
    hand_xy: tuple[float, float] | None
    confidence: float
    reason: str | None


def _null_detection(reason: str) -> ReleaseDetection:
    return ReleaseDetection(
        release_frame=None, release_ms=None, hand_xy=None, confidence=0.0, reason=reason
    )


def _wrist_speeds(track: PoseTrack, config: ReleaseConfig) -> list[float | None]:
    """Per-frame wrist speed (px/ms): the larger visible neighbor difference.

    ``None`` marks frames where no adjacent interval has both wrist endpoints
    visible — speed there is unknown, never assumed zero.
    """
    ms_per_frame = 1000.0 / track.fps
    steps: list[float | None] = []  # steps[i] = speed between frame i and i+1
    for prev, cur in zip(track.frames, track.frames[1:], strict=False):
        a, b = prev.landmark(config.wrist), cur.landmark(config.wrist)
        if min(a.visibility, b.visibility) < config.min_visibility:
            steps.append(None)
            continue
        steps.append(
            math.hypot(b.image_xy[0] - a.image_xy[0], b.image_xy[1] - a.image_xy[1]) / ms_per_frame
        )
    speeds: list[float | None] = []
    for i in range(len(track.frames)):
        before = steps[i - 1] if i > 0 else None
        after = steps[i] if i < len(steps) else None
        candidates = [s for s in (before, after) if s is not None]
        speeds.append(max(candidates) if candidates else None)
    return speeds


def detect_release(
    track: PoseTrack, *, config: ReleaseConfig = DEFAULT_RELEASE_CONFIG
) -> ReleaseDetection:
    """Detect the release frame from wrist/arm kinematics (US-I3, ±2f @ 120 fps).

    A frame qualifies when the bowling wrist and shoulder are visible, the
    wrist is above the shoulder (image y smaller — the delivery arc's overhead
    portion), and the wrist speed clears the swing gate. Among qualifying
    frames the arc apex (minimum wrist image y; earliest on a tie) is release.
    ``confidence`` = min(wrist, shoulder visibility at release) x a speed
    factor saturating at :data:`_SPEED_SATURATION` x the gate.

    Null paths (in precedence order, each with its own reason): empty track;
    wrist/shoulder never visible together; arm never overhead; overhead but
    never at swing speed.
    """
    if not track.frames:
        return _null_detection(REASON_EMPTY_TRACK)
    speeds = _wrist_speeds(track, config)
    saw_visible = False
    saw_overhead = False
    best: tuple[float, int, float, float] | None = None  # (wrist_y, frame, vis, speed)
    for index, frame in enumerate(track.frames):
        wrist = frame.landmark(config.wrist)
        shoulder = frame.landmark(config.shoulder)
        visibility = min(wrist.visibility, shoulder.visibility)
        if visibility < config.min_visibility:
            continue
        saw_visible = True
        if wrist.image_xy[1] >= shoulder.image_xy[1]:
            continue
        saw_overhead = True
        speed = speeds[index]
        if speed is None or speed < config.min_wrist_speed_px_per_ms:
            continue
        if best is None or wrist.image_xy[1] < best[0]:
            best = (wrist.image_xy[1], index, visibility, speed)
    if best is None:
        if not saw_visible:
            return _null_detection(REASON_WRIST_OCCLUDED)
        if not saw_overhead:
            return _null_detection(REASON_ARM_NEVER_OVERHEAD)
        return _null_detection(REASON_NO_ARM_SWING)
    _, frame_index, visibility, speed = best
    wrist_xy = track.frames[frame_index].landmark(config.wrist).image_xy
    speed_factor = min(speed / (config.min_wrist_speed_px_per_ms * _SPEED_SATURATION), 1.0)
    return ReleaseDetection(
        release_frame=frame_index,
        release_ms=frame_index * 1000.0 / track.fps,
        hand_xy=wrist_xy,
        confidence=visibility * speed_factor,
        reason=None,
    )


@dataclass(frozen=True)
class HeightEstimate:
    """Release height in cm above the pitch surface (or null-with-reason).

    ``source`` names the seam that produced the value: ``"stereo"`` (the
    triangulated 3D ball track, US-F6 calibrated path) or ``"pose_scale"``
    (wrist-above-ground x calibration scale). Null estimates carry
    ``source=None`` and ``confidence == 0.0``.
    """

    value_cm: float | None
    confidence: float
    source: str | None
    reason: str | None


def _null_height(reason: str) -> HeightEstimate:
    return HeightEstimate(value_cm=None, confidence=0.0, source=None, reason=reason)


#: Foot landmarks anchoring the ground reference (front foot planted at release).
_GROUND_LANDMARKS: Final = (
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
)


def _height_from_stereo(track3d: Track3D, release_ms: float) -> HeightEstimate:
    """Calibrated path: pitch-frame z (m) at release, from the 3D ball track."""
    estimate: ReleaseHeight = release_height_3d(track3d, release_ms)
    if estimate.value_m is None:
        return _null_height(str(estimate.reason))
    return HeightEstimate(
        value_cm=estimate.value_m * 100.0,
        confidence=estimate.confidence,
        source="stereo",
        reason=None,
    )


def _ground_reference(frame: PoseFrame, min_visibility: float) -> tuple[float, float] | None:
    """(ground image y, min foot visibility) from the visible foot landmarks.

    The ground reference is the lowest visible foot landmark (largest image y;
    image y grows downward) — at release the front foot is planted, so it sits
    on the pitch surface. ``None`` when every foot landmark is occluded.
    """
    feet = [
        frame.landmark(name)
        for name in _GROUND_LANDMARKS
        if frame.landmark(name).visibility >= min_visibility
    ]
    if not feet:
        return None
    ground_y = max(landmark.image_xy[1] for landmark in feet)
    return ground_y, min(landmark.visibility for landmark in feet)


def release_height_cm(
    track: PoseTrack,
    detection: ReleaseDetection,
    *,
    stereo: tuple[Track3D, float] | None = None,
    px_per_cm: float | None = None,
    config: ReleaseConfig = DEFAULT_RELEASE_CONFIG,
) -> HeightEstimate:
    """Release height in cm: stereo path first, pose+scale fallback (US-I2).

    ``stereo`` — the ball's triangulated 3D track plus the release time on ITS
    video timeline — selects the calibrated path; when it yields a value that
    wins. Otherwise the pose+scale path ((ground y - wrist y) / ``px_per_cm``
    at the release frame) runs when a scale is given. With neither seam
    available — or when both miss — the estimate is null with the most
    specific reason (the stereo miss reason survives only when no fallback
    exists).

    Raises :class:`ValueError` on a non-positive ``px_per_cm`` (caller
    contract).
    """
    if px_per_cm is not None and px_per_cm <= 0:
        raise ValueError(f"px_per_cm must be positive, got {px_per_cm}")
    if detection.release_frame is None or detection.hand_xy is None:
        return _null_height(REASON_NO_RELEASE)
    stereo_estimate: HeightEstimate | None = None
    if stereo is not None:
        stereo_estimate = _height_from_stereo(*stereo)
        if stereo_estimate.value_cm is not None:
            return stereo_estimate
    if px_per_cm is not None:
        ground = _ground_reference(track.frames[detection.release_frame], config.min_visibility)
        if ground is None:
            return _null_height(REASON_FEET_LOW)
        ground_y, foot_visibility = ground
        height_cm = (ground_y - detection.hand_xy[1]) / px_per_cm
        if height_cm <= 0.0:
            return _null_height(REASON_HAND_BELOW_GROUND)
        return HeightEstimate(
            value_cm=height_cm,
            confidence=detection.confidence * foot_visibility,
            source="pose_scale",
            reason=None,
        )
    return stereo_estimate if stereo_estimate is not None else _null_height(REASON_NO_SCALE)
