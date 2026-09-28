"""US-I2 bowler selection: crease-zone reparameterization of the US-E1 geometry."""

import pytest
from cricai_vision.bowler_pose import (
    BOWLER_POSE_CAMERAS,
    DEFAULT_BOWLING_ZONES,
    BowlerZoneError,
    bowling_crease_zone,
    score_bowler,
    select_bowler,
)
from cricai_vision.person_select import Box, PersonCandidate, score_candidate

#: Bowler at the crease on C5: central band, decent size, clearly visible.
BOWLER = PersonCandidate(bbox=(0.45, 0.3, 0.62, 0.85), mean_visibility=0.95, track_id=3)
#: Batter at the frame's far edge on C5 (down the pitch, outside the zone).
BATTER = PersonCandidate(bbox=(0.88, 0.4, 0.99, 0.8), mean_visibility=0.9, track_id=4)
#: Bystander in a far corner: tiny and outside the zone.
BYSTANDER = PersonCandidate(bbox=(0.02, 0.05, 0.07, 0.16), mean_visibility=0.85, track_id=5)


def test_zone_pinned_for_both_pose_cameras() -> None:
    assert BOWLER_POSE_CAMERAS == ("C5", "C6")
    for camera_id in BOWLER_POSE_CAMERAS:
        x0, y0, x1, y1 = bowling_crease_zone(camera_id)
        assert 0.0 <= x0 < x1 <= 1.0
        assert 0.0 <= y0 < y1 <= 1.0


def test_unknown_camera_is_loud() -> None:
    with pytest.raises(BowlerZoneError, match="no bowler zone pinned for camera 'C1'"):
        bowling_crease_zone("C1")


def test_bowler_wins_multi_person_layout_on_c5() -> None:
    selection = select_bowler([BATTER, BOWLER, BYSTANDER], camera_id="C5")
    assert selection.candidate is BOWLER
    assert selection.confidence is not None
    assert 0.0 < selection.confidence <= 1.0


def test_no_selection_when_nobody_in_zone() -> None:
    """Front-on umpire-occlusion case (US-I2 AC): never fabricate a bowler."""
    selection = select_bowler([BATTER, BYSTANDER], camera_id="C6")
    assert selection.candidate is None
    assert selection.confidence is None


def test_zone_override_replaces_the_pinned_prior() -> None:
    corner_zone: Box = (0.0, 0.0, 0.1, 0.2)
    selection = select_bowler([BOWLER, BYSTANDER], zone=corner_zone)
    assert selection.candidate is BYSTANDER


def test_scoring_matches_person_select_under_the_bowling_zone() -> None:
    """Composition, not a fork: identical scores to US-E1 with the C5 zone."""
    expected = score_candidate(BOWLER, crease_zone=DEFAULT_BOWLING_ZONES["C5"], previous=BATTER)
    assert score_bowler(BOWLER, camera_id="C5", previous=BATTER) == expected


def test_continuity_keeps_the_bowler_through_the_follow_through() -> None:
    """A matching track_id keeps the subject as the bowler drifts off the zone."""
    drifted = PersonCandidate(bbox=(0.78, 0.3, 0.95, 0.85), mean_visibility=0.95, track_id=3)
    selection = select_bowler([drifted, BYSTANDER], camera_id="C5", previous=BOWLER)
    assert selection.candidate is drifted


def test_both_camera_and_zone_rejected() -> None:
    with pytest.raises(BowlerZoneError, match="exactly one of camera_id or zone"):
        select_bowler([BOWLER], camera_id="C5", zone=(0.0, 0.0, 1.0, 1.0))


def test_neither_camera_nor_zone_rejected() -> None:
    with pytest.raises(BowlerZoneError, match="exactly one of camera_id or zone"):
        score_bowler(BOWLER)
