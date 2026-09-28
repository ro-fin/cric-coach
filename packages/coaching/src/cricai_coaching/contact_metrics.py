"""US-E3 contact-phase batting metrics as pose-only proxies (bat/ball tracking is Epic F).

Every metric is a :class:`MetricValue` -- ``{value, unit, confidence, reason-if-null,
proxy?}`` -- nullable-with-reason, never a silent zero (US-E2/E3 AC). Formulas and the
classifier rule tables live in ``docs/coaching_metrics.md`` and are mirrored by
:class:`ContactConfig` so a coach-approved boundary change is configuration, not code.

Geometry conventions (side-on reference camera, image coordinates):

- image x grows to the right, image y grows DOWNWARD;
- ``config.toward_ball_sign`` maps an image-x displacement onto "toward the ball
  line" (``-1.0`` by default: the bowler is on the low-x side of the frame);
- the front foot is the batter's ``config.front_side`` foot (``"left"`` for a
  right-handed stance).

``bat_path_class`` is classified from the WRIST-midpoint path, not the bat, and
``contact_point_class`` from the wrist midpoint's position, not the ball; both are
tracking-free stand-ins and therefore always carry ``proxy=True`` until Epic F
bat/ball tracking lands (US-E3 AC).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

from cricai_vision.pose import PoseTrack

#: JSON-safe metric value payloads (bounce_xy is a 2-list; classes are strings).
MetricScalar = float | str | bool | list[float] | None


class MetricError(ValueError):
    """Raised for invalid metric inputs or contract violations (e.g. silent zeros)."""


@dataclass(frozen=True)
class MetricValue:
    """One metric observation under the pinned {value, unit, confidence, ...} contract.

    ``reason`` is REQUIRED whenever ``value`` is null (US-E2/E3: nullable-with-reason,
    never silent zeros). ``proxy`` marks stand-in formulas (US-E3 bat-path proxy) and
    ``source`` carries provenance for US-E4 unified flight metrics.
    """

    value: MetricScalar
    unit: str
    confidence: float
    reason: str | None = None
    proxy: bool = False
    source: str | None = None

    def __post_init__(self) -> None:
        if self.value is None and not self.reason:
            raise MetricError("null metric value requires a reason (no silent zeros, US-E3)")
        if not 0.0 <= self.confidence <= 1.0:
            raise MetricError(f"confidence must be in [0, 1], got {self.confidence}")

    def to_payload(self) -> dict[str, Any]:
        """JSON payload; optional keys appear only when meaningful."""
        payload: dict[str, Any] = {
            "value": self.value,
            "unit": self.unit,
            "confidence": self.confidence,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.proxy:
            payload["proxy"] = True
        if self.source is not None:
            payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class ContactConfig:
    """Thresholds and classifier rule tables for US-E3 contact metrics.

    Contact-point rule table (``d`` = wrist-midpoint px ahead of the nose, toward
    the ball line):

    ====================  =========================================
    class                 condition
    ====================  =========================================
    late                  d <= contact_point_late_max_px
    cramped               late_max < d <= contact_point_cramped_max_px
    under_eyes            cramped_max < d <= contact_point_under_eyes_max_px
    too_far_in_front      d > contact_point_under_eyes_max_px
    ====================  =========================================

    Bat-path (PROXY) rule table (``theta`` = wrist-path angle in degrees,
    ``0`` = straight down the pitch-vertical, ``90`` = flat toward the ball):

    ====================  =========================================
    class                 condition
    ====================  =========================================
    straight              straight_min <= theta <= straight_max
    across                straight_max < theta <= across_max
    closed                closed_min <= theta < straight_min
    open                  theta < closed_min or theta > across_max
    ====================  =========================================
    """

    min_availability: float = 0.8
    min_visibility: float = 0.5
    baseline_frames: int = 3
    toward_ball_sign: float = -1.0
    front_side: str = "left"
    balance_threshold_px: float = 30.0
    head_stability_shoulder_factor: float = 0.5
    contact_point_late_max_px: float = -40.0
    contact_point_cramped_max_px: float = -10.0
    contact_point_under_eyes_max_px: float = 35.0
    bat_path_window_frames: int = 5
    bat_path_min_travel_px: float = 5.0
    bat_path_closed_min_deg: float = -10.0
    bat_path_straight_min_deg: float = 25.0
    bat_path_straight_max_deg: float = 65.0
    bat_path_across_max_deg: float = 110.0

    def __post_init__(self) -> None:
        if self.front_side not in ("left", "right"):
            raise MetricError(f"front_side must be 'left' or 'right', got {self.front_side!r}")
        if self.toward_ball_sign not in (-1.0, 1.0):
            raise MetricError(f"toward_ball_sign must be -1.0 or 1.0, got {self.toward_ball_sign}")
        if self.baseline_frames < 1:
            raise MetricError(f"baseline_frames must be >= 1, got {self.baseline_frames}")
        if self.bat_path_window_frames < 2:
            raise MetricError(
                f"bat_path_window_frames must be >= 2, got {self.bat_path_window_frames}"
            )
        if not (
            self.contact_point_late_max_px
            < self.contact_point_cramped_max_px
            <= self.contact_point_under_eyes_max_px
        ):
            raise MetricError("contact-point rule table must be ordered late < cramped <= under")
        if not (
            self.bat_path_closed_min_deg
            < self.bat_path_straight_min_deg
            < self.bat_path_straight_max_deg
            <= self.bat_path_across_max_deg
        ):
            raise MetricError(
                "bat-path rule table must be ordered closed_min < straight_min"
                " < straight_max <= across_max"
            )


#: Every key :func:`compute_contact` emits, in stable output order.
CONTACT_METRIC_KEYS: tuple[str, ...] = (
    "front_foot_direction_cm",
    "front_foot_direction_px",
    "head_stability_score",
    "balance_at_contact",
    "balance_margin_px",
    "contact_point_class",
    "bat_path_class",
)

_UNITS: dict[str, str] = {
    "front_foot_direction_cm": "cm",
    "front_foot_direction_px": "px",
    "head_stability_score": "score",
    "balance_at_contact": "class",
    "balance_margin_px": "px",
    "contact_point_class": "class",
    "bat_path_class": "class",
}

#: Tracking-free stand-ins (wrist midpoint for bat/ball) — flagged until Epic F.
_PROXY_KEYS: frozenset[str] = frozenset({"bat_path_class", "contact_point_class"})


def _null(key: str, reason: str) -> MetricValue:
    """Null-with-reason; the stand-in proxy flag survives even on null (US-E3 AC)."""
    return MetricValue(None, _UNITS[key], 0.0, reason=reason, proxy=key in _PROXY_KEYS)


def _visibility_gate(
    track: PoseTrack, frame_idxs: list[int], names: tuple[str, ...], config: ContactConfig
) -> tuple[float, str | None]:
    """Mean used-landmark visibility plus the first below-threshold reason, if any."""
    values: list[float] = []
    reason: str | None = None
    for idx in frame_idxs:
        frame = track.frames[idx]
        for name in names:
            vis = frame.landmark(name).visibility
            values.append(vis)
            if vis < config.min_visibility and reason is None:
                reason = (
                    f"{name} visibility {vis:.2f} below {config.min_visibility:.2f} at frame {idx}"
                )
    return statistics.fmean(values), reason


def _wrist_mid_x(track: PoseTrack, frame_idx: int) -> float:
    frame = track.frames[frame_idx]
    left = frame.landmark("left_wrist").image_xy[0]
    return (left + frame.landmark("right_wrist").image_xy[0]) / 2


def _front_foot(
    track: PoseTrack, config: ContactConfig, contact_frame: int, px_per_cm: float | None
) -> dict[str, MetricValue]:
    """front_foot_direction: front-ankle lateral displacement from the stance baseline
    toward the ball line; always emitted in px, in cm only when a scale is known."""
    ankle = f"{config.front_side}_ankle"
    baseline_idx = list(range(min(config.baseline_frames, contact_frame + 1)))
    confidence, low = _visibility_gate(track, [*baseline_idx, contact_frame], (ankle,), config)
    if low is not None:
        return {
            "front_foot_direction_cm": _null("front_foot_direction_cm", low),
            "front_foot_direction_px": _null("front_foot_direction_px", low),
        }
    baseline_x = statistics.fmean(
        track.frames[idx].landmark(ankle).image_xy[0] for idx in baseline_idx
    )
    contact_x = track.frames[contact_frame].landmark(ankle).image_xy[0]
    displacement_px = (contact_x - baseline_x) * config.toward_ball_sign
    px_value = MetricValue(round(displacement_px, 2), "px", round(confidence, 4))
    if px_per_cm is None:
        cm_value = _null(
            "front_foot_direction_cm",
            "no pixel-to-cm scale (px_per_cm not provided); see front_foot_direction_px",
        )
    else:
        cm_value = MetricValue(round(displacement_px / px_per_cm, 2), "cm", round(confidence, 4))
    return {"front_foot_direction_cm": cm_value, "front_foot_direction_px": px_value}


def _head_stability(
    track: PoseTrack, config: ContactConfig, release_frame: int, contact_frame: int
) -> MetricValue:
    """score = clamp(1 - mean|x_nose - median(x_nose)| / (factor * shoulder_width), 0, 1)
    over the release->contact window; shoulder width is taken at the contact frame."""
    window = list(range(release_frame, contact_frame + 1))
    nose_conf, nose_low = _visibility_gate(track, window, ("nose",), config)
    shoulder_conf, shoulder_low = _visibility_gate(
        track, [contact_frame], ("left_shoulder", "right_shoulder"), config
    )
    low = nose_low or shoulder_low
    if low is not None:
        return _null("head_stability_score", low)
    xs = [track.frames[idx].landmark("nose").image_xy[0] for idx in window]
    median_x = statistics.median(xs)
    deviation = statistics.fmean(abs(x - median_x) for x in xs)
    contact = track.frames[contact_frame]
    shoulder_width = abs(
        contact.landmark("left_shoulder").image_xy[0]
        - contact.landmark("right_shoulder").image_xy[0]
    )
    full_scale = config.head_stability_shoulder_factor * shoulder_width
    if full_scale <= 0:
        return _null(
            "head_stability_score",
            "degenerate shoulder width at contact (cannot normalize head path)",
        )
    score = max(0.0, min(1.0, 1.0 - deviation / full_scale))
    return MetricValue(round(score, 4), "score", round((nose_conf + shoulder_conf) / 2, 4))


def _balance(track: PoseTrack, config: ContactConfig, contact_frame: int) -> dict[str, MetricValue]:
    """stable when the head is within threshold of, or ahead of (toward the ball),
    the front ankle at contact; the signed margin from the boundary is reported."""
    ankle = f"{config.front_side}_ankle"
    confidence, low = _visibility_gate(track, [contact_frame], ("nose", ankle), config)
    if low is not None:
        return {
            "balance_at_contact": _null("balance_at_contact", low),
            "balance_margin_px": _null("balance_margin_px", low),
        }
    frame = track.frames[contact_frame]
    offset_toward_ball = (
        frame.landmark("nose").image_xy[0] - frame.landmark(ankle).image_xy[0]
    ) * config.toward_ball_sign
    margin = offset_toward_ball + config.balance_threshold_px
    label = "stable" if margin >= 0 else "falling_away"
    rounded_conf = round(confidence, 4)
    return {
        "balance_at_contact": MetricValue(label, "class", rounded_conf),
        "balance_margin_px": MetricValue(round(margin, 2), "px", rounded_conf),
    }


def _contact_point(track: PoseTrack, config: ContactConfig, contact_frame: int) -> MetricValue:
    """PROXY (US-E3 AC): rule-table class over d = wrist-midpoint px ahead of the nose
    (toward the ball). The wrist midpoint stands in for the ball-contact position, so
    the value carries ``proxy=True`` until Epic F ball tracking replaces it."""
    confidence, low = _visibility_gate(
        track, [contact_frame], ("nose", "left_wrist", "right_wrist"), config
    )
    if low is not None:
        return _null("contact_point_class", low)
    nose_x = track.frames[contact_frame].landmark("nose").image_xy[0]
    d = (_wrist_mid_x(track, contact_frame) - nose_x) * config.toward_ball_sign
    if d <= config.contact_point_late_max_px:
        label = "late"
    elif d <= config.contact_point_cramped_max_px:
        label = "cramped"
    elif d <= config.contact_point_under_eyes_max_px:
        label = "under_eyes"
    else:
        label = "too_far_in_front"
    return MetricValue(label, "class", round(confidence, 4), proxy=True)


def _bat_path(
    track: PoseTrack, config: ContactConfig, release_frame: int, contact_frame: int
) -> MetricValue:
    """PROXY (US-E3 AC): wrist-midpoint path angle over the swing window, classified
    by the rule table; carries ``proxy=True`` until Epic F bat tracking replaces it."""
    start = max(release_frame, contact_frame - (config.bat_path_window_frames - 1))
    confidence, low = _visibility_gate(
        track, [start, contact_frame], ("left_wrist", "right_wrist"), config
    )
    if low is not None:
        return _null("bat_path_class", low)
    start_frame = track.frames[start]
    end_frame = track.frames[contact_frame]
    start_y = (
        start_frame.landmark("left_wrist").image_xy[1]
        + start_frame.landmark("right_wrist").image_xy[1]
    ) / 2
    end_y = (
        end_frame.landmark("left_wrist").image_xy[1] + end_frame.landmark("right_wrist").image_xy[1]
    ) / 2
    dx_toward_ball = (
        _wrist_mid_x(track, contact_frame) - _wrist_mid_x(track, start)
    ) * config.toward_ball_sign
    dy_down = end_y - start_y  # image y grows downward: positive = downswing
    travel = math.hypot(dx_toward_ball, dy_down)
    if travel < config.bat_path_min_travel_px:
        return _null(
            "bat_path_class",
            f"wrist travel {travel:.1f} px below {config.bat_path_min_travel_px:.1f} px"
            " in the swing window (cannot classify swing direction)",
        )
    theta = math.degrees(math.atan2(dx_toward_ball, dy_down))
    if theta > config.bat_path_straight_max_deg:
        label = "across" if theta <= config.bat_path_across_max_deg else "open"
    elif theta >= config.bat_path_straight_min_deg:
        label = "straight"
    elif theta >= config.bat_path_closed_min_deg:
        label = "closed"
    else:
        label = "open"
    return MetricValue(label, "class", round(confidence, 4), proxy=True)


def compute_contact(
    track: PoseTrack,
    *,
    contact_frame: int,
    release_frame: int = 0,
    px_per_cm: float | None = None,
    config: ContactConfig | None = None,
) -> dict[str, MetricValue]:
    """Compute all US-E3 contact-phase metrics for one ball's pose track.

    ``contact_frame``/``release_frame`` are indices into ``track.frames``. Every
    metric is nullable-with-reason on low track availability or low used-landmark
    visibility; per-metric confidence is the mean visibility of the landmarks and
    frames that metric used.
    """
    cfg = config if config is not None else ContactConfig()
    if px_per_cm is not None and px_per_cm <= 0:
        raise MetricError(f"px_per_cm must be positive, got {px_per_cm}")
    if not 0 <= release_frame <= contact_frame < track.frame_count:
        raise MetricError(
            f"need 0 <= release_frame ({release_frame}) <= contact_frame ({contact_frame})"
            f" < frame_count ({track.frame_count})"
        )
    availability = track.availability(min_visibility=cfg.min_visibility)
    if availability < cfg.min_availability:
        reason = f"pose availability {availability:.2f} below required {cfg.min_availability:.2f}"
        return {key: _null(key, reason) for key in CONTACT_METRIC_KEYS}
    metrics: dict[str, MetricValue] = {}
    metrics.update(_front_foot(track, cfg, contact_frame, px_per_cm))
    metrics["head_stability_score"] = _head_stability(track, cfg, release_frame, contact_frame)
    metrics.update(_balance(track, cfg, contact_frame))
    metrics["contact_point_class"] = _contact_point(track, cfg, contact_frame)
    metrics["bat_path_class"] = _bat_path(track, cfg, release_frame, contact_frame)
    return metrics
