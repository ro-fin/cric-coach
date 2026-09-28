"""Per-session data-quality score (US-L4): "random videos -> random advice" never silently.

Five components, each ``0..1`` and computed from injected plain row data (the
caller — worker job or report pipeline — reads the DB; this module stays pure):

- ``sync``: cross-camera event alignment proxy — the fraction of balls whose
  per-camera event windows agree within :data:`SYNC_TOLERANCE_MS`. A ball seen
  by fewer than two cameras cannot be verified and scores 0 (unverified is
  untrusted, never assumed good).
- ``exposure``: proxy over sampled mean-luma values — the fraction inside the
  usable band :data:`EXPOSURE_LUMA_RANGE`. No samples means no evidence: 0.
- ``pose_coverage``: mean over balls of the best camera's PoseTrack
  ``availability`` (a ball with no pose contributes 0).
- ``track_coverage``: mean over balls of the best camera's BallTrack
  ``coverage`` (a ball with no track contributes 0).
- ``calibration_freshness``: age/suspect state of the session's calibration —
  1 when fresh, decaying linearly to 0 at :data:`CALIBRATION_STALE_DAYS`;
  a missing calibration or a US-C4 ``calibration_suspect`` flag scores 0.

The composite is the weighted mean (:data:`COMPONENT_WEIGHTS`); below
:data:`HONESTY_BANNER_THRESHOLD` the report must lead with the honesty banner
(pinned cross-group contract #6: ``{components, composite, banner}``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Weighted-mean weights of the composite; keys are the pinned component names.
COMPONENT_WEIGHTS: dict[str, float] = {
    "sync": 0.20,
    "exposure": 0.15,
    "pose_coverage": 0.25,
    "track_coverage": 0.25,
    "calibration_freshness": 0.15,
}

#: Max spread (ms) between per-camera event times for a ball to count as synced.
SYNC_TOLERANCE_MS = 50.0

#: Usable mean-luma band (8-bit): below is underexposed, above is blown out.
EXPOSURE_LUMA_RANGE: tuple[float, float] = (60.0, 200.0)

#: Calibration age (days) with full credit, and the age where credit hits zero.
CALIBRATION_FRESH_DAYS = 30.0
CALIBRATION_STALE_DAYS = 120.0

#: Composite below this leads the report with the honesty banner (US-L4 AC).
HONESTY_BANNER_THRESHOLD = 0.7


@dataclass(frozen=True)
class SessionQualityInputs:
    """Plain row data for one session, injected by the caller (US-L4).

    ``ball_nos`` is the valid-ball universe (rejected events are not balls,
    US-D4); the per-ball maps are keyed ``ball_no -> camera_id -> value``.
    """

    ball_nos: tuple[int, ...]
    event_offsets_ms: Mapping[int, Mapping[str, float]]
    luma_samples: Mapping[str, Sequence[float]]
    pose_availability: Mapping[int, Mapping[str, float]]
    track_coverage: Mapping[int, Mapping[str, float]]
    calibration_age_days: float | None
    calibration_suspect: bool


@dataclass(frozen=True)
class QualityScore:
    """The pinned QualityScore contract: components, composite, banner."""

    components: dict[str, float]
    composite: float
    banner: str | None

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict shape consumed by reports (pinned contract #6)."""
        return {
            "components": dict(self.components),
            "composite": self.composite,
            "banner": self.banner,
        }


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def sync_component(
    ball_nos: Sequence[int],
    event_offsets_ms: Mapping[int, Mapping[str, float]],
    *,
    tolerance_ms: float = SYNC_TOLERANCE_MS,
) -> float:
    """Fraction of balls whose cross-camera event times agree within tolerance.

    Balls observed by fewer than two cameras cannot be cross-checked and count
    as unverified (0): a single-camera session earns no sync credit.
    """
    if not ball_nos:
        return 0.0
    aligned = 0
    for ball_no in ball_nos:
        times = list(event_offsets_ms.get(ball_no, {}).values())
        if len(times) >= 2 and max(times) - min(times) <= tolerance_ms:
            aligned += 1
    return aligned / len(ball_nos)


def exposure_component(
    luma_samples: Mapping[str, Sequence[float]],
    *,
    luma_range: tuple[float, float] = EXPOSURE_LUMA_RANGE,
) -> float:
    """Fraction of sampled mean-luma values inside the usable band.

    No samples means no exposure evidence — the component is 0, never an
    assumed-good default.
    """
    low, high = luma_range
    total = 0
    usable = 0
    for samples in luma_samples.values():
        for value in samples:
            total += 1
            if low <= value <= high:
                usable += 1
    if total == 0:
        return 0.0
    return usable / total


def _best_per_ball(ball_nos: Sequence[int], per_ball: Mapping[int, Mapping[str, float]]) -> float:
    """Mean over balls of the best camera's value; absent balls contribute 0."""
    if not ball_nos:
        return 0.0
    total = 0.0
    for ball_no in ball_nos:
        values = per_ball.get(ball_no, {}).values()
        if values:
            total += _clamp01(max(values))
    return total / len(ball_nos)


def pose_coverage_component(
    ball_nos: Sequence[int], pose_availability: Mapping[int, Mapping[str, float]]
) -> float:
    """PoseTrack availability (US-E1) averaged over the valid-ball universe."""
    return _best_per_ball(ball_nos, pose_availability)


def track_coverage_component(
    ball_nos: Sequence[int], track_coverage: Mapping[int, Mapping[str, float]]
) -> float:
    """BallTrack coverage (US-F3) averaged over the valid-ball universe."""
    return _best_per_ball(ball_nos, track_coverage)


def calibration_freshness_component(
    calibration_age_days: float | None,
    calibration_suspect: bool,
    *,
    fresh_days: float = CALIBRATION_FRESH_DAYS,
    stale_days: float = CALIBRATION_STALE_DAYS,
) -> float:
    """Freshness of the session's calibration: age decay plus the US-C4 flag.

    A session with no calibration attached, or one flagged
    ``calibration_suspect`` (bump/drift detected), scores 0 outright.
    """
    if calibration_age_days is None or calibration_suspect:
        return 0.0
    if calibration_age_days <= fresh_days:
        return 1.0
    return _clamp01((stale_days - calibration_age_days) / (stale_days - fresh_days))


def composite_score(components: Mapping[str, float]) -> float:
    """Weighted mean over :data:`COMPONENT_WEIGHTS`; keys must match exactly."""
    if set(components) != set(COMPONENT_WEIGHTS):
        raise ValueError(
            f"components must be exactly {sorted(COMPONENT_WEIGHTS)}, got {sorted(components)}"
        )
    total_weight = sum(COMPONENT_WEIGHTS.values())
    weighted = sum(COMPONENT_WEIGHTS[name] * _clamp01(value) for name, value in components.items())
    return weighted / total_weight


def honesty_banner(
    composite: float,
    components: Mapping[str, float],
    *,
    threshold: float = HONESTY_BANNER_THRESHOLD,
) -> str | None:
    """The report-leading honesty banner when the composite is below threshold.

    Names the weakest components so a parent reads *why* the numbers are
    shaky, not just that they are (US-L4 AC: low score adds an honesty banner).
    """
    if composite >= threshold:
        return None
    weakest = sorted(
        (name for name, value in components.items() if value < threshold),
        key=lambda name: (components[name], name),
    )
    detail = ", ".join(weakest).replace("_", " ")
    return (
        f"Data quality was low this session (score {round(composite * 100)}%; "
        f"weakest: {detail}). Numbers below may be unreliable — treat specific "
        "figures with caution."
    )


def score_session(
    inputs: SessionQualityInputs, *, tolerance_ms: float = SYNC_TOLERANCE_MS
) -> QualityScore:
    """Compute the full pinned QualityScore for one session (US-L4)."""
    components = {
        "sync": sync_component(inputs.ball_nos, inputs.event_offsets_ms, tolerance_ms=tolerance_ms),
        "exposure": exposure_component(inputs.luma_samples),
        "pose_coverage": pose_coverage_component(inputs.ball_nos, inputs.pose_availability),
        "track_coverage": track_coverage_component(inputs.ball_nos, inputs.track_coverage),
        "calibration_freshness": calibration_freshness_component(
            inputs.calibration_age_days, inputs.calibration_suspect
        ),
    }
    composite = composite_score(components)
    return QualityScore(
        components=components,
        composite=composite,
        banner=honesty_banner(composite, components),
    )
