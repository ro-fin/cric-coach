"""Pre-release batting metrics (US-E2): deterministic formulas over a pose track.

Every metric is a pure function of a :class:`cricai_vision.pose.PoseTrack` plus an
optional pixel-to-centimeter scale from calibration. Values follow the BallMetrics
contract — ``{value, unit, confidence, reason-if-null}`` — and are nullable with an
explicit reason whenever pose evidence is insufficient (US-E2 AC: no silent zeros).
The head-speed evidence behind ``still_at_release`` is its own first-class metric
(``head_speed_at_release``, unit ``px_per_ms``) so every emitted value conforms to
the pinned contract the ball-metrics API validates.

Frame/time conventions
    ``release_frame`` indexes the track frame at ball release. Millisecond values are
    relative to release (negative = before release), using ``1000 / track.fps`` per
    frame. Image ``y`` grows downward; angles are reported "screen-up" positive.

Sign conventions (documented in ``docs/coaching_metrics.md``)
    ``head_offset`` is positive toward the OFF side. On the pinned C1 rig orientation
    a right-handed batter's off side lies toward increasing image ``x``; for a
    left-handed batter the sign flips (``PreReleaseConfig.handedness``).

Confidence
    Derived from the visibilities of the landmarks actually used: the minimum for
    single-frame measurements (stance, head offset, pickup endpoints) and the mean
    over window measurements (trigger run, stillness window). Null metrics always
    carry ``confidence == 0.0``.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Final, Literal, NotRequired, TypedDict

from cricai_vision.pose import PoseFrame, PoseTrack

#: Exact reason string required by US-E2 when ``px_per_cm`` is not provided.
REASON_NO_SCALE: Final = "no calibration scale"
REASON_LOW_AVAILABILITY: Final = "insufficient pose availability"
REASON_REFERENCE_BEFORE_TRACK: Final = "reference frame before start of track"
REASON_ANKLES_LOW: Final = "ankle landmarks below visibility threshold"
REASON_HEAD_BASE_LOW: Final = "nose or ankle landmarks below visibility threshold"
REASON_INSUFFICIENT_FRAMES: Final = "insufficient frames before release"
REASON_TRIGGER_LOW_VIS: Final = "ankle/hip landmarks below visibility threshold"
REASON_NO_TRIGGER: Final = "no sustained movement before release"
REASON_NOSE_LOW: Final = "nose below visibility threshold"
REASON_WRISTS_LOW: Final = "wrist landmarks below visibility threshold"
REASON_NO_PICKUP: Final = "no discernible pickup movement"

_TRIGGER_LANDMARKS: Final = ("left_ankle", "right_ankle", "left_hip", "right_hip")

Handedness = Literal["right", "left"]


class MetricValue(TypedDict):
    """One metric entry matching the BallMetrics JSON contract (foundation schema)."""

    value: float | bool | None
    unit: str
    confidence: float
    reason: NotRequired[str]
    proxy: NotRequired[bool]


@dataclass(frozen=True)
class PreReleaseConfig:
    """Thresholds and windows for the pre-release metric formulas.

    All frame windows end at ``release_frame``; ``reference_offset_frames`` picks the
    stance/head sampling frame ``release_frame - reference_offset_frames``.
    """

    min_availability: float = 0.8
    min_visibility: float = 0.5
    reference_offset_frames: int = 0
    trigger_speed_px_per_ms: float = 0.05
    trigger_sustain_frames: int = 3
    stillness_window_frames: int = 4
    stillness_speed_px_per_ms: float = 0.1
    pickup_window_frames: int = 6
    pickup_min_displacement_px: float = 2.0
    handedness: Handedness = "right"

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_availability <= 1.0 and 0.0 <= self.min_visibility <= 1.0):
            raise ValueError("min_availability and min_visibility must be in [0, 1]")
        if (
            self.reference_offset_frames < 0
            or self.trigger_sustain_frames < 1
            or self.stillness_window_frames < 1
            or self.pickup_window_frames < 1
        ):
            raise ValueError("frame windows must be positive (reference offset non-negative)")
        if (
            self.trigger_speed_px_per_ms <= 0
            or self.stillness_speed_px_per_ms <= 0
            or self.pickup_min_displacement_px < 0
        ):
            raise ValueError("speed thresholds must be positive")


DEFAULT_CONFIG: Final = PreReleaseConfig()


def _null(unit: str, reason: str) -> MetricValue:
    return {"value": None, "unit": unit, "confidence": 0.0, "reason": reason}


def _metric(value: float | bool, unit: str, confidence: float) -> MetricValue:
    return {"value": value, "unit": unit, "confidence": confidence}


def _to_cm(px_metric: MetricValue, px_per_cm: float | None) -> MetricValue:
    """Convert a px metric to cm; null with reason when the px value or scale is missing."""
    if px_metric["value"] is None:
        return _null("cm", px_metric["reason"])
    if px_per_cm is None:
        return _null("cm", REASON_NO_SCALE)
    return _metric(float(px_metric["value"]) / px_per_cm, "cm", px_metric["confidence"])


def _stance_width_px(ref_frame: PoseFrame | None, config: PreReleaseConfig) -> MetricValue:
    """Ankle-to-ankle image distance (px) at the pre-release reference frame."""
    if ref_frame is None:
        return _null("px", REASON_REFERENCE_BEFORE_TRACK)
    left = ref_frame.landmark("left_ankle")
    right = ref_frame.landmark("right_ankle")
    if min(left.visibility, right.visibility) < config.min_visibility:
        return _null("px", REASON_ANKLES_LOW)
    width = math.hypot(right.image_xy[0] - left.image_xy[0], right.image_xy[1] - left.image_xy[1])
    return _metric(width, "px", min(left.visibility, right.visibility))


def _head_offset_px(ref_frame: PoseFrame | None, config: PreReleaseConfig) -> MetricValue:
    """Signed nose-x vs mid-ankle-x (px), positive toward the batter's off side."""
    if ref_frame is None:
        return _null("px", REASON_REFERENCE_BEFORE_TRACK)
    nose = ref_frame.landmark("nose")
    left = ref_frame.landmark("left_ankle")
    right = ref_frame.landmark("right_ankle")
    confidence = min(nose.visibility, left.visibility, right.visibility)
    if confidence < config.min_visibility:
        return _null("px", REASON_HEAD_BASE_LOW)
    mid_ankle_x = (left.image_xy[0] + right.image_xy[0]) / 2.0
    raw = nose.image_xy[0] - mid_ankle_x
    signed = raw if config.handedness == "right" else -raw
    return _metric(signed, "px", confidence)


def _interval_speed(prev: PoseFrame, cur: PoseFrame, ms_per_frame: float) -> float:
    """Mean ankle+hip speed (px/ms) between two frames (visibility checked by caller)."""
    total = 0.0
    for name in _TRIGGER_LANDMARKS:
        p, c = prev.landmark(name), cur.landmark(name)
        total += math.hypot(c.image_xy[0] - p.image_xy[0], c.image_xy[1] - p.image_xy[1])
    return total / len(_TRIGGER_LANDMARKS) / ms_per_frame


def _trigger_start_ms(
    track: PoseTrack, release_frame: int, config: PreReleaseConfig, ms_per_frame: float
) -> MetricValue:
    """Onset (ms vs release, negative = before) of the first sustained ankle/hip movement.

    An inter-frame interval qualifies when the mean ankle+hip speed is at or above
    ``trigger_speed_px_per_ms``; the onset is the end frame of the first interval in a
    run of ``trigger_sustain_frames`` consecutive qualifying intervals. Low-visibility
    intervals never qualify and reset the run (no fabricated movement).
    """
    if release_frame < config.trigger_sustain_frames:
        return _null("ms", REASON_INSUFFICIENT_FRAMES)
    run_length = 0
    run_visibilities: list[float] = []
    saw_usable_interval = False
    for end in range(1, release_frame + 1):
        prev, cur = track.frames[end - 1], track.frames[end]
        visibilities = [
            frame.landmark(name).visibility for frame in (prev, cur) for name in _TRIGGER_LANDMARKS
        ]
        if min(visibilities) < config.min_visibility:
            run_length, run_visibilities = 0, []
            continue
        saw_usable_interval = True
        speed = _interval_speed(prev, cur, ms_per_frame)
        if speed < config.trigger_speed_px_per_ms:
            run_length, run_visibilities = 0, []
            continue
        run_length += 1
        run_visibilities.extend(visibilities)
        if run_length == config.trigger_sustain_frames:
            onset_frame = end - run_length + 1
            value = (onset_frame - release_frame) * ms_per_frame
            return _metric(value, "ms", sum(run_visibilities) / len(run_visibilities))
    if not saw_usable_interval:
        return _null("ms", REASON_TRIGGER_LOW_VIS)
    return _null("ms", REASON_NO_TRIGGER)


def _stillness_null(reason: str) -> dict[str, MetricValue]:
    """Both stillness metrics null together: the evidence is null when the bool is."""
    return {
        "still_at_release": _null("bool", reason),
        "head_speed_at_release": _null("px_per_ms", reason),
    }


def _stillness_metrics(
    track: PoseTrack, release_frame: int, config: PreReleaseConfig, ms_per_frame: float
) -> dict[str, MetricValue]:
    """Head (nose) stillness over the window ending at release.

    Emits ``still_at_release`` (bool) plus its evidence ``head_speed_at_release``
    (px_per_ms) as a separate first-class metric so both entries conform to the
    pinned ``{value, unit, confidence, reason-if-null}`` contract.
    """
    start = release_frame - config.stillness_window_frames
    if start < 0:
        return _stillness_null(REASON_INSUFFICIENT_FRAMES)
    window = track.frames[start : release_frame + 1]
    visibilities = [frame.landmark("nose").visibility for frame in window]
    if min(visibilities) < config.min_visibility:
        return _stillness_null(REASON_NOSE_LOW)
    step_px = [
        math.hypot(
            cur.landmark("nose").image_xy[0] - prev.landmark("nose").image_xy[0],
            cur.landmark("nose").image_xy[1] - prev.landmark("nose").image_xy[1],
        )
        for prev, cur in itertools.pairwise(window)
    ]
    head_speed = sum(step_px) / len(step_px) / ms_per_frame
    confidence = sum(visibilities) / len(visibilities)
    return {
        "still_at_release": _metric(
            head_speed < config.stillness_speed_px_per_ms, "bool", confidence
        ),
        "head_speed_at_release": _metric(head_speed, "px_per_ms", confidence),
    }


def _pickup_direction_deg(
    track: PoseTrack, release_frame: int, config: PreReleaseConfig
) -> MetricValue:
    """Mid-wrist displacement angle over the pickup window ending at release.

    Degrees from image +x, "screen-up" positive (90 = straight up), range (-180, 180].
    Only the window's endpoint frames are sampled.
    """
    start = release_frame - config.pickup_window_frames
    if start < 0:
        return _null("deg", REASON_INSUFFICIENT_FRAMES)
    first, last = track.frames[start], track.frames[release_frame]
    wrists = [
        frame.landmark(name) for frame in (first, last) for name in ("left_wrist", "right_wrist")
    ]
    confidence = min(w.visibility for w in wrists)
    if confidence < config.min_visibility:
        return _null("deg", REASON_WRISTS_LOW)
    mid_first = (
        (wrists[0].image_xy[0] + wrists[1].image_xy[0]) / 2.0,
        (wrists[0].image_xy[1] + wrists[1].image_xy[1]) / 2.0,
    )
    mid_last = (
        (wrists[2].image_xy[0] + wrists[3].image_xy[0]) / 2.0,
        (wrists[2].image_xy[1] + wrists[3].image_xy[1]) / 2.0,
    )
    dx, dy = mid_last[0] - mid_first[0], mid_last[1] - mid_first[1]
    if math.hypot(dx, dy) < config.pickup_min_displacement_px:
        return _null("deg", REASON_NO_PICKUP)
    return _metric(math.degrees(math.atan2(-dy, dx)), "deg", confidence)


def _all_null(reason: str) -> dict[str, MetricValue]:
    return {
        "stance_width_cm": _null("cm", reason),
        "stance_width_px": _null("px", reason),
        "head_offset_cm": _null("cm", reason),
        "head_offset_px": _null("px", reason),
        "trigger_start_ms": _null("ms", reason),
        **_stillness_null(reason),
        "pickup_direction_deg": _null("deg", reason),
    }


def compute_pre_release(
    track: PoseTrack,
    *,
    release_frame: int,
    px_per_cm: float | None = None,
    config: PreReleaseConfig = DEFAULT_CONFIG,
) -> dict[str, MetricValue]:
    """Compute all US-E2 pre-release metrics for one ball.

    Returns exactly: ``stance_width_cm``, ``stance_width_px``, ``head_offset_cm``,
    ``head_offset_px``, ``trigger_start_ms``, ``still_at_release`` (bool),
    ``head_speed_at_release`` (its px_per_ms evidence, null whenever the bool is),
    ``pickup_direction_deg``. When the track's landmark availability is below
    ``config.min_availability`` every metric is null with the same reason
    (US-E2 AC — never silent zeros).

    Raises :class:`ValueError` on caller-contract violations (non-positive
    ``px_per_cm``, ``release_frame`` outside the track).
    """
    if px_per_cm is not None and px_per_cm <= 0:
        raise ValueError(f"px_per_cm must be positive, got {px_per_cm}")
    if track.availability(min_visibility=config.min_visibility) < config.min_availability:
        return _all_null(REASON_LOW_AVAILABILITY)
    if not 0 <= release_frame < track.frame_count:
        raise ValueError(
            f"release_frame {release_frame} outside track of {track.frame_count} frames"
        )
    ms_per_frame = 1000.0 / track.fps
    ref_index = release_frame - config.reference_offset_frames
    ref_frame = track.frames[ref_index] if ref_index >= 0 else None

    stance_px = _stance_width_px(ref_frame, config)
    head_px = _head_offset_px(ref_frame, config)
    return {
        "stance_width_cm": _to_cm(stance_px, px_per_cm),
        "stance_width_px": stance_px,
        "head_offset_cm": _to_cm(head_px, px_per_cm),
        "head_offset_px": head_px,
        "trigger_start_ms": _trigger_start_ms(track, release_frame, config, ms_per_frame),
        **_stillness_metrics(track, release_frame, config, ms_per_frame),
        "pickup_direction_deg": _pickup_direction_deg(track, release_frame, config),
    }
