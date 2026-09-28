"""Bowling action-quality checkpoints at release (US-I2): deterministic formulas.

Evaluators over a bowler pose series (:class:`cricai_vision.pose.PoseTrack`,
extracted on C5/C6 under the :mod:`cricai_vision.bowler_pose` prior) at the
release frame found by :mod:`cricai_vision.release`. Every value is a
:class:`cricai_coaching.contact_metrics.MetricValue` — the pinned
``{value, unit, confidence, reason-if-null}`` contract — and is
nullable-with-reason under occlusion (US-I2 AC: the front-on view's
umpire-position occlusions are expected, so no silent zeros, ever).

Checkpoints (definitions documented in ``docs/coaching_metrics.md``):

* ``brace_state`` — front-leg knee angle at release, banded over
  :class:`cricai_data.enums.BraceState`: ``braced`` at or above
  ``braced_min_deg``, ``collapsed`` below ``collapsed_max_deg``, ``bent``
  between. The front leg is the arm's opposite leg (a right-arm bowler lands
  on the left foot). Band edges are initial targets pending the coach-labeled
  benchmark (US-I2 AC: >= 80% agreement on 100 deliveries).
* ``falling_away_deg`` — lateral trunk lean at release: the mid-hip ->
  mid-shoulder segment's angle from image vertical, signed so positive means
  leaning toward the bowler's non-bowling-arm side (falling away from the
  target line). On the pinned front-on C6 orientation a right-arm bowler's
  left side appears toward increasing image x; ``arm`` flips the sign.
* ``head_offset_at_release_px`` / ``_cm`` — nose x minus front-foot ankle x,
  positive toward the target (increasing image x on the pinned orientations;
  ``target_toward_positive_x`` flips it for a mirrored mount). Zero means the
  head is stacked over the front foot.

Confidence is the minimum visibility of the landmarks actually used
(single-frame measurements, matching the US-E2 convention); null metrics carry
``confidence == 0.0``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from cricai_data.enums import BraceState
from cricai_vision.pose import PoseFrame, PoseTrack

from cricai_coaching.contact_metrics import MetricValue
from cricai_coaching.pre_release import REASON_LOW_AVAILABILITY, REASON_NO_SCALE

#: Null reasons (US-I2: every checkpoint nullable-with-reason under occlusion).
REASON_FRONT_LEG_LOW: Final = "front-leg landmarks below visibility threshold"
REASON_TRUNK_LOW: Final = "shoulder/hip landmarks below visibility threshold"
REASON_HEAD_FOOT_LOW: Final = "nose or front-ankle landmarks below visibility threshold"
REASON_DEGENERATE_LEG: Final = "degenerate front-leg geometry (coincident landmarks)"
REASON_DEGENERATE_TRUNK: Final = "degenerate trunk geometry (coincident shoulders/hips)"

#: The checkpoint keys this module emits (ball_metrics phase ``pre_release``;
#: ``brace_state``/``falling_away_deg`` are BallRecord v1.1 names, contract #6).
CHECKPOINT_KEYS: Final[tuple[str, ...]] = (
    "brace_state",
    "falling_away_deg",
    "head_offset_at_release_px",
    "head_offset_at_release_cm",
)

BowlingArm = Literal["right", "left"]


@dataclass(frozen=True)
class CheckpointConfig:
    """Thresholds, bands and orientation switches for the release checkpoints."""

    arm: BowlingArm = "right"
    min_visibility: float = 0.5
    min_availability: float = 0.8
    braced_min_deg: float = 165.0
    collapsed_max_deg: float = 140.0
    target_toward_positive_x: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= self.min_visibility <= 1.0 and 0.0 <= self.min_availability <= 1.0):
            raise ValueError("min_visibility and min_availability must be in [0, 1]")
        if not 0.0 < self.collapsed_max_deg < self.braced_min_deg <= 180.0:
            raise ValueError(
                "brace bands must satisfy 0 < collapsed_max_deg < braced_min_deg <= 180"
            )

    @property
    def front_side(self) -> str:
        """The front-leg side: a right-arm bowler lands on the left foot."""
        return "left" if self.arm == "right" else "right"


DEFAULT_CHECKPOINT_CONFIG: Final = CheckpointConfig()


def _null(unit: str, reason: str) -> MetricValue:
    return MetricValue(value=None, unit=unit, confidence=0.0, reason=reason)


def _brace_state(frame: PoseFrame, config: CheckpointConfig) -> MetricValue:
    """Front-leg knee angle at release, banded over the BraceState vocabulary."""
    side = config.front_side
    hip = frame.landmark(f"{side}_hip")
    knee = frame.landmark(f"{side}_knee")
    ankle = frame.landmark(f"{side}_ankle")
    confidence = min(hip.visibility, knee.visibility, ankle.visibility)
    if confidence < config.min_visibility:
        return _null("class", REASON_FRONT_LEG_LOW)
    to_hip = (hip.image_xy[0] - knee.image_xy[0], hip.image_xy[1] - knee.image_xy[1])
    to_ankle = (ankle.image_xy[0] - knee.image_xy[0], ankle.image_xy[1] - knee.image_xy[1])
    norms = math.hypot(*to_hip) * math.hypot(*to_ankle)
    if norms == 0.0:
        return _null("class", REASON_DEGENERATE_LEG)
    cosine = (to_hip[0] * to_ankle[0] + to_hip[1] * to_ankle[1]) / norms
    angle = math.degrees(math.acos(min(max(cosine, -1.0), 1.0)))
    if angle >= config.braced_min_deg:
        state = BraceState.BRACED
    elif angle < config.collapsed_max_deg:
        state = BraceState.COLLAPSED
    else:
        state = BraceState.BENT
    return MetricValue(value=state.value, unit="class", confidence=confidence)


def _falling_away_deg(frame: PoseFrame, config: CheckpointConfig) -> MetricValue:
    """Signed trunk lean from vertical at release (positive = falling away)."""
    shoulders = [frame.landmark("left_shoulder"), frame.landmark("right_shoulder")]
    hips = [frame.landmark("left_hip"), frame.landmark("right_hip")]
    confidence = min(lm.visibility for lm in (*shoulders, *hips))
    if confidence < config.min_visibility:
        return _null("deg", REASON_TRUNK_LOW)
    mid_shoulder = (
        (shoulders[0].image_xy[0] + shoulders[1].image_xy[0]) / 2.0,
        (shoulders[0].image_xy[1] + shoulders[1].image_xy[1]) / 2.0,
    )
    mid_hip = (
        (hips[0].image_xy[0] + hips[1].image_xy[0]) / 2.0,
        (hips[0].image_xy[1] + hips[1].image_xy[1]) / 2.0,
    )
    dx = mid_shoulder[0] - mid_hip[0]
    dy_up = mid_hip[1] - mid_shoulder[1]  # image y grows downward
    if dx == 0.0 and dy_up == 0.0:
        return _null("deg", REASON_DEGENERATE_TRUNK)
    signed_dx = dx if config.arm == "right" else -dx
    lean = math.degrees(math.atan2(signed_dx, dy_up))
    return MetricValue(value=lean, unit="deg", confidence=confidence)


def _head_offsets(
    frame: PoseFrame, config: CheckpointConfig, px_per_cm: float | None
) -> tuple[MetricValue, MetricValue]:
    """Signed nose-x vs front-ankle-x, positive toward the target: (px, cm).

    The cm twin is null with the US-E2 no-scale reason when ``px_per_cm`` is
    absent, and shares the px metric's occlusion reason when that one is null.
    """
    nose = frame.landmark("nose")
    ankle = frame.landmark(f"{config.front_side}_ankle")
    confidence = min(nose.visibility, ankle.visibility)
    if confidence < config.min_visibility:
        return _null("px", REASON_HEAD_FOOT_LOW), _null("cm", REASON_HEAD_FOOT_LOW)
    raw = nose.image_xy[0] - ankle.image_xy[0]
    signed = raw if config.target_toward_positive_x else -raw
    px = MetricValue(value=signed, unit="px", confidence=confidence)
    if px_per_cm is None:
        return px, _null("cm", REASON_NO_SCALE)
    return px, MetricValue(value=signed / px_per_cm, unit="cm", confidence=confidence)


def _all_null(reason: str) -> dict[str, MetricValue]:
    return {
        "brace_state": _null("class", reason),
        "falling_away_deg": _null("deg", reason),
        "head_offset_at_release_px": _null("px", reason),
        "head_offset_at_release_cm": _null("cm", reason),
    }


def evaluate_checkpoints(
    track: PoseTrack,
    *,
    release_frame: int,
    px_per_cm: float | None = None,
    config: CheckpointConfig = DEFAULT_CHECKPOINT_CONFIG,
) -> dict[str, MetricValue]:
    """Evaluate all US-I2 release checkpoints for one delivery.

    Returns exactly :data:`CHECKPOINT_KEYS`: ``brace_state`` (class),
    ``falling_away_deg`` (deg), ``head_offset_at_release_px`` (px) and
    ``head_offset_at_release_cm`` (cm, null with the US-E2 no-scale reason when
    ``px_per_cm`` is absent). When the track's landmark availability is below
    ``config.min_availability`` every checkpoint is null with the same reason
    (matching the US-E2 convention — never silent zeros).

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
    frame = track.frames[release_frame]
    head_px, head_cm = _head_offsets(frame, config, px_per_cm)
    return {
        "brace_state": _brace_state(frame, config),
        "falling_away_deg": _falling_away_deg(frame, config),
        "head_offset_at_release_px": head_px,
        "head_offset_at_release_cm": head_cm,
    }
