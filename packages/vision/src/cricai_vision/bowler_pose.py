"""Bowler selection among detected people on the bowling-side cameras (US-I2).

The bowling pipeline tracks the BOWLER, not the batter: at release the frame
also contains the batter down the pitch, an umpire-position observer in the
front-on view (occlusions there are expected, US-I2 AC) and bystanders. This
module REUSES the US-E1 person-selection geometry — zone prior + track
continuity + size prior, blended by landmark visibility — and reparameterizes
only what actually differs: the location prior, which becomes the bowling
crease zone as seen from each bowling-side camera.

:mod:`cricai_vision.person_select` is composed, never forked: the scoring math
and the no-selection contract ("never fabricate a subject") are identical, so
any future fix to the selection geometry lands for both subjects at once.

Camera zones (normalized frame fractions, pinned rig orientations, US-I1):

* **C5** — side-on, square of the bowling crease 4-6 m away. The bowler crosses
  the middle of the frame at release; the batter is out of frame or at the far
  edge, so the zone spans the central band.
* **C6** — front-on, behind the batting end. The bowler approaches down the
  pitch line and appears in the central corridor; the batter and stumps sit at
  the frame's bottom, outside the zone's lower edge.

Zone boxes are starting points tuned on the pinned rig placements (backlog
honesty: initial targets, not final claims) and overridable per call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from cricai_vision.person_select import (
    Box,
    PersonCandidate,
    Selection,
    score_candidate,
    select_batter,
)

#: Bowling crease zone per bowling-side camera (normalized x0, y0, x1, y1).
#: C5 (side-on): central band where the bowler crosses at release.
#: C6 (front-on): the pitch-line corridor above the batting-end clutter.
DEFAULT_BOWLING_ZONES: Final[dict[str, Box]] = {
    "C5": (0.30, 0.10, 0.75, 0.95),
    "C6": (0.35, 0.15, 0.65, 0.80),
}

#: Cameras with a pinned bowler zone (the two pose views of US-I2; C7 is the
#: wrist close-up — one hand fills the frame, so person selection is moot).
BOWLER_POSE_CAMERAS: Final[tuple[str, ...]] = tuple(DEFAULT_BOWLING_ZONES)


class BowlerZoneError(ValueError):
    """Raised when no bowler zone is known for the requested camera."""


def bowling_crease_zone(camera_id: str) -> Box:
    """The pinned bowler location prior for one bowling-side camera.

    Raises :class:`BowlerZoneError` for cameras without a pinned zone — a
    caller wiring e.g. C1 into the bowler path is a bug, not a default.
    """
    try:
        return DEFAULT_BOWLING_ZONES[camera_id]
    except KeyError as exc:
        raise BowlerZoneError(
            f"no bowler zone pinned for camera {camera_id!r} "
            f"(known: {', '.join(BOWLER_POSE_CAMERAS)})"
        ) from exc


def _resolve_zone(camera_id: str | None, zone: Box | None) -> Box:
    """Exactly one of ``camera_id``/``zone`` picks the prior (loud otherwise)."""
    if zone is not None:
        if camera_id is not None:
            raise BowlerZoneError("pass exactly one of camera_id or zone")
        return zone
    if camera_id is None:
        raise BowlerZoneError("pass exactly one of camera_id or zone")
    return bowling_crease_zone(camera_id)


def score_bowler(
    candidate: PersonCandidate,
    *,
    camera_id: str | None = None,
    zone: Box | None = None,
    previous: PersonCandidate | None = None,
) -> float:
    """Bowler likelihood in [0, 1]: US-E1 scoring under the bowling crease zone."""
    return score_candidate(candidate, crease_zone=_resolve_zone(camera_id, zone), previous=previous)


def select_bowler(
    candidates: Sequence[PersonCandidate],
    *,
    camera_id: str | None = None,
    zone: Box | None = None,
    previous: PersonCandidate | None = None,
) -> Selection:
    """Pick the bowler among ``candidates`` (or nobody — never fabricate, US-I2).

    Same contract as :func:`cricai_vision.person_select.select_batter`, with the
    bowling crease zone as the location prior: the front-on umpire-view
    occlusion case (US-I2 AC) resolves to ``Selection(None, None)`` so callers
    emit null-with-reason checkpoints instead of tracking the wrong person.
    """
    return select_batter(candidates, crease_zone=_resolve_zone(camera_id, zone), previous=previous)
