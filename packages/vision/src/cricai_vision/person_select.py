"""Batter selection among detected people (US-E1).

Multiple people share the frame (batter, coach behind the net, a parent in the
corner). The tracked subject must be the batter, chosen by a location prior
(the crease zone) plus track continuity — never by guessing. When no candidate
plausibly is the batter, :func:`select_batter` returns the no-selection path
(``Selection(None, None)``) so callers flag the frame instead of fabricating a
subject (US-E1 AC).

Scoring is pure geometry over normalized boxes:

* crease-zone prior — containment of the candidate box in the zone blended
  with box/zone IoU,
* continuity bonus — a matching ``track_id`` with the previous selection is a
  full bonus; otherwise box IoU with the previous selection (keeps the track
  through brief occlusions when the batter drifts out of the zone),
* size prior — a batter fills a meaningful fraction of the frame; specks in
  the background score low.

The blend is weighted by the candidate's mean landmark visibility, so a barely
visible person never outranks a clearly seen one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Normalized (x0, y0, x1, y1) rectangle; coordinates are fractions of the frame.
Box = tuple[float, float, float, float]

#: Score blend weights (sum to 1.0): the location prior dominates.
ZONE_WEIGHT = 0.6
CONTINUITY_WEIGHT = 0.25
SIZE_WEIGHT = 0.15

#: Box area (fraction of the frame) of a typically framed batter; larger saturates.
REFERENCE_AREA = 0.05

#: Continuity at or above this keeps the track even with zero zone overlap.
CONTINUITY_KEEP = 0.5

#: Below this score nobody is plausibly the batter — return the no-selection path.
MIN_CONFIDENCE = 0.2


@dataclass(frozen=True)
class PersonCandidate:
    """One detected person: normalized box, mean landmark visibility, optional track id."""

    bbox: Box
    mean_visibility: float
    track_id: int | None = None


@dataclass(frozen=True)
class Selection:
    """The chosen batter (or ``(None, None)`` when no candidate plausibly is one)."""

    candidate: PersonCandidate | None
    confidence: float | None


def _area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(a: Box, b: Box) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0.0 or height <= 0.0:
        return 0.0
    return width * height


def _iou(a: Box, b: Box) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0.0 else 0.0


def _containment(inner: Box, outer: Box) -> float:
    """Fraction of ``inner`` lying inside ``outer``; 0.0 for degenerate boxes."""
    area = _area(inner)
    return _intersection(inner, outer) / area if area > 0.0 else 0.0


def _zone_score(candidate: PersonCandidate, crease_zone: Box) -> float:
    return 0.5 * _containment(candidate.bbox, crease_zone) + 0.5 * _iou(candidate.bbox, crease_zone)


def _continuity(candidate: PersonCandidate, previous: PersonCandidate | None) -> float:
    if previous is None:
        return 0.0
    if candidate.track_id is not None and candidate.track_id == previous.track_id:
        return 1.0
    return _iou(candidate.bbox, previous.bbox)


def _size_score(candidate: PersonCandidate) -> float:
    return min(_area(candidate.bbox) / REFERENCE_AREA, 1.0)


def _validate_zone(crease_zone: Box) -> None:
    x0, y0, x1, y1 = crease_zone
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"crease_zone must be a non-empty (x0, y0, x1, y1) box: {crease_zone!r}")


def score_candidate(
    candidate: PersonCandidate, *, crease_zone: Box, previous: PersonCandidate | None = None
) -> float:
    """Batter likelihood in [0, 1]: weighted zone/continuity/size blend x visibility."""
    _validate_zone(crease_zone)
    blend = (
        ZONE_WEIGHT * _zone_score(candidate, crease_zone)
        + CONTINUITY_WEIGHT * _continuity(candidate, previous)
        + SIZE_WEIGHT * _size_score(candidate)
    )
    return blend * min(max(candidate.mean_visibility, 0.0), 1.0)


def select_batter(
    candidates: Sequence[PersonCandidate],
    *,
    crease_zone: Box,
    previous: PersonCandidate | None = None,
) -> Selection:
    """Pick the batter among ``candidates`` (or nobody — never fabricate, US-E1 AC).

    The best-scoring candidate is selected only when it plausibly is the batter:
    it must touch the crease zone or continue the previous track, and its score
    must clear :data:`MIN_CONFIDENCE`.
    """
    _validate_zone(crease_zone)
    best: PersonCandidate | None = None
    best_score = 0.0
    for candidate in candidates:
        score = score_candidate(candidate, crease_zone=crease_zone, previous=previous)
        if best is None or score > best_score:
            best = candidate
            best_score = score
    if best is None:
        return Selection(candidate=None, confidence=None)
    plausible = (
        _zone_score(best, crease_zone) > 0.0 or _continuity(best, previous) >= CONTINUITY_KEEP
    )
    if not plausible or best_score < MIN_CONFIDENCE:
        return Selection(candidate=None, confidence=None)
    return Selection(candidate=best, confidence=best_score)
