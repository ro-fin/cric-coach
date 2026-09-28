"""Calibration drift detection (US-C4): landmark re-detection shift statistics.

Pure math, no I/O. The API layer projects the stored era calibration to get the
expected pixel position of each named landmark, re-detects the same landmarks
in a fresh frame, and feeds both here. A median pixel shift beyond the
threshold (initial target: ~5 px, US-C3/C4) means the camera has moved and the
session must be flagged ``calibration_suspect``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass

#: US-C4 initial target: > 5 px median landmark shift means the camera moved.
DEFAULT_DRIFT_THRESHOLD_PX = 5.0


@dataclass(frozen=True)
class ShiftStats:
    """Pixel-shift statistics over the landmarks common to both detections."""

    per_landmark: dict[str, float]
    median_px: float
    max_px: float
    n: int


def landmark_shift_stats(
    expected_px: Mapping[str, tuple[float, float]],
    observed_px: Mapping[str, tuple[float, float]],
) -> ShiftStats:
    """Euclidean pixel shift per landmark present in BOTH mappings.

    Landmarks present on only one side are ignored (partial re-detection is
    normal); an empty intersection raises ``ValueError`` because a drift
    verdict from zero landmarks would be meaningless.
    """
    names = sorted(set(expected_px) & set(observed_px))
    if not names:
        raise ValueError("no common landmarks between expected and observed sets")
    shifts = {name: math.dist(expected_px[name], observed_px[name]) for name in names}
    values = list(shifts.values())
    return ShiftStats(
        per_landmark=shifts,
        median_px=statistics.median(values),
        max_px=max(values),
        n=len(values),
    )


def is_drifted(stats: ShiftStats, *, threshold_px: float = DEFAULT_DRIFT_THRESHOLD_PX) -> bool:
    """Camera-moved verdict: median shift strictly beyond the threshold (US-C4)."""
    return stats.median_px > threshold_px
